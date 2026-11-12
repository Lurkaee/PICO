"""Voice service coordinating local audio capture, transcription, and conversation delivery (Milestone P11.3).

Architectural Invariants:
1. Voice input is a presentation modality into PICO's standard conversation pipeline; it NEVER
   bypasses context assembly, model scheduling, capability allowlists, or human approval.
2. Raw audio buffers are never stored on disk, never logged, and never sent to cloud providers.
3. Transcripts inherit trusted perception classification (default INTERNAL) and fail closed
   against cloud routing unless explicitly PUBLIC and cloud-enabled.
"""

from __future__ import annotations

from typing import Any
from sqlalchemy.orm import Session

from pico.core.errors import EntityNotFoundError, ValidationError
from pico.core.id_gen import generate_id
from pico.perception.audio.adapter import AudioSessionManager
from pico.perception.classification import compute_trusted_perception_classification
from pico.perception.contracts import AudioCaptureState, Modality
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository


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
            ValidationError: If the audio session is not completed or has an empty transcription.
            EntityNotFoundError: If the conversation does not exist.
        """
        session = self.audio_manager.get_session(session_id)
        if not session:
            raise ValidationError(f"Audio session '{session_id}' not found.")

        if session.state != AudioCaptureState.COMPLETED:
            raise ValidationError(
                f"Cannot deliver transcript: session '{session_id}' is in state '{session.state.value}', not 'completed'."
            )

        if not session.transcription or not session.transcription.strip():
            raise ValidationError(
                f"Audio session '{session_id}' has no transcription text to deliver."
            )

        conv_repo = ConversationRepository(db)
        conversation = conv_repo.get_by_id(conversation_id)
        if not conversation:
            raise EntityNotFoundError("Conversation", conversation_id)

        # Compute effective classification from trusted perception boundary
        effective_classification = compute_trusted_perception_classification(
            modality=Modality.AUDIO,
            source_device=session.source_device,
        )

        msg_repo = MessageRepository(db)
        message_id = generate_id("msg")
        msg = msg_repo.create(
            message_id=message_id,
            conversation_id=conversation_id,
            role="user",
            content=session.transcription.strip(),
            data_classification=effective_classification,
        )

        return {
            "session_id": session_id,
            "conversation_id": conversation_id,
            "message_id": msg.id,
            "transcription": session.transcription.strip(),
            "data_classification": effective_classification,
            "delivered": True,
            "created_at": msg.created_at.isoformat() if hasattr(msg.created_at, "isoformat") else str(msg.created_at),
        }
