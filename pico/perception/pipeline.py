from datetime import datetime, timezone
from typing import Any
from sqlalchemy.orm import Session

from pico.core.config import settings
from pico.core.errors import EntityNotFoundError, ValidationError
from pico.perception.audio.adapter import AudioSessionManager
from pico.perception.contracts import (
    AudioSessionStartRequest,
    AudioSessionStartResponse,
    AudioSessionStopRequest,
    AudioSessionStopResponse,
    AudioSynthesisRequest,
    AudioSynthesisResponse,
    CrossModalCorrelationResponse,
    EnvironmentalIngestRequest,
    MotionIngestRequest,
    MovementInterpretationRequest,
    MovementInterpretationResponse,
    NormalizedPerceptionEvent,
    PerceptionSubsystemStatus,
    VisionCaptureRequest,
    VisionCaptureResponse,
)
from pico.perception.environmental.adapter import EnvironmentalAdapter
from pico.perception.motion.adapter import CrossModalCorrelator, MotionAdapter
from pico.perception.vision.adapter import VisionAdapter
from pico.repositories.perception_event_repo import PerceptionEventRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_shared_audio_manager = AudioSessionManager()
_shared_vision_adapter = VisionAdapter()
_shared_motion_adapter = MotionAdapter()
_shared_environmental_adapter = EnvironmentalAdapter()
_shared_correlator = CrossModalCorrelator()


class PerceptionPipeline:
    """Central perception pipeline orchestrating audio, vision, motion, and environmental adapters."""

    def __init__(
        self,
        db: Session | None = None,
        audio_manager: AudioSessionManager | None = None,
        vision_adapter: VisionAdapter | None = None,
        motion_adapter: MotionAdapter | None = None,
        environmental_adapter: EnvironmentalAdapter | None = None,
        correlator: CrossModalCorrelator | None = None,
    ) -> None:
        self.db = db
        self.repo = PerceptionEventRepository(db) if db is not None else None
        self.audio_manager = audio_manager or _shared_audio_manager
        self.vision_adapter = vision_adapter or _shared_vision_adapter
        self.motion_adapter = motion_adapter or _shared_motion_adapter
        self.environmental_adapter = environmental_adapter or _shared_environmental_adapter
        self.correlator = correlator or _shared_correlator

    def get_status(self) -> PerceptionSubsystemStatus:
        audio_stat = self.audio_manager.get_status()
        vision_stat = self.vision_adapter.get_status()
        motion_stat = self.motion_adapter.get_status()
        env_stat = self.environmental_adapter.get_status()

        overall_ready = settings.PERCEPTION_ENABLED

        return PerceptionSubsystemStatus(
            overall_ready=overall_ready,
            audio=audio_stat,
            vision=vision_stat,
            motion=motion_stat,
            environmental=env_stat,
            timestamp=utc_now(),
        )

    # --- Audio Pipeline ---
    def start_audio_session(self, request: AudioSessionStartRequest) -> AudioSessionStartResponse:
        return self.audio_manager.start_session(
            user_consent=request.user_consent,
            source_device=request.source_device,
            metadata=request.metadata,
        )

    async def stop_audio_session(
        self, session_id: str, request: AudioSessionStopRequest
    ) -> AudioSessionStopResponse:
        response, event = await self.audio_manager.stop_session(
            session_id=session_id,
            audio_base64=request.audio_base64,
            audio_format=request.audio_format,
            client_duration_seconds=request.client_duration_seconds,
        )
        if self.repo:
            self.repo.create(event)
        return response

    def cancel_audio_session(self, session_id: str) -> AudioSessionStartResponse:
        return self.audio_manager.cancel_session(session_id)

    async def synthesize_speech(self, request: AudioSynthesisRequest) -> AudioSynthesisResponse:
        return await self.audio_manager.synthesize_speech(
            text=request.text,
            voice_id=request.voice_id,
            speed=request.speed,
        )

    # --- Vision Pipeline ---
    async def ingest_vision_snapshot(self, request: VisionCaptureRequest) -> VisionCaptureResponse:
        response, event = await self.vision_adapter.ingest_snapshot(request)
        if self.repo:
            self.repo.create(event)
        return response

    # --- Motion Pipeline ---
    def ingest_pir_motion(self, request: MotionIngestRequest) -> NormalizedPerceptionEvent:
        event = self.motion_adapter.ingest_pir_motion(request)
        if self.repo:
            self.repo.create(event)
        return event

    def interpret_movement(
        self, request: MovementInterpretationRequest
    ) -> MovementInterpretationResponse:
        response, event = self.motion_adapter.interpret_movement_between_frames(request)
        if self.repo:
            self.repo.create(event)
        return response

    # --- Environmental Pipeline ---
    def ingest_environmental(self, request: EnvironmentalIngestRequest) -> tuple[NormalizedPerceptionEvent, bool]:
        event, is_duplicate = self.environmental_adapter.ingest_reading(request)
        if self.repo:
            self.repo.create(event)
        return event, is_duplicate

    # --- Cross-Modal Correlation ---
    def correlate_event(
        self, target_event_id: str, time_window_seconds: float = 5.0
    ) -> CrossModalCorrelationResponse:
        if not self.repo:
            raise ValidationError("Database session required for event correlation.")

        target = self.repo.get_by_id(target_event_id)
        if not target:
            raise EntityNotFoundError("PerceptionEvent", target_event_id)

        target_event = NormalizedPerceptionEvent(
            event_id=target.id,
            correlation_id=target.correlation_id,
            timestamp=target.created_at,
            modality=target.modality,
            event_type=target.event_type,
            source_device_id=target.source_device_id,
            data_classification=target.data_classification,
            confidence=target.confidence,
            raw_payload_scrubbed=target.raw_payload_scrubbed,
            structured_data=target.structured_data,
            provenance=target.provenance,
            retention_policy=target.retention_policy,
            session_id=target.session_id,
        )

        recent_db = self.repo.get_recent(seconds=time_window_seconds * 2.0, limit=20)
        candidates = [
            NormalizedPerceptionEvent(
                event_id=r.id,
                correlation_id=r.correlation_id,
                timestamp=r.created_at,
                modality=r.modality,
                event_type=r.event_type,
                source_device_id=r.source_device_id,
                data_classification=r.data_classification,
                confidence=r.confidence,
                raw_payload_scrubbed=r.raw_payload_scrubbed,
                structured_data=r.structured_data,
                provenance=r.provenance,
                retention_policy=r.retention_policy,
                session_id=r.session_id,
            )
            for r in recent_db
            if r.id != target_event_id
        ]

        fused = self.correlator.correlate(
            target_event=target_event,
            candidates=candidates,
            time_window_seconds=time_window_seconds,
        )

        if not fused:
            return CrossModalCorrelationResponse(
                correlation_id=target.correlation_id,
                target_event_id=target.id,
                correlated_event_ids=[],
                time_window_seconds=time_window_seconds,
                confidence=target.confidence,
                synthesized_observation=f"No co-occurring perception events found within {time_window_seconds}s.",
                underlying_modalities=[target.modality],
                data_classification=target.data_classification,
            )

        # Save synthesized event
        self.repo.create(fused)

        return CrossModalCorrelationResponse(
            correlation_id=fused.correlation_id,
            target_event_id=target_event_id,
            correlated_event_ids=fused.structured_data.get("correlated_event_ids", []),
            time_window_seconds=time_window_seconds,
            confidence=fused.confidence,
            synthesized_observation=fused.structured_data.get("synthesized_observation", ""),
            underlying_modalities=fused.structured_data.get("underlying_modalities", []),
            data_classification=fused.data_classification,
        )
