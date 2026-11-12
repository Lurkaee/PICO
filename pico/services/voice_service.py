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

from pico.core.errors import EntityNotFoundError, ValidationError
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
    ) -> dict[str, Any]:
        """Deliver the transcribed speech from a completed audio session into a conversation.

        Args:
            session_id: The completed audio capture session identifier.
            conversation_id: The destination conversation identifier.
            db: Active database session.

        Returns:
            Dictionary containing delivery metadata, message ID, and effective classification.

        Raises:
            ValidationError: If the session is uncompleted, expired, already delivered, or has invalid content.
            EntityNotFoundError: If the conversation does not exist.
        """
        session = self.audio_manager.get_session(session_id)
        if not session:
            raise ValidationError(f"Audio session '{session_id}' not found.")

        # Replay and duplicate delivery guard
        if getattr(session, "delivered", False):
            raise ValidationError(f"Audio session '{session_id}' has already been delivered. Replay is rejected.")

        # Expiry guard
        now = utc_now()
        age_seconds = (now - session.started_at).total_seconds()
        if age_seconds > AUDIO_SESSION_TTL_SECONDS:
            raise ValidationError(
                f"Audio session '{session_id}' has expired ({age_seconds:.0f}s > {AUDIO_SESSION_TTL_SECONDS}s)."
            )

        if session.state != AudioCaptureState.COMPLETED:
            raise ValidationError(
                f"Cannot deliver transcript: session '{session_id}' state is '{session.state.value}', not 'completed'."
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

        # Compute effective classification from trusted perception boundary (enforce at least INTERNAL)
        effective_classification = compute_trusted_perception_classification(
            modality=Modality.AUDIO,
            source_device=session.source_device,
        )
        if effective_classification == "PUBLIC":
            effective_classification = "INTERNAL"

        msg_repo = MessageRepository(db)
        message_id = generate_id("msg")
        msg = msg_repo.create(
            message_id=message_id,
            conversation_id=conversation_id,
            role="user",
            content=transcription,
            data_classification=effective_classification,
        )

        # Mark delivered to prevent duplicate replay
        session.delivered = True
        session.delivered_at = now

        return {
            "session_id": session_id,
            "conversation_id": conversation_id,
            "message_id": msg.id,
            "transcription": transcription,
            "data_classification": effective_classification,
            "delivered": True,
            "created_at": msg.created_at.isoformat() if hasattr(msg.created_at, "isoformat") else str(msg.created_at),
        }
