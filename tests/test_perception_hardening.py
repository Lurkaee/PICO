import base64
import struct
import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.perception.audio.adapter import (
    AudioSessionManager,
    LocalTextToSpeechAdapter,
    LocalWhisperSpeechToTextAdapter,
    MockSpeechToTextAdapter,
)
from pico.perception.classification import (
    compute_trusted_perception_classification,
    validate_trusted_source_device,
)
from pico.perception.context import format_perception_context_items
from pico.perception.contracts import (
    AudioCaptureState,
    AudioSessionStartRequest,
    EnvironmentalIngestRequest,
    Modality,
    MotionIngestRequest,
    MovementInterpretationRequest,
    NormalizedPerceptionEvent,
    VisionCaptureRequest,
)
from pico.perception.environmental.adapter import EnvironmentalAdapter
from pico.perception.motion.adapter import MotionAdapter, decode_frame_grayscale
from pico.perception.pipeline import PerceptionPipeline
from pico.perception.vision.adapter import VisionAdapter
from pico.repositories.perception_event_repo import PerceptionEventRepository
from pico.schemas.context import ContextItem
from pico.services.context_compiler import ContextCompiler
from pico.services.routing_service import RoutingService
from tests.test_perception_motion import create_ppm_frame
from tests.test_perception_vision import make_dummy_png


def create_uncompressed_bmp(width: int, height: int, active_rect: tuple[int, int, int, int] | None = None) -> bytes:
    """Create a minimal valid 24-bit uncompressed BMP image in bytes."""
    row_stride = (width * 3 + 3) & ~3
    image_size = row_stride * height
    offset = 54
    file_size = offset + image_size

    # BMP File Header (14 bytes)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, offset)
    # DIB Header - BITMAPINFOHEADER (40 bytes)
    dib = struct.pack("<IIIHHIIIIII", 40, width, height, 1, 24, 0, image_size, 2835, 2835, 0, 0)

    # Pixel rows (bottom-to-top)
    rows = bytearray(image_size)
    if active_rect:
        rx, ry, rw, rh = active_rect
        for y in range(ry, min(height, ry + rh)):
            row_start = y * row_stride
            for x in range(rx, min(width, rx + rw)):
                px = row_start + x * 3
                rows[px] = 255      # B
                rows[px + 1] = 255  # G
                rows[px + 2] = 255  # R

    return header + dib + bytes(rows)


# --- 1. Explicit Capture Consent Tests ---

def test_consent_omitted_fails_validation() -> None:
    # AudioSessionStartRequest: omitting user_consent raises PydanticValidationError
    with pytest.raises(PydanticValidationError):
        AudioSessionStartRequest()  # type: ignore[call-arg]

    # VisionCaptureRequest: omitting user_consent raises PydanticValidationError
    with pytest.raises(PydanticValidationError):
        VisionCaptureRequest(image_base64="AAAA")  # type: ignore[call-arg]


def test_consent_false_fails_closed() -> None:
    mgr = AudioSessionManager()
    with pytest.raises(AuthorizationError) as exc_aud:
        mgr.start_session(user_consent=False)
    assert "mandatory" in str(exc_aud.value).lower()


@pytest.mark.anyio
async def test_vision_consent_false_fails_closed() -> None:
    vision = VisionAdapter()
    req = VisionCaptureRequest(
        user_consent=False,
        image_base64=base64.b64encode(make_dummy_png(64, 64)).decode("ascii"),
    )
    with pytest.raises(AuthorizationError):
        await vision.ingest_snapshot(req)


def test_consent_true_reports_honest_mic_active_false() -> None:
    mgr = AudioSessionManager()
    res = mgr.start_session(user_consent=True, source_device="mic:test")
    assert res.state == AudioCaptureState.CAPTURING
    # Invariant: Physical mic is not operating
    assert res.microphone_active is False
    assert mgr.get_status()["microphone_active"] is False


# --- 2. Trusted Classification & Adversarial Bypass Tests ---

def test_caller_cannot_downgrade_motion_to_public() -> None:
    adapter = MotionAdapter()
    req = MotionIngestRequest(
        source_device="pir:front_hallway",
        motion_detected=True,
        data_classification="PUBLIC",  # Adversarial attempt to downgrade
    )
    event = adapter.ingest_pir_motion(req)
    # Must fail closed / clamp to INTERNAL
    assert event.data_classification == "INTERNAL"


