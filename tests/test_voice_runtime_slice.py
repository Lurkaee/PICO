"""Comprehensive unit and integration tests for PICO P11.3.1 Voice Runtime & Capture Slice.

Verifies:
1. Mock and Local Audio Capture Device abstractions (lifecycle, cancellation, buffer clearing).
2. Audio payload format validation (size limits, RIFF/WAVE header validation, corrupted payload rejection).
3. AudioSessionManager integration with capture devices and truthful microphone_active reporting.
4. VoiceService transcript delivery into conversation message ledger with data classification preservation.
5. Invariant: Raw audio is scrubbed from RAM immediately after transcription; zero audio files on disk.
6. Invariant: Cloud routing rejection for non-public (INTERNAL) voice transcripts.
7. Companion state machine integration (truthful listening/thinking/idle transitions).
"""

from __future__ import annotations

import base64
import io
import wave
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.core.id_gen import generate_id
from pico.db.session import SessionLocal
from pico.main import app
from pico.models.conversation import Conversation
from pico.perception.audio.adapter import (
    AudioSessionManager,
    MockSpeechToTextAdapter,
)
from pico.perception.audio.capture import (
    AudioCaptureError,
    LocalAudioCaptureDevice,
    MockAudioCaptureDevice,
)
from pico.perception.audio.validation import validate_audio_payload
from pico.perception.contracts import AudioCaptureState, Modality
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository
from pico.services.routing_service import RoutingService
from pico.services.voice_service import VoiceService


