import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Modality(str, Enum):
    AUDIO = "audio"
    VISION = "vision"
    MOTION = "motion"
    ENVIRONMENTAL = "environmental"


class AudioCaptureState(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    CAPTURING = "capturing"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class VisionCaptureState(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    CAPTURING = "capturing"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class MovementDirection(str, Enum):
    STATIONARY = "stationary"
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"
    APPROACHING = "approaching"
    RECEDING = "receding"
    AMBIGUOUS = "ambiguous"


class MovementMagnitude(str, Enum):
    NONE = "none"
    SUBTLE = "subtle"
    MODERATE = "moderate"
    SIGNIFICANT = "significant"


class MovementProgression(str, Enum):
    STARTED = "started"
    CONTINUING = "continuing"
    STOPPED = "stopped"


class RetentionPolicy(str, Enum):
    TRANSIENT_ONLY = "transient_only"
    SESSION_ONLY = "session_only"
    SHORT_TERM_AUDIT = "short_term_audit"
    EPISODIC_ELIGIBLE = "episodic_eligible"


VALID_CLASSIFICATIONS = {"PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"}


class NormalizedPerceptionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"pevt_{uuid.uuid4().hex[:16]}")
    correlation_id: str = Field(default_factory=lambda: f"corr_{uuid.uuid4().hex[:16]}")
    timestamp: datetime = Field(default_factory=utc_now)
    modality: Modality
    event_type: str
    source_device_id: str
    data_classification: str = "INTERNAL"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    raw_payload_scrubbed: bool = True
    structured_data: dict[str, Any] = Field(default_factory=dict)
    provenance: str
    retention_policy: RetentionPolicy = RetentionPolicy.SHORT_TERM_AUDIT
    session_id: str | None = None
    parent_event_ids: list[str] = Field(default_factory=list)

    @field_validator("data_classification")
    @classmethod
    def validate_classification(cls, v: str) -> str:
        upper_v = (v or "").upper().strip()
        if upper_v not in VALID_CLASSIFICATIONS:
            # Invariant: Unknown/invalid classification defaults to INTERNAL (fails closed)
            return "INTERNAL"
        return upper_v


# --- Status Contracts ---
class PerceptionSubsystemStatus(BaseModel):
    overall_ready: bool
    audio: dict[str, Any]
    vision: dict[str, Any]
    motion: dict[str, Any]
    environmental: dict[str, Any]
    timestamp: datetime = Field(default_factory=utc_now)


# --- Audio Requests & Responses ---
class AudioSessionStartRequest(BaseModel):
    user_consent: bool = Field(default=True, description="Explicit user consent required for capture")
    source_device: str = "mic:local"
    metadata: dict[str, Any] | None = None


class AudioSessionStartResponse(BaseModel):
    session_id: str
    state: AudioCaptureState
    microphone_active: bool
    started_at: datetime
    message: str


class AudioSessionStopRequest(BaseModel):
    audio_base64: str | None = Field(default=None, description="Optional audio payload in base64 format")
    audio_format: str = Field(default="wav", description="Audio format (wav, mp3, ogg)")
    client_duration_seconds: float | None = None


class AudioSessionStopResponse(BaseModel):
    session_id: str
    state: AudioCaptureState
    transcription: str | None = None
    confidence: float = 0.0
    duration_seconds: float = 0.0
    event_id: str | None = None
    data_classification: str = "INTERNAL"
    provenance: str


class AudioSynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    voice_id: str | None = None
    speed: float = 1.0


class AudioSynthesisResponse(BaseModel):
    audio_base64: str | None = None
    audio_format: str = "wav"
    duration_seconds: float
    synthesizer_provider: str
    is_simulated: bool = False


# --- Vision Requests & Responses ---
class VisionCaptureRequest(BaseModel):
    user_consent: bool = Field(default=True, description="Explicit user consent required for vision ingestion")
    image_base64: str = Field(description="Base64 encoded image data (PNG, JPEG, BMP, PPM, WebP)")
    image_format: str = Field(default="png", description="Image format")
    source_device: str = "cam:snapshot"
    purpose: str = "user_inspection"
    task_id: str | None = None


class VisionCaptureResponse(BaseModel):
    event_id: str
    source_device: str
    width: int
    height: int
    format: str
    scene_description: str
    ocr_text: str | None = None
    confidence: float
    data_classification: str
    provenance: str
    raw_buffer_retained: bool = False


# --- Motion Requests & Responses ---
class MotionIngestRequest(BaseModel):
    source_device: str = "pir:hallway"
    motion_detected: bool = True
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    measured_duration_ms: int | None = None
    raw_reading: dict[str, Any] | None = None
    data_classification: str = "INTERNAL"


class MovementInterpretationRequest(BaseModel):
    source_device: str = "cam:motion_eval"
    frame_a_base64: str = Field(description="First frame in base64 (PPM, BMP, or PNG)")
    frame_b_base64: str = Field(description="Second frame in base64 (PPM, BMP, or PNG)")
    image_format: str = "ppm"
    sensitivity_threshold: float = Field(default=0.05, ge=0.01, le=0.5)


class MovementInterpretationResponse(BaseModel):
    event_id: str
    motion_detected: bool
    direction: MovementDirection
    magnitude: MovementMagnitude
    progression: MovementProgression
    delta_pixel_ratio: float
    centroid_a: tuple[int, int] | None = None
    centroid_b: tuple[int, int] | None = None
    confidence: float
    evidence: str
    data_classification: str


# --- Environmental Requests & Responses ---
class EnvironmentalIngestRequest(BaseModel):
    source_device: str = Field(description="Sensor source ID (e.g. sensor:esp32:living_room)")
    protocol: str = Field(default="mqtt", description="Protocol source (mqtt, esp32_http, manual)")
    temperature_c: float | None = Field(default=None, ge=-50.0, le=100.0)
    humidity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    ambient_light_lux: float | None = Field(default=None, ge=0.0)
    contact_state: str | None = Field(default=None, description="open or closed")
    air_quality_aqi: int | None = Field(default=None, ge=0, le=500)
    battery_level_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    data_classification: str = "INTERNAL"
    timestamp: datetime = Field(default_factory=utc_now)


# --- Generic Perception Event Responses ---
class PerceptionEventResponse(BaseModel):
    event_id: str
    correlation_id: str
    timestamp: datetime
    modality: Modality
    event_type: str
    source_device_id: str
    data_classification: str
    confidence: float
    structured_data: dict[str, Any]
    provenance: str
    retention_policy: RetentionPolicy
    session_id: str | None = None
    parent_event_ids: list[str] = Field(default_factory=list)


class PerceptionEventListResponse(BaseModel):
    items: list[PerceptionEventResponse]
    total: int
    limit: int
    offset: int


# --- Cross-Modal Correlation Request & Response ---
class CrossModalCorrelationRequest(BaseModel):
    target_event_id: str
    time_window_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    max_correlated_events: int = Field(default=5, ge=1, le=20)


class CrossModalCorrelationResponse(BaseModel):
    correlation_id: str
    target_event_id: str
    correlated_event_ids: list[str]
    time_window_seconds: float
    confidence: float
    synthesized_observation: str
    underlying_modalities: list[str]
    data_classification: str