def test_caller_cannot_downgrade_environmental_to_public() -> None:
    adapter = EnvironmentalAdapter()
    req = EnvironmentalIngestRequest(
        source_device="sensor:balcony",
        temperature_c=20.0,
        data_classification="PUBLIC",  # Adversarial attempt to downgrade
    )
    event, _ = adapter.ingest_reading(req)
    assert event.data_classification == "INTERNAL"


def test_invalid_classification_defaults_to_internal() -> None:
    cl = compute_trusted_perception_classification(
        modality=Modality.ENVIRONMENTAL,
        source_device="sensor:temp",
        caller_classification="TOP_SECRET_UNKNOWN",
    )
    assert cl == "INTERNAL"


def test_untrusted_source_device_fails_closed() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_trusted_source_device(Modality.MOTION, "untrusted:pir_fake")
    assert "untrusted" in str(exc.value).lower()

    with pytest.raises(ValidationError):
        validate_trusted_source_device(Modality.ENVIRONMENTAL, "hacker:sensor")

    with pytest.raises(ValidationError):
        validate_trusted_source_device(Modality.AUDIO, "")


def test_perception_events_pin_routing_to_local_privacy_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Build perception event where caller attempted PUBLIC
    ev = NormalizedPerceptionEvent(
        event_id="pevt_adversarial",
        modality=Modality.MOTION,
        event_type="motion_detected",
        source_device_id="pir:hallway",
        data_classification="INTERNAL",  # Guaranteed INTERNAL by trusted ingestion
        confidence=0.9,
        provenance="sensor:pir:hallway",
        structured_data={"motion_detected": True},
    )
    items = format_perception_context_items([ev])
    compiler = ContextCompiler()
    compiled = compiler.compile(items=items)

    assert compiled.effective_classification == "INTERNAL"

    # Verify central router pins to local and forbids cloud
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "ollama")
    router = RoutingService()
    decision = router.resolve_route(
        task_category="general_chat",
        data_classification=compiled.effective_classification,
        cloud_approved=True,  # Even with cloud approved!
    )
    assert decision.selected_profile == "local-small"
    assert decision.provider == "ollama"
    assert decision.decision_rule == "RULE_PRIVATE_LOCAL_PIN"

    # Explicit cloud request must fail closed
    with pytest.raises(ValidationError) as exc:
        router.resolve_route(
            task_category="reasoning",
            data_classification=compiled.effective_classification,
            requested_profile="cloud-strong",
            cloud_approved=True,
        )
    assert "not eligible for cloud processing" in str(exc.value).lower()


# --- 3. Honest Audio Capability & Fake Fallback Tests ---

def test_local_whisper_and_tts_report_unavailable() -> None:
    whisper = LocalWhisperSpeechToTextAdapter()
    tts = LocalTextToSpeechAdapter()
    assert whisper.is_available() is False
    assert "unavailable" in whisper.get_provider_name()
    assert tts.is_available() is False
    assert "unavailable" in tts.get_provider_name()


@pytest.mark.anyio
async def test_absent_audio_payload_fails_closed_zero_fake_audio() -> None:
    mgr = AudioSessionManager()
    start = mgr.start_session(user_consent=True, source_device="mic:local")

    # Stop without audio payload must fail closed, NOT synthesize fake audio
    with pytest.raises(ValidationError) as exc:
        await mgr.stop_session(session_id=start.session_id, audio_base64=None)
    assert "payload is empty or missing" in str(exc.value).lower()


# --- 4. Honest Vision Behavior Tests ---

@pytest.mark.anyio
async def test_vision_metadata_only_behavior() -> None:
    vision = VisionAdapter()
    png_bytes = make_dummy_png(100, 100)
    req = VisionCaptureRequest(
        user_consent=True,
        image_base64=base64.b64encode(png_bytes).decode("ascii"),
        source_device="cam:webcam0",
    )
    res, ev = await vision.ingest_snapshot(req)

    # Renamed to visual_snapshot_ingested (not analyzed)
    assert ev.event_type == "visual_snapshot_ingested"
    assert res.confidence == 0.0
    assert ev.confidence == 0.0
    assert ev.structured_data["metadata_only"] is True
    assert ev.structured_data["scene_analysis_performed"] is False
    assert "unavailable" in ev.structured_data["scene_description"].lower()


# --- 5. Movement Input Parsing & Unsupported Format Tests ---

