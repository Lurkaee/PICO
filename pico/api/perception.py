from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from pico.api.operator import AuthorizationContext, get_authorization_context, is_loopback_client
from pico.core.errors import AuthorizationError, EntityNotFoundError
from pico.db.session import get_db
from pico.perception.contracts import (
    AudioSessionStartRequest,
    AudioSessionStartResponse,
    AudioSessionStatusResponse,
    AudioSessionStopRequest,
    AudioSessionStopResponse,
    AudioSynthesisRequest,
    AudioSynthesisResponse,
    CrossModalCorrelationRequest,
    CrossModalCorrelationResponse,
    EnvironmentalIngestRequest,
    MotionIngestRequest,
    MovementInterpretationRequest,
    MovementInterpretationResponse,
    PerceptionEventListResponse,
    PerceptionEventResponse,
    PerceptionSubsystemStatus,
    VisionCaptureRequest,
    VisionCaptureResponse,
)
from pico.perception.pipeline import PerceptionPipeline
from pico.repositories.perception_event_repo import PerceptionEventRepository


class AudioDeliverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=1, max_length=128)


class AudioDeliverResponse(BaseModel):
    session_id: str
    conversation_id: str
    message_id: str
    transcription: str
    data_classification: str
    delivered: bool
    created_at: str


router = APIRouter(prefix="/api/perception", tags=["Multimodal Perception"])


@router.get(
    "/status",
    response_model=PerceptionSubsystemStatus,
    summary="Get overall perception subsystem readiness and adapter availability",
)
def get_perception_status(db: Session = Depends(get_db)) -> PerceptionSubsystemStatus:
    pipeline = PerceptionPipeline(db=db)
    return pipeline.get_status()


# --- Audio Endpoints ---
@router.post(
    "/audio/session/start",
    response_model=AudioSessionStartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start push-to-talk audio recording session",
)
def start_audio_session(
    payload: AudioSessionStartRequest,
    request: Request,
    auth_ctx: AuthorizationContext = Depends(get_authorization_context),
    db: Session = Depends(get_db),
) -> AudioSessionStartResponse:
    if not is_loopback_client(request):
        raise AuthorizationError("Audio session start is strictly restricted to local loopback connections.")

    caller_operator_id: str | None = None
    if auth_ctx.is_authorized:
        caller_operator_id = (
            auth_ctx.session.approver
            if (auth_ctx.session and auth_ctx.session.approver)
            else auth_ctx.principal
        )
    elif is_loopback_client(request):
        caller_operator_id = "local_operator"

    if payload.conversation_id:
        from pico.repositories.conversation_repo import ConversationRepository

        conv_repo = ConversationRepository(db)
        if not conv_repo.get_by_id(payload.conversation_id):
            raise EntityNotFoundError("Conversation", payload.conversation_id)

    pipeline = PerceptionPipeline(db=db)
    return pipeline.start_audio_session(
        payload,
        operator_id=caller_operator_id,
        conversation_id=payload.conversation_id,
    )


@router.post(
    "/audio/session/{session_id}/stop",
    response_model=AudioSessionStopResponse,
    summary="Stop push-to-talk audio session and transcribe speech",
)
async def stop_audio_session(
    session_id: str,
    payload: AudioSessionStopRequest,
    db: Session = Depends(get_db),
) -> AudioSessionStopResponse:
    pipeline = PerceptionPipeline(db=db)
    return await pipeline.stop_audio_session(session_id, payload)


@router.post(
    "/audio/session/{session_id}/cancel",
    response_model=AudioSessionStartResponse,
    summary="Cancel active audio recording session",
)
def cancel_audio_session(
    session_id: str,
    db: Session = Depends(get_db),
) -> AudioSessionStartResponse:
    pipeline = PerceptionPipeline(db=db)
    return pipeline.cancel_audio_session(session_id)


@router.get(
    "/audio/session/{session_id}",
    response_model=AudioSessionStatusResponse,
    summary="Get audio session lifecycle status (read-only)",
)
def get_audio_session_status(
    session_id: str,
    db: Session = Depends(get_db),
) -> AudioSessionStatusResponse:
    pipeline = PerceptionPipeline(db=db)
    return pipeline.get_audio_session_status(session_id)


@router.post(
    "/audio/session/{session_id}/deliver",
    response_model=AudioDeliverResponse,
    summary="Deliver transcribed speech from completed audio session into conversation message history",
)
def deliver_audio_transcript(
    session_id: str,
    payload: AudioDeliverRequest,
    request: Request,
    auth_ctx: AuthorizationContext = Depends(get_authorization_context),
    db: Session = Depends(get_db),
) -> AudioDeliverResponse:
    if not is_loopback_client(request):
        raise AuthorizationError("Voice transcript delivery is strictly restricted to local loopback connections.")

    if not auth_ctx.is_authorized:
        raise AuthorizationError(
            "Voice transcript delivery requires an authenticated local operator, bridge proof, or approval token."
        )

    caller_operator_id = (
        auth_ctx.session.approver
        if (auth_ctx.session and auth_ctx.session.approver)
        else auth_ctx.principal
    )

    from pico.services.voice_service import VoiceService

    voice_svc = VoiceService()
    result = voice_svc.deliver_transcript_to_conversation(
        session_id=session_id,
        conversation_id=payload.conversation_id,
        db=db,
        caller_operator_id=caller_operator_id,
    )
    return AudioDeliverResponse(**result)


