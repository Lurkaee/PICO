import base64
from fastapi.testclient import TestClient
from tests.test_perception_motion import create_ppm_frame
from tests.test_perception_vision import make_dummy_png


def test_api_perception_status(client: TestClient) -> None:
    res = client.get("/api/perception/status")
    assert res.status_code == 200
    data = res.json()
    assert "overall_ready" in data
    assert "audio" in data
    assert "vision" in data
    assert "motion" in data
    assert "environmental" in data
    assert data["audio"]["microphone_active"] is False


def test_api_audio_push_to_talk_and_synthesis(client: TestClient) -> None:
    # 1. Start audio session
    start_res = client.post(
        "/api/perception/audio/session/start",
        json={"user_consent": True, "source_device": "mic:desktop"},
    )
    assert start_res.status_code == 201
    start_data = start_res.json()
    session_id = start_data["session_id"]
    assert start_data["microphone_active"] is False

    # 2. Stop audio session with audio payload
    audio_bytes = b"TEXT:Turn off the kitchen lights later."
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    stop_res = client.post(
        f"/api/perception/audio/session/{session_id}/stop",
        json={"audio_base64": audio_b64, "audio_format": "wav"},
    )
    assert stop_res.status_code == 200
    stop_data = stop_res.json()
    assert stop_data["transcription"] == "Turn off the kitchen lights later."
    assert stop_data["confidence"] >= 0.9

    # 3. Cancel a new session
    s2 = client.post("/api/perception/audio/session/start", json={"user_consent": True}).json()
    cancel_res = client.post(f"/api/perception/audio/session/{s2['session_id']}/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["state"] == "cancelled"

    # 4. Synthesize speech
    synth_res = client.post(
        "/api/perception/audio/synthesize",
        json={"text": "Acknowledged. Command logged."},
    )
    assert synth_res.status_code == 200
    synth_data = synth_res.json()
    assert synth_data["audio_base64"] is not None
    assert synth_data["duration_seconds"] > 0


def test_api_vision_capture(client: TestClient) -> None:
    png_bytes = make_dummy_png(240, 160)
    png_b64 = base64.b64encode(png_bytes).decode("ascii")

    res = client.post(
        "/api/perception/vision/capture",
        json={
            "user_consent": True,
            "image_base64": png_b64,
            "image_format": "png",
            "source_device": "cam:desk_cam",
            "purpose": "workspace_check",
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["width"] == 240
    assert data["height"] == 160
    assert data["format"] == "png"
    assert data["data_classification"] == "INTERNAL"
    assert "desk_cam" in data["source_device"]


def test_api_motion_and_movement_interpretation(client: TestClient) -> None:
    # 1. PIR ingest
    pir_res = client.post(
        "/api/perception/motion/ingest",
        json={
            "source_device": "pir:hallway",
            "motion_detected": True,
            "confidence": 0.95,
        },
    )
    assert pir_res.status_code == 201
    pir_data = pir_res.json()
    assert pir_data["event_type"] == "motion_detected"
    assert pir_data["source_device_id"] == "pir:hallway"

    # 2. Movement interpretation between two frames
    f_a = create_ppm_frame(80, 80, active_rect=(10, 30, 20, 20))
    f_b = create_ppm_frame(80, 80, active_rect=(40, 30, 20, 20))
    b64_a = base64.b64encode(f_a).decode("ascii")
    b64_b = base64.b64encode(f_b).decode("ascii")

    interp_res = client.post(
        "/api/perception/motion/interpret",
        json={
            "source_device": "cam:corridor",
            "frame_a_base64": b64_a,
            "frame_b_base64": b64_b,
            "sensitivity_threshold": 0.03,
        },
    )
    assert interp_res.status_code == 200
    interp_data = interp_res.json()
    assert interp_data["motion_detected"] is True
    assert interp_data["direction"] == "right"
    assert interp_data["confidence"] > 0


def test_api_environmental_ingest_and_list_events(client: TestClient) -> None:
    # 1. Ingest environmental reading
    env_res = client.post(
        "/api/perception/environmental/ingest",
        json={
            "source_device": "sensor:lab_env",
            "protocol": "mqtt",
            "temperature_c": 21.8,
            "humidity_percent": 48.0,
            "contact_state": "closed",
        },
    )
    assert env_res.status_code == 201
    target_event_id = env_res.json()["event_id"]

    # 2. List perception events
    list_res = client.get("/api/perception/events?modality=environmental")
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total"] >= 1
    assert any(it["event_id"] == target_event_id for it in list_data["items"])

    # 3. Test cross-modal correlation endpoint
    corr_res = client.post(
        "/api/perception/correlate",
        json={
            "target_event_id": target_event_id,
            "time_window_seconds": 10.0,
        },
    )
    assert corr_res.status_code == 200
    corr_data = corr_res.json()
    assert corr_data["target_event_id"] == target_event_id
    assert "synthesized_observation" in corr_data