def test_frame_differencing_unsupported_format_rejected() -> None:
    adapter = MotionAdapter()
    dummy = base64.b64encode(b"PNG_FAKE_BYTES").decode("ascii")
    req = MovementInterpretationRequest(
        source_device="cam:test",
        frame_a_base64=dummy,
        frame_b_base64=dummy,
        image_format="png",  # Direct differencing on PNG is unsupported without PIL
    )
    with pytest.raises(ValidationError) as exc:
        adapter.interpret_movement_between_frames(req)
    assert "unsupported format 'png'" in str(exc.value).lower()


def test_frame_differencing_malformed_ppm_rejected() -> None:
    with pytest.raises(ValidationError):
        decode_frame_grayscale(b"P6\n20\n", "ppm")  # Truncated header

    with pytest.raises(ValidationError):
        decode_frame_grayscale(b"P6\n20 20\n255\n" + b"\x00" * 5, "ppm")  # Truncated pixel data


def test_frame_differencing_uncompressed_bmp_supported() -> None:
    adapter = MotionAdapter()
    bmp_a = create_uncompressed_bmp(60, 60, active_rect=(5, 20, 15, 15))
    bmp_b = create_uncompressed_bmp(60, 60, active_rect=(35, 20, 15, 15))
    b64_a = base64.b64encode(bmp_a).decode("ascii")
    b64_b = base64.b64encode(bmp_b).decode("ascii")

    req = MovementInterpretationRequest(
        source_device="cam:test_bmp",
        frame_a_base64=b64_a,
        frame_b_base64=b64_b,
        image_format="bmp",
        sensitivity_threshold=0.03,
    )
    res, ev = adapter.interpret_movement_between_frames(req)
    assert res.motion_detected is True
    assert res.direction.value == "right"


def test_frame_differencing_oversized_frame_rejected() -> None:
    oversized = b"P6\n10 10\n255\n" + b"\x00" * (5 * 1024 * 1024)
    with pytest.raises(ValidationError) as exc:
        decode_frame_grayscale(oversized, "ppm")
    assert "exceeds maximum allowed size" in str(exc.value).lower()


# --- 6. Deduplication, Cooldown & Real Hardware Status Tests ---

def test_motion_cooldown_suppression_and_no_persistence(db_session: Session) -> None:
    pipeline = PerceptionPipeline(db=db_session)
    repo = PerceptionEventRepository(db_session)

    req1 = MotionIngestRequest(source_device="pir:driveway", motion_detected=True)
    ev1 = pipeline.ingest_pir_motion(req1)
    assert ev1.structured_data["cooldown_suppressed"] is False
    assert repo.get_by_id(ev1.event_id) is not None

    # Immediate second motion from same device within cooldown
    req2 = MotionIngestRequest(source_device="pir:driveway", motion_detected=True)
    ev2 = pipeline.ingest_pir_motion(req2)
    assert ev2.structured_data["cooldown_suppressed"] is True
    assert ev2.structured_data["cooldown_seconds_remaining"] > 0
    # Must NOT be persisted to DB
    assert repo.get_by_id(ev2.event_id) is None


def test_environmental_deduplication_no_persistence(db_session: Session) -> None:
    pipeline = PerceptionPipeline(db=db_session)
    repo = PerceptionEventRepository(db_session)

    req1 = EnvironmentalIngestRequest(
        source_device="sensor:office",
        temperature_c=22.0,
        humidity_percent=50.0,
    )
    ev1, is_dup1 = pipeline.ingest_environmental(req1)
    assert is_dup1 is False
    assert repo.get_by_id(ev1.event_id) is not None

    # Immediate identical reading: suppressed from persistence
    req2 = EnvironmentalIngestRequest(
        source_device="sensor:office",
        temperature_c=22.0,
        humidity_percent=50.0,
    )
    ev2, is_dup2 = pipeline.ingest_environmental(req2)
    assert is_dup2 is True
    assert ev2.structured_data["is_deduplicated"] is True
    assert repo.get_by_id(ev2.event_id) is None


def test_honest_physical_hardware_status() -> None:
    pipeline = PerceptionPipeline()
    status = pipeline.get_status()

    # Audio
    assert status.audio["physical_microphone_connected"] is False
    assert status.audio["microphone_active"] is False

    # Vision
    assert status.vision["physical_camera_connected"] is False
    assert status.vision["camera_available"] is False

    # Motion
    assert status.motion["physical_pir_connected"] is False
    assert status.motion["pir_available"] is False

    # Environmental
    assert status.environmental["physical_sensors_connected"] is False
    assert status.environmental["esp32_gateway_connected"] is False
    assert status.environmental["mqtt_connected"] is False