@router.post(
    "/audio/synthesize",
    response_model=AudioSynthesisResponse,
    summary="Synthesize spoken audio from text using local TTS adapter",
)
async def synthesize_speech(
    payload: AudioSynthesisRequest,
    db: Session = Depends(get_db),
) -> AudioSynthesisResponse:
    pipeline = PerceptionPipeline(db=db)
    return await pipeline.synthesize_speech(payload)


# --- Vision Endpoints ---
@router.post(
    "/vision/capture",
    response_model=VisionCaptureResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest on-demand visual snapshot (camera or screenshot)",
)
async def ingest_vision_snapshot(
    payload: VisionCaptureRequest,
    db: Session = Depends(get_db),
) -> VisionCaptureResponse:
    pipeline = PerceptionPipeline(db=db)
    return await pipeline.ingest_vision_snapshot(payload)


# --- Motion Endpoints ---
@router.post(
    "/motion/ingest",
    response_model=PerceptionEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest PIR motion sensor trigger signal (Tier 1 Motion Detection)",
)
def ingest_pir_motion(
    payload: MotionIngestRequest,
    db: Session = Depends(get_db),
) -> PerceptionEventResponse:
    pipeline = PerceptionPipeline(db=db)
    event = pipeline.ingest_pir_motion(payload)
    return PerceptionEventResponse(
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        timestamp=event.timestamp,
        modality=event.modality,
        event_type=event.event_type,
        source_device_id=event.source_device_id,
        data_classification=event.data_classification,
        confidence=event.confidence,
        structured_data=event.structured_data,
        provenance=event.provenance,
        retention_policy=event.retention_policy,
        session_id=event.session_id,
        parent_event_ids=event.parent_event_ids,
    )


@router.post(
    "/motion/interpret",
    response_model=MovementInterpretationResponse,
    summary="Interpret movement direction and magnitude between frames (Tier 2 Movement Interpretation)",
)
def interpret_movement(
    payload: MovementInterpretationRequest,
    db: Session = Depends(get_db),
) -> MovementInterpretationResponse:
    pipeline = PerceptionPipeline(db=db)
    return pipeline.interpret_movement(payload)


# --- Environmental Endpoints ---
@router.post(
    "/environmental/ingest",
    response_model=PerceptionEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest environmental telemetry reading (MQTT / ESP32 / Sensor)",
)
def ingest_environmental(
    payload: EnvironmentalIngestRequest,
    db: Session = Depends(get_db),
) -> PerceptionEventResponse:
    pipeline = PerceptionPipeline(db=db)
    event, _ = pipeline.ingest_environmental(payload)
    return PerceptionEventResponse(
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        timestamp=event.timestamp,
        modality=event.modality,
        event_type=event.event_type,
        source_device_id=event.source_device_id,
        data_classification=event.data_classification,
        confidence=event.confidence,
        structured_data=event.structured_data,
        provenance=event.provenance,
        retention_policy=event.retention_policy,
        session_id=event.session_id,
        parent_event_ids=event.parent_event_ids,
    )


# --- Event Trace & Cross-Modal Correlation ---
@router.get(
    "/events",
    response_model=PerceptionEventListResponse,
    summary="Query recent normalized perception events across all modalities",
)
def list_perception_events(
    modality: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    source_device_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> PerceptionEventListResponse:
    repo = PerceptionEventRepository(db)
    items = repo.list_events(
        modality=modality,
        event_type=event_type,
        source_device_id=source_device_id,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    total = repo.count(
        modality=modality,
        event_type=event_type,
        source_device_id=source_device_id,
        session_id=session_id,
    )
    res_items = [
        PerceptionEventResponse(
            event_id=item.id,
            correlation_id=item.correlation_id,
            timestamp=item.created_at,
            modality=item.modality,
            event_type=item.event_type,
            source_device_id=item.source_device_id,
            data_classification=item.data_classification,
            confidence=item.confidence,
            structured_data=item.structured_data,
            provenance=item.provenance,
            retention_policy=item.retention_policy,
            session_id=item.session_id,
            parent_event_ids=item.parent_event_ids or [],
        )
        for item in items
    ]
    return PerceptionEventListResponse(items=res_items, total=total, limit=limit, offset=offset)


@router.post(
    "/correlate",
    response_model=CrossModalCorrelationResponse,
    summary="Correlate target perception event with other modalities within a temporal window (Tier 3 Cross-Modal)",
)
def correlate_events(
    payload: CrossModalCorrelationRequest,
    db: Session = Depends(get_db),
) -> CrossModalCorrelationResponse:
    pipeline = PerceptionPipeline(db=db)
    return pipeline.correlate_event(
        target_event_id=payload.target_event_id,
        time_window_seconds=payload.time_window_seconds,
    )
