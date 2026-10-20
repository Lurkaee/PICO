import base64
from datetime import datetime, timedelta, timezone
from pico.perception.contracts import (
    Modality,
    MotionIngestRequest,
    MovementDirection,
    MovementInterpretationRequest,
    MovementMagnitude,
    MovementProgression,
    NormalizedPerceptionEvent,
    RetentionPolicy,
)
from pico.perception.motion.adapter import CrossModalCorrelator, MotionAdapter


def create_ppm_frame(width: int, height: int, active_rect: tuple[int, int, int, int] | None = None) -> bytes:
    """Create a simple PPM image with background and an optional bright rectangle."""
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    pixels = bytearray(width * height * 3)

    if active_rect:
        rx, ry, rw, rh = active_rect
        for y in range(ry, min(height, ry + rh)):
            for x in range(rx, min(width, rx + rw)):
                idx = (y * width + x) * 3
                pixels[idx] = 255      # R
                pixels[idx + 1] = 255  # G
                pixels[idx + 2] = 255  # B

    return header + bytes(pixels)


def test_tier1_pir_motion_detection() -> None:
    adapter = MotionAdapter()
    req = MotionIngestRequest(
        source_device="pir:front_hallway",
        motion_detected=True,
        confidence=0.98,
        measured_duration_ms=1200,
    )
    event = adapter.ingest_pir_motion(req)

    assert event.modality == Modality.MOTION
    assert event.event_type == "motion_detected"
    assert event.source_device_id == "pir:front_hallway"
    assert event.confidence == 0.98
    assert event.data_classification == "INTERNAL"

    # Invariant: PIR trigger detects presence but does not assert direction or identity
    assert "direction" not in event.structured_data
    assert "identity" not in event.structured_data
    assert "PIR detects motion presence only" in event.structured_data["assertion_limits"]


def test_tier2_frame_differencing_movement_interpretation() -> None:
    adapter = MotionAdapter()
    width, height = 100, 100

    # 1. Identical static frames: No motion
    frame_blank = create_ppm_frame(width, height, None)
    b64_blank = base64.b64encode(frame_blank).decode("ascii")

    req_static = MovementInterpretationRequest(
        source_device="cam:study",
        frame_a_base64=b64_blank,
        frame_b_base64=b64_blank,
    )
    res_static, ev_static = adapter.interpret_movement_between_frames(req_static)
    assert res_static.motion_detected is False
    assert res_static.direction == MovementDirection.STATIONARY
    assert res_static.magnitude == MovementMagnitude.NONE
    assert res_static.progression == MovementProgression.STOPPED
    assert res_static.delta_pixel_ratio == 0.0

    # 2. Frame B has an object moved to the RIGHT:
    # Frame A has object at (10, 40, 20, 20), Frame B has object at (50, 40, 20, 20)
    frame_a = create_ppm_frame(width, height, active_rect=(10, 40, 20, 20))
    frame_b = create_ppm_frame(width, height, active_rect=(50, 40, 20, 20))

    b64_a = base64.b64encode(frame_a).decode("ascii")
    b64_b = base64.b64encode(frame_b).decode("ascii")

    req_moving_right = MovementInterpretationRequest(
        source_device="cam:study",
        frame_a_base64=b64_a,
        frame_b_base64=b64_b,
        sensitivity_threshold=0.03,
    )
    res_right, ev_right = adapter.interpret_movement_between_frames(req_moving_right)
    assert res_right.motion_detected is True
    assert res_right.direction == MovementDirection.RIGHT
    assert res_right.magnitude in (MovementMagnitude.SUBTLE, MovementMagnitude.MODERATE)
    assert res_right.progression == MovementProgression.CONTINUING
    assert res_right.delta_pixel_ratio > 0.03

    # 3. Frame B has object moved DOWN:
    # Frame A has object at (40, 10, 20, 20), Frame B has object at (40, 60, 20, 20)
    frame_a_down = create_ppm_frame(width, height, active_rect=(40, 10, 20, 20))
    frame_b_down = create_ppm_frame(width, height, active_rect=(40, 60, 20, 20))
    b64_ad = base64.b64encode(frame_a_down).decode("ascii")
    b64_bd = base64.b64encode(frame_b_down).decode("ascii")

    req_moving_down = MovementInterpretationRequest(
        source_device="cam:study",
        frame_a_base64=b64_ad,
        frame_b_base64=b64_bd,
        sensitivity_threshold=0.03,
    )
    res_down, ev_down = adapter.interpret_movement_between_frames(req_moving_down)
    assert res_down.motion_detected is True
    assert res_down.direction == MovementDirection.DOWN


def test_tier3_cross_modal_correlation() -> None:
    correlator = CrossModalCorrelator()
    now = datetime.now(timezone.utc)

    # Motion event
    target_event = NormalizedPerceptionEvent(
        event_id="pevt_target_motion",
        correlation_id="corr_test_1",
        timestamp=now,
        modality=Modality.MOTION,
        event_type="motion_detected",
        source_device_id="pir:front_door",
        confidence=0.95,
        structured_data={"motion_detected": True},
        provenance="sensor:pir:front_door",
    )

    # Co-occurring environmental contact sensor event (door open) 1.5 seconds earlier
    candidate_door = NormalizedPerceptionEvent(
        event_id="pevt_door_opened",
        correlation_id="corr_test_2",
        timestamp=now - timedelta(seconds=1.5),
        modality=Modality.ENVIRONMENTAL,
        event_type="environmental_reading",
        source_device_id="sensor:front_door_contact",
        confidence=1.0,
        structured_data={"readings": {"contact_state": "open"}},
        provenance="sensor:esp32:front_door_contact",
    )

    # Distant unrelated event (30 seconds ago) outside 5s window
    candidate_old = NormalizedPerceptionEvent(
        event_id="pevt_old_temp",
        correlation_id="corr_test_3",
        timestamp=now - timedelta(seconds=35.0),
        modality=Modality.ENVIRONMENTAL,
        event_type="environmental_reading",
        source_device_id="sensor:bedroom_temp",
        confidence=1.0,
        structured_data={"readings": {"temperature_c": 21.0}},
        provenance="sensor:bedroom_temp",
    )

    fused = correlator.correlate(
        target_event=target_event,
        candidates=[candidate_door, candidate_old],
        time_window_seconds=5.0,
    )

    assert fused is not None
    assert fused.event_type == "cross_modal_correlation"
    assert "pevt_target_motion" in fused.parent_event_ids
    assert "pevt_door_opened" in fused.parent_event_ids
    assert "pevt_old_temp" not in fused.parent_event_ids
    assert "motion" in fused.structured_data["underlying_modalities"]
    assert "environmental" in fused.structured_data["underlying_modalities"]
    assert "observational inference" in fused.structured_data["uncertainty_label"]
