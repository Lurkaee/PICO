"""Voice service coordinating local audio capture, transcription, and conversation delivery (Milestone P11.3).

Architectural Invariants:
1. Voice input is a presentation modality into PICO's standard conversation pipeline; it NEVER
   bypasses context assembly, model scheduling, capability allowlists, or human approval.
2. Raw audio buffers are never stored on disk, never logged, and never sent to cloud providers.
3. Transcripts inherit trusted perception classification (at least INTERNAL) and fail closed
   against cloud routing unless explicitly PUBLIC and cloud-enabled.
4. Replay and duplicate delivery protection: Completed sessions can only be delivered once.
5. Session expiry: Sessions older than TTL are rejected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.core.id_gen import generate_id
from pico.perception.audio.adapter import AUDIO_SESSION_TTL_SECONDS, AudioSessionManager, utc_now
from pico.perception.classification import compute_trusted_perception_classification
from pico.perception.contracts import AudioCaptureState, Modality
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository

MAX_TRANSCRIPT_LENGTH_CHARS = 10000


class VoiceService:
    """Orchestrates delivery of verified voice transcripts into conversation history."""

    def __init__(self, audio_manager: AudioSessionManager | None = None) -> None:
        from pico.perception.pipeline import _shared_audio_manager

        self.audio_manager = audio_manager or _shared_audio_manager

    def deliver_transcript_to_conversation(
        self,
        session_id: str,
        conversation_id: str,
        db: Session,
        caller_operator_id: str | None = None,
    ) -> dict[str, Any]:
        """Deliver the transcribed speech from a completed audio session into a conversation.

        Args:
            session_id: The completed audio capture session identifier.
            conversation_id: The destination conversation identifier.
            db: Active database session.
            caller_operator_id: Identity of the authenticated calling operator.

        Returns:
            Dictionary containing delivery metadata, message ID, and effective classification.

        Raises:
            ValidationError: If the session is uncompleted, expired, already delivered, or has invalid content.
            AuthorizationError: If caller operator does not match session ownership.
            EntityNotFoundError: If the conversation does not exist.
        """
        session = self.audio_manager.get_session(session_id)
        if not session:
            raise ValidationError(f"Audio session '{session_id}' not found.")

        # Thread-safe atomic delivery transition
        with session._delivery_lock:
            # Replay and duplicate delivery guard
            if getattr(session, "delivered", False):
                raise ValidationError(f"Audio session '{session_id}' has already been delivered. Replay is rejected.")

            # Cancelled session guard
            if session.state == AudioCaptureState.CANCELLED:
                raise ValidationError(f"Cannot deliver transcript: session '{session_id}' has been cancelled.")

            # Completion state guard
            if session.state != AudioCaptureState.COMPLETED:
                raise ValidationError(
                    f"Cannot deliver transcript: session '{session_id}' state is "
                    f"'{session.state.value}', not 'completed'."
                )

            # Expiry guard
            now = utc_now()
            age_seconds = (now - session.started_at).total_seconds()
            if age_seconds > AUDIO_SESSION_TTL_SECONDS:
                raise ValidationError(
                    f"Audio session '{session_id}' has expired ({age_seconds:.0f}s > {AUDIO_SESSION_TTL_SECONDS}s)."
                )

            # Operator ownership guard: caller must match session creator
            if session.operator_id and caller_operator_id and session.operator_id != caller_operator_id:
                raise AuthorizationError(
                    f"Caller '{caller_operator_id}' is not authorized to deliver "
                    f"audio session owned by '{session.operator_id}'."
                )

            # Destination conversation binding guard
            if session.conversation_id and session.conversation_id != conversation_id:
                raise ValidationError(
                    f"Audio session '{session_id}' was initialized for conversation "
                    f"'{session.conversation_id}', but delivery was attempted for "
                    f"conversation '{conversation_id}'."
                )

            transcription = (session.transcription or "").strip()
            if not transcription:
                raise ValidationError(f"Audio session '{session_id}' has no transcription text to deliver.")

            if len(transcription) > MAX_TRANSCRIPT_LENGTH_CHARS:
                raise ValidationError(
                    f"Transcript length ({len(transcription)}) exceeds limit ({MAX_TRANSCRIPT_LENGTH_CHARS})."
                )

            conv_repo = ConversationRepository(db)
            conversation = conv_repo.get_by_id(conversation_id)
            if not conversation:
                raise EntityNotFoundError("Conversation", conversation_id)

            msg_repo = MessageRepository(db)

            # Database-level persistence deduplication check (defense-in-depth against replay)
            existing_messages = msg_repo.list_by_conversation(conversation_id=conversation_id, limit=500)
            if any(
                m.metadata_json and m.metadata_json.get("perception_session_id") == session_id
                for m in existing_messages
            ):
                session.delivered = True
                raise ValidationError(
                    f"Audio session '{session_id}' was already recorded in conversation '{conversation_id}'. "
                    "Replay is rejected."
                )

            # Compute effective classification from trusted perception boundary (enforce at least INTERNAL)
            effective_classification = compute_trusted_perception_classification(
                modality=Modality.AUDIO,
                source_device=session.source_device,
            )
            if effective_classification == "PUBLIC":
                effective_classification = "INTERNAL"

            metadata_dict = {
                "source": "voice_transcript",
                "perception_session_id": session_id,
                "duration_seconds": getattr(session, "duration_seconds", 0.0),
                "operator_id": caller_operator_id or session.operator_id or "local_operator",
            }

            message_id = generate_id("msg")
            msg = msg_repo.create(
                message_id=message_id,
                conversation_id=conversation_id,
                role="user",
                content=transcription,
                data_classification=effective_classification,
                metadata_json=metadata_dict,
            )

            # Mark delivered to prevent duplicate replay
            session.delivered = True
            session.delivered_at = now
            session.delivered_message_id = msg.id

            created_iso = msg.created_at.isoformat() if hasattr(msg.created_at, "isoformat") else str(msg.created_at)
            return {
                "session_id": session_id,
                "conversation_id": conversation_id,
                "message_id": msg.id,
                "transcription": transcription,
                "data_classification": effective_classification,
                "delivered": True,
                "created_at": created_iso,
            }