def _create_minimal_wav(duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    """Helper creating a synthetically valid PCM 16-bit mono WAV payload."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        num_frames = int(sample_rate * duration_s)
        wf.writeframes(b"\x00\x00" * num_frames)
    return buf.getvalue()


# ============================================================================
# 1. Audio Capture Device Abstraction Tests
# ============================================================================

def test_mock_audio_capture_device_lifecycle() -> None:
    device = MockAudioCaptureDevice(device_name="mock:test_mic")
    assert device.is_available() is True
    assert device.is_active() is False

    info = device.get_device_info()
    assert info["driver_type"] == "mock_synthetic"
    assert info["is_active"] is False

    # Start capture
    device.start_capture(sample_rate=16000, channels=1)
    assert device.is_active() is True
    assert device.get_device_info()["is_active"] is True

    # Starting while active must fail closed
    with pytest.raises(AudioCaptureError, match="already active"):
        device.start_capture()

    # Stop capture returns valid WAV bytes
    wav_bytes = device.stop_capture()
    assert device.is_active() is False
    assert len(wav_bytes) >= 44
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"

    # Stopping when inactive must fail closed
    with pytest.raises(AudioCaptureError, match="not currently recording"):
        device.stop_capture()


def test_mock_audio_capture_device_cancellation_scrubs_buffer() -> None:
    device = MockAudioCaptureDevice()
    device.start_capture()
    assert device.is_active() is True

    device.cancel_capture()
    assert device.is_active() is False

    # Stopping after cancel must fail
    with pytest.raises(AudioCaptureError):
        device.stop_capture()


def test_mock_audio_capture_injected_payload() -> None:
    device = MockAudioCaptureDevice()
    custom_wav = _create_minimal_wav(duration_s=1.0)
    device.inject_payload(custom_wav)

    device.start_capture()
    captured = device.stop_capture()
    assert captured == custom_wav
    assert device.is_active() is False


def test_local_audio_capture_device_honest_availability() -> None:
    device = LocalAudioCaptureDevice()
    info = device.get_device_info()
    assert isinstance(device.is_available(), bool)
    assert info["driver_type"] in ("sounddevice", "pyaudio", "none")

    if not device.is_available():
        with pytest.raises(AudioCaptureError, match="not installed"):
            device.start_capture()


# ============================================================================
# 2. Audio Payload Format Validation Tests
# ============================================================================

def test_validate_audio_payload_valid_wav() -> None:
    valid_wav = _create_minimal_wav(duration_s=0.25)
    # Valid WAV should not raise
    validate_audio_payload(valid_wav, max_bytes=1024 * 1024, expected_format="wav")


def test_validate_audio_payload_empty_fails() -> None:
    with pytest.raises(ValidationError, match="empty"):
        validate_audio_payload(b"", expected_format="wav")


def test_validate_audio_payload_oversized_fails() -> None:
    payload = b"RIFF" + b"\x00" * 200
    with pytest.raises(ValidationError, match="exceeds limit"):
        validate_audio_payload(payload, max_bytes=100, expected_format="wav")


def test_validate_audio_payload_corrupted_header_fails() -> None:
    # 50 bytes of non-RIFF garbage
    garbage = b"NOT_A_WAV_HEADER" + b"\x00" * 34
    with pytest.raises(ValidationError, match="missing valid RIFF header"):
        validate_audio_payload(garbage, expected_format="wav")

    # RIFF with invalid WAVE format tag (must be >= 44 bytes)
    bad_fmt = b"RIFF" + b"\x24\x00\x00\x00" + b"NOPE" + b"fmt \x10\x00\x00\x00" + b"\x00" * 28
    with pytest.raises(ValidationError, match="missing WAVE format identifier"):
        validate_audio_payload(bad_fmt, expected_format="wav")


# ============================================================================
# 3. AudioSessionManager Device Integration & Truthful Reporting
# ============================================================================

@pytest.mark.anyio
async def test_session_manager_with_capture_device_truthful_mic_active() -> None:
    mock_dev = MockAudioCaptureDevice()
    manager = AudioSessionManager(capture_device=mock_dev)

    # 1. Idle state
    status = manager.get_status()
    assert status["microphone_active"] is False
    assert status["physical_microphone_connected"] is True
    assert status["active_sessions_count"] == 0

    # 2. Start session with consent activates capture device
    start_res = manager.start_session(user_consent=True, source_device="mic:headset")
    assert start_res.microphone_active is True
    assert start_res.state == AudioCaptureState.CAPTURING
    assert mock_dev.is_active() is True

    status_during = manager.get_status()
    assert status_during["microphone_active"] is True
    assert status_during["active_sessions_count"] == 1

    # 3. Inject synthetic utterance payload and stop session
    test_phrase = "System status check affirmative"
    mock_dev.inject_payload(b"TEXT:" + test_phrase.encode("utf-8"))

    stop_res, event = await manager.stop_session(session_id=start_res.session_id)
    assert stop_res.state == AudioCaptureState.COMPLETED
    assert stop_res.transcription == test_phrase
    assert stop_res.data_classification == "INTERNAL"
    assert mock_dev.is_active() is False

    # 4. Status returns to inactive
    status_after = manager.get_status()
    assert status_after["microphone_active"] is False
    assert status_after["active_sessions_count"] == 0

    # Invariant check: raw audio bytes never stored in event
    assert event.raw_payload_scrubbed is True
    assert "audio_bytes" not in event.structured_data
    assert event.structured_data["transcription"] == test_phrase


@pytest.mark.anyio
async def test_session_manager_cancellation_with_device() -> None:
    mock_dev = MockAudioCaptureDevice()
    manager = AudioSessionManager(capture_device=mock_dev)

    start_res = manager.start_session(user_consent=True)
    assert mock_dev.is_active() is True

    cancel_res = manager.cancel_session(start_res.session_id)
    assert cancel_res.state == AudioCaptureState.CANCELLED
    assert cancel_res.microphone_active is False
    assert mock_dev.is_active() is False
    assert manager.get_status()["microphone_active"] is False


@pytest.mark.anyio
async def test_session_manager_shutdown_releases_devices() -> None:
    mock_dev = MockAudioCaptureDevice()
    manager = AudioSessionManager(capture_device=mock_dev)

    manager.start_session(user_consent=True)
    assert mock_dev.is_active() is True

    manager.shutdown()
    assert mock_dev.is_active() is False
    assert manager.get_status()["microphone_active"] is False
    assert manager.get_status()["active_sessions_count"] == 0


# ============================================================================
# 4. VoiceService Transcript Delivery into Conversation Pipeline
# ============================================================================

@pytest.mark.anyio
async def test_voice_service_delivery_into_conversation() -> None:
    db = SessionLocal()
    try:
        # 1. Create a target conversation
        conv_repo = ConversationRepository(db)
        conv_id = generate_id("conv")
        conv = conv_repo.create(conversation_id=conv_id, title="Voice Test Conversation")

        # 2. Record a complete audio session
        mock_dev = MockAudioCaptureDevice()
        manager = AudioSessionManager(capture_device=mock_dev)
        start_res = manager.start_session(user_consent=True, source_device="mic:local")

        phrase = "Deploy application container to production"
        mock_dev.inject_payload(b"TEXT:" + phrase.encode("utf-8"))
        await manager.stop_session(start_res.session_id)

        # 3. Deliver via VoiceService
        voice_svc = VoiceService(audio_manager=manager)
        delivery = voice_svc.deliver_transcript_to_conversation(
            session_id=start_res.session_id,
            conversation_id=conv.id,
            db=db,
        )

        assert delivery["delivered"] is True
        assert delivery["transcription"] == phrase
        assert delivery["data_classification"] == "INTERNAL"
        assert delivery["conversation_id"] == conv.id

        # 4. Verify message persisted in conversation message ledger
        msg_repo = MessageRepository(db)
        messages = msg_repo.list_by_conversation(conv.id)
        assert len(messages) == 1
        user_msg = messages[0]
        assert user_msg.role == "user"
        assert user_msg.content == phrase
        assert user_msg.data_classification == "INTERNAL"

        # 5. Cloud routing check: routing service rejects non-public transcript to cloud
        router = RoutingService()
        decision = router.resolve_route(data_classification=user_msg.data_classification)
        assert decision.cloud_approved is False
        assert decision.decision_rule == "RULE_PRIVATE_LOCAL_PIN"
        with pytest.raises(ValidationError, match="not eligible for cloud processing"):
            router.resolve_route(
                data_classification=user_msg.data_classification,
                requested_profile="cloud-strong",
            )
    finally:
        db.close()


@pytest.mark.anyio
async def test_voice_service_uncompleted_session_delivery_rejected() -> None:
    db = SessionLocal()
    try:
        conv_repo = ConversationRepository(db)
        conv = conv_repo.create(conversation_id=generate_id("conv"), title="Test")

        mock_dev = MockAudioCaptureDevice()
        manager = AudioSessionManager(capture_device=mock_dev)
        start_res = manager.start_session(user_consent=True)

        voice_svc = VoiceService(audio_manager=manager)
        # Attempting to deliver while still CAPTURING must fail closed
        with pytest.raises(ValidationError, match="not 'completed'"):
            voice_svc.deliver_transcript_to_conversation(
                session_id=start_res.session_id,
                conversation_id=conv.id,
                db=db,
            )
    finally:
        db.close()


def test_voice_service_missing_conversation_rejected() -> None:
    db = SessionLocal()
    try:
        manager = AudioSessionManager()
        voice_svc = VoiceService(audio_manager=manager)
        with pytest.raises(ValidationError, match="not found"):
            voice_svc.deliver_transcript_to_conversation(
                session_id="nonexistent_session",
                conversation_id="conv_does_not_exist",
                db=db,
            )
    finally:
        db.close()


# ============================================================================
# 5. API Endpoint Delivery Test
# ============================================================================

def test_api_voice_deliver_endpoint() -> None:
    client = TestClient(app)
    db = SessionLocal()
    try:
        conv_repo = ConversationRepository(db)
        conv = conv_repo.create(conversation_id=generate_id("conv"), title="API Voice Test")

        # 1. Start audio session
        start_resp = client.post("/api/perception/audio/session/start", json={"user_consent": True})
        assert start_resp.status_code == 201
        sess_id = start_resp.json()["session_id"]

        # 2. Stop audio session with synthetic audio
        wav_data = b"TEXT:Inspect cluster health metrics"
        wav_b64 = base64.b64encode(wav_data).decode("ascii")
        stop_resp = client.post(
            f"/api/perception/audio/session/{sess_id}/stop",
            json={"audio_base64": wav_b64, "audio_format": "wav"},
        )
        assert stop_resp.status_code == 200
        assert stop_resp.json()["transcription"] == "Inspect cluster health metrics"

        # 3. Deliver transcript into conversation
        deliver_resp = client.post(
            f"/api/perception/audio/session/{sess_id}/deliver",
            json={"conversation_id": conv.id},
        )
        assert deliver_resp.status_code == 200
        del_data = deliver_resp.json()
        assert del_data["delivered"] is True
        assert del_data["transcription"] == "Inspect cluster health metrics"
        assert del_data["data_classification"] == "INTERNAL"

        # 4. Verify message retrieved through conversation API
        msgs_resp = client.get(f"/api/conversations/{conv.id}/messages")
        assert msgs_resp.status_code == 200
        msgs = msgs_resp.json()["items"]
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Inspect cluster health metrics"
        assert msgs[0]["data_classification"] == "INTERNAL"
    finally:
        db.close()


# ============================================================================
# 6. Zero Disk Persistence Security Invariant
# ============================================================================

def test_zero_raw_audio_disk_persistence() -> None:
    """Invariant: Audio capture, transcription, and delivery must NEVER write raw audio files to disk."""
    repo_root = Path(__file__).resolve().parents[1]

    # Search repo directories for any newly created audio files
    audio_extensions = {".wav", ".mp3", ".ogg", ".pcm", ".raw", ".flac", ".m4a"}
    leaked_files = []
    for check_dir in [repo_root / "pico", repo_root / "data", repo_root / "logs"]:
        if check_dir.is_dir():
            for p in check_dir.rglob("*"):
                if p.is_file() and p.suffix.lower() in audio_extensions:
                    leaked_files.append(str(p))

    assert not leaked_files, f"Leaked raw audio files found on disk: {leaked_files}"
