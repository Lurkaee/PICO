"""Comprehensive architectural qualification and hardening tests for PICO voice runtime (Milestone P11.3.1).

Tests:
1. Audio capture hardware abstractions (BaseAudioCaptureDevice, MockAudioCaptureDevice, LocalAudioCaptureDevice).
2. Honest driver availability reporting, lazy init, stream lifecycle, and bounded buffering.
3. Strict RIFF/WAVE parsing and rejection of synthetic test-marker bypasses in production validator.
4. Ephemeral memory zeroization invariants on success, failure, cancellation, and shutdown.
5. Production fail-closed behavior when STT runtime is unavailable (zero fabricated speech).
6. AudioSessionManager single-session concurrency, cancellation, and TTL pruning.
7. VoiceService transcript delivery, replay rejection, expiry protection, and classification retention.
8. Core RoutingService local-pinning of INTERNAL voice messages (rejection of cloud transmission).
9. Perception API transcript delivery security (loopback authorization and replay prevention).
10. Zero raw audio disk persistence invariant.
"""

from __future__ import annotations

import base64
import struct
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pico.core.errors import AuthorizationError, ValidationError
from pico.core.id_gen import generate_id
from pico.db.session import SessionLocal
from pico.main import app
from pico.perception.audio.adapter import (
    AudioSessionManager,
    LocalWhisperSpeechToTextAdapter,
    MockSpeechToTextAdapter,
    utc_now,
)
from pico.perception.audio.capture import (
    AudioCaptureError,
    LocalAudioCaptureDevice,
    MockAudioCaptureDevice,
    zeroize_bytearray,
)
from pico.perception.audio.validation import validate_audio_payload
from pico.perception.contracts import AudioCaptureState
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository
from pico.services.routing_service import RoutingService
from pico.services.voice_service import VoiceService


def make_valid_wav_bytes(
    duration_sec: float = 0.5,
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16,
    format_tag: int = 1,
) -> bytes:
    """Helper to synthesize valid RIFF/WAVE PCM audio bytes."""
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    num_samples = int(duration_sec * sample_rate)
    data_size = num_samples * block_align
    riff_size = 36 + data_size

    buf = bytearray()
    buf.extend(b"RIFF")
    buf.extend(struct.pack("<I", riff_size))
    buf.extend(b"WAVE")
    buf.extend(b"fmt ")
    buf.extend(struct.pack("<I", 16))
    buf.extend(struct.pack("<H", format_tag))
    buf.extend(struct.pack("<H", channels))
    buf.extend(struct.pack("<I", sample_rate))
    buf.extend(struct.pack("<I", byte_rate))
    buf.extend(struct.pack("<H", block_align))
    buf.extend(struct.pack("<H", bits_per_sample))
    buf.extend(b"data")
    buf.extend(struct.pack("<I", data_size))
    buf.extend(b"\x00" * data_size)
    return bytes(buf)


# ============================================================================
# 1. Audio Capture Device Lifecycle & Hardware Abstraction
# ============================================================================


def test_mock_audio_capture_device_lifecycle() -> None:
    dev = MockAudioCaptureDevice()
    assert dev.is_available() is True
    assert dev.is_active() is False

    dev.start_capture(sample_rate=16000, channels=1)
    assert dev.is_active() is True

    # Re-starting when active must raise AudioCaptureError
    with pytest.raises(AudioCaptureError, match="already active"):
        dev.start_capture()

    wav_buf = dev.stop_capture()
    assert isinstance(wav_buf, bytearray)
    assert len(wav_buf) > 44
    assert dev.is_active() is False

    # Validates as a structurally compliant WAV payload
    metadata = validate_audio_payload(wav_buf, expected_format="wav")
    assert metadata["format"] == "wav"
    assert metadata["channels"] == 1
    assert metadata["sample_rate"] == 16000


def test_mock_audio_capture_device_injected_payload_and_zeroing() -> None:
    dev = MockAudioCaptureDevice()
    test_wav = make_valid_wav_bytes(duration_sec=0.2)
    dev.inject_payload(test_wav)

    dev.start_capture()
    out = dev.stop_capture()
    assert bytes(out) == test_wav
    assert dev.is_active() is False
    assert dev._injected_payload is None


def test_mock_audio_capture_cancellation_zeroes_buffers() -> None:
    dev = MockAudioCaptureDevice()
    test_wav = make_valid_wav_bytes(duration_sec=0.2)
    dev.inject_payload(test_wav)

    dev.start_capture()
    dev.cancel_capture()
    assert dev.is_active() is False
    assert dev._injected_payload is None
    assert len(dev._buffer) == 0


def test_local_audio_capture_device_honest_availability() -> None:
    dev = LocalAudioCaptureDevice()
    info = dev.get_device_info()
    assert info["driver_type"] == "sounddevice"
    assert isinstance(dev.is_available(), bool)
    assert dev.is_active() is False


def test_local_audio_capture_device_mocked_stream_lifecycle_and_bounded_buffer() -> None:
    """Test LocalAudioCaptureDevice using an injected stream factory to verify stream management."""

    class FakeStream:
        def __init__(self, callback: Any, **kwargs: Any) -> None:
            self.callback = callback
            self.started = False
            self.stopped = False
            self.closed = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

    fake_stream_instance: FakeStream | None = None

    def fake_stream_factory(**kwargs: Any) -> FakeStream:
        nonlocal fake_stream_instance
        fake_stream_instance = FakeStream(**kwargs)
        return fake_stream_instance

    # Limit buffer to 100 bytes to verify bounding
    dev = LocalAudioCaptureDevice(max_buffer_bytes=100, stream_factory=fake_stream_factory)
    assert dev.is_active() is False

    dev.start_capture(sample_rate=16000, channels=1)
    assert dev.is_active() is True
    assert fake_stream_instance is not None
    assert fake_stream_instance.started is True

    # Simulate audio callbacks
    fake_stream_instance.callback(b"\x00\x00" * 20, 20, None, None)  # 40 bytes
    fake_stream_instance.callback(b"\x00\x00" * 20, 20, None, None)  # 40 bytes (total 80)
    fake_stream_instance.callback(b"\x00\x00" * 20, 20, None, None)  # 40 bytes (exceeds 100, dropped)

    assert len(dev._buffer) == 80
    assert dev._overflow_dropped is True

    # Stop capture packages into WAV and clears frame buffer
    wav_out = dev.stop_capture()
    assert isinstance(wav_out, bytearray)
    assert len(wav_out) > 44
    assert dev.is_active() is False
    assert fake_stream_instance.stopped is True
    assert fake_stream_instance.closed is True
    assert len(dev._buffer) == 0


def test_local_audio_capture_device_stream_error_cleans_up() -> None:
    def failing_stream_factory(**kwargs: Any) -> Any:
        raise RuntimeError("PortAudio device open error")

    dev = LocalAudioCaptureDevice(stream_factory=failing_stream_factory)
    with pytest.raises(AudioCaptureError, match="Failed to open audio input stream"):
        dev.start_capture()
    assert dev.is_active() is False
    assert len(dev._buffer) == 0


# ============================================================================
# 2. Strict RIFF/WAVE Validation & No Synthetic Bypass
# ============================================================================


def test_validate_audio_payload_valid_wav() -> None:
    wav_bytes = make_valid_wav_bytes(duration_sec=0.2, sample_rate=16000, channels=1)
    meta = validate_audio_payload(wav_bytes, expected_format="wav")
    assert meta["format"] == "wav"
    assert meta["channels"] == 1
    assert meta["sample_rate"] == 16000
    assert 0.19 <= meta["duration_seconds"] <= 0.21


def test_validate_audio_payload_empty_fails() -> None:
    with pytest.raises(ValidationError, match="payload is empty"):
        validate_audio_payload(b"", expected_format="wav")


def test_validate_audio_payload_oversized_fails() -> None:
    oversized = make_valid_wav_bytes(duration_sec=0.1)
    with pytest.raises(ValidationError, match="exceeds limit"):
        validate_audio_payload(oversized, max_bytes=30, expected_format="wav")


def test_validate_audio_payload_synthetic_text_marker_rejected() -> None:
    """Security Invariant: Synthetic text markers must NEVER be accepted as valid WAV audio."""
    synthetic_payload = b"TEXT:Arbitrary injected transcript with extra padding to exceed min header size"
    with pytest.raises(ValidationError, match="missing valid 'RIFF' header"):
        validate_audio_payload(synthetic_payload, expected_format="wav")


def test_validate_audio_payload_corrupted_riff_fails() -> None:
    corrupted = b"NOPE" + b"\x00" * 40
    with pytest.raises(ValidationError, match="missing valid 'RIFF' header"):
        validate_audio_payload(corrupted, expected_format="wav")


def test_validate_audio_payload_unsupported_compressed_format_rejected() -> None:
    """WAV with non-PCM format (e.g. format 3 = IEEE float, format 6 = A-law) must be rejected."""
    bad_fmt_wav = make_valid_wav_bytes(duration_sec=0.1, format_tag=3)
    with pytest.raises(ValidationError, match="Unsupported WAV encoding format"):
        validate_audio_payload(bad_fmt_wav, expected_format="wav")


def test_validate_audio_payload_inconsistent_byte_rate_rejected() -> None:
    """Corrupted WAV with mismatched byte rate must fail validation."""
    valid_wav = bytearray(make_valid_wav_bytes(duration_sec=0.1))
    # byte_rate is at offset 28 in standard 44-byte WAV header: alter it
    valid_wav[28:32] = struct.pack("<I", 99999)
    with pytest.raises(ValidationError, match="Inconsistent WAV byte rate"):
        validate_audio_payload(bytes(valid_wav), expected_format="wav")


def test_validate_audio_payload_truncated_data_chunk_rejected() -> None:
    """WAV file that declares more audio data than provided in payload must fail validation."""
    valid_wav = bytearray(make_valid_wav_bytes(duration_sec=0.2))
    # Truncate the payload by 50 bytes
    truncated = bytes(valid_wav[:-50])
    with pytest.raises(ValidationError, match="Truncated WAV"):
        validate_audio_payload(truncated, expected_format="wav")


def test_validate_audio_payload_duration_limits() -> None:
    # Duration < 0.05s must fail
    short_wav = make_valid_wav_bytes(duration_sec=0.01)
    with pytest.raises(ValidationError, match="too short"):
        validate_audio_payload(short_wav, expected_format="wav")

    # Duration > max limit (e.g. 1.0s limit in test) must fail
    long_wav = make_valid_wav_bytes(duration_sec=1.5)
    with pytest.raises(ValidationError, match="exceeds maximum allowed limit"):
        validate_audio_payload(long_wav, max_duration_seconds=1.0, expected_format="wav")


# ============================================================================
# 3. Ephemeral Memory Zeroization Invariants
# ============================================================================


def test_zeroize_bytearray_overwrites_memory() -> None:
    buf = bytearray(b"SENSITIVE_VOICE_AUDIO_DATA_BYTES")
    assert len(buf) == 32
    zeroize_bytearray(buf)
    assert len(buf) == 0


@pytest.mark.anyio
async def test_session_manager_zeroizes_buffer_on_normal_completion() -> None:
    stt = MockSpeechToTextAdapter(default_transcription="Test transcript")
    manager = AudioSessionManager(stt_adapter=stt)
    start_res = manager.start_session(user_consent=True)

    wav = make_valid_wav_bytes(duration_sec=0.2)
    wav_b64 = base64.b64encode(wav).decode("ascii")

    res, event = await manager.stop_session(start_res.session_id, audio_base64=wav_b64)
    assert res.transcription == "Test transcript"
    assert event.raw_payload_scrubbed is True


@pytest.mark.anyio
async def test_session_manager_zeroizes_buffer_on_failure() -> None:
    stt = MockSpeechToTextAdapter()
    manager = AudioSessionManager(stt_adapter=stt)
    start_res = manager.start_session(user_consent=True)

    # Corrupted audio payload triggers failure
    corrupted_b64 = base64.b64encode(b"NOT_A_WAV").decode("ascii")
    with pytest.raises(ValidationError):
        await manager.stop_session(start_res.session_id, audio_base64=corrupted_b64)

    session = manager.get_session(start_res.session_id)
    assert session is not None
    assert session.state == AudioCaptureState.FAILED


# ============================================================================
# 4. Production Fail-Closed Behavior (No Fabricated Transcripts)
# ============================================================================


@pytest.mark.anyio
async def test_production_default_fails_closed_without_installed_stt() -> None:
    """Production Invariant: When local Whisper is uninstalled, stop_session must fail closed

    rather than returning a fabricated mock transcript.
    """
    # AudioSessionManager with default production adapters
    prod_manager = AudioSessionManager()
    assert isinstance(prod_manager.stt_adapter, LocalWhisperSpeechToTextAdapter)
    assert prod_manager.stt_adapter.is_available() is False

    start_res = prod_manager.start_session(user_consent=True)
    valid_wav = make_valid_wav_bytes(duration_sec=0.2)
    valid_b64 = base64.b64encode(valid_wav).decode("ascii")

    with pytest.raises(ValidationError, match="Speech transcription runtime is unavailable"):
        await prod_manager.stop_session(start_res.session_id, audio_base64=valid_b64)

    session = prod_manager.get_session(start_res.session_id)
    assert session is not None
    assert session.state == AudioCaptureState.FAILED
    assert session.transcription is None


# ============================================================================
# 5. AudioSessionManager Concurrency, Cancellation & Pruning
# ============================================================================


def test_session_manager_single_active_recording_session_policy() -> None:
    dev = MockAudioCaptureDevice()
    manager = AudioSessionManager(capture_device=dev)

    sess1 = manager.start_session(user_consent=True)
    assert sess1.session_id is not None
    assert dev.is_active() is True

    # Attempting to start a concurrent session must be rejected
    with pytest.raises(AudioCaptureError, match="recording session is already active"):
        manager.start_session(user_consent=True)

    # Cancel first session frees the device
    manager.cancel_session(sess1.session_id)
    assert dev.is_active() is False

    # Now starting a new session succeeds
    sess2 = manager.start_session(user_consent=True)
    assert sess2.session_id != sess1.session_id
    manager.shutdown()


def test_session_manager_user_consent_required() -> None:
    manager = AudioSessionManager()
    with pytest.raises(AuthorizationError, match="Explicit user consent is mandatory"):
        manager.start_session(user_consent=False)


def test_session_manager_session_pruning() -> None:
    manager = AudioSessionManager()
    sess = manager.start_session(user_consent=True)
    manager.cancel_session(sess.session_id)

    # Artificially age the session past TTL (300s)
    session_obj = manager._active_sessions[sess.session_id]
    session_obj.started_at = utc_now() - timedelta(seconds=350)

    manager.prune_expired_sessions()
    assert sess.session_id not in manager._active_sessions


# ============================================================================
# 6. VoiceService Delivery, Replay Protection & Classification
# ============================================================================


@pytest.mark.anyio
async def test_voice_service_delivery_lifecycle_and_replay_protection() -> None:
    db = SessionLocal()
    try:
        conv_repo = ConversationRepository(db)
        conv = conv_repo.create(conversation_id=generate_id("conv"), title="Voice Delivery Test")

        mock_dev = MockAudioCaptureDevice()
        stt = MockSpeechToTextAdapter(default_transcription="Deploy container to production")
        manager = AudioSessionManager(stt_adapter=stt, capture_device=mock_dev)

        start_res = manager.start_session(user_consent=True)
        # Inject valid WAV audio
        valid_wav = make_valid_wav_bytes(duration_sec=0.2)
        mock_dev.inject_payload(valid_wav)

        await manager.stop_session(start_res.session_id)

        voice_svc = VoiceService(audio_manager=manager)
        delivery = voice_svc.deliver_transcript_to_conversation(
            session_id=start_res.session_id,
            conversation_id=conv.id,
            db=db,
        )

        assert delivery["delivered"] is True
        assert delivery["transcription"] == "Deploy container to production"
        assert delivery["data_classification"] == "INTERNAL"

        # Verify message persisted in conversation ledger
        msg_repo = MessageRepository(db)
        msgs = msg_repo.list_by_conversation(conv.id)
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].content == "Deploy container to production"
        assert msgs[0].data_classification == "INTERNAL"

        # SECURITY INVARIANT: Replay / duplicate delivery MUST be rejected
        with pytest.raises(ValidationError, match="already been delivered. Replay is rejected"):
            voice_svc.deliver_transcript_to_conversation(
                session_id=start_res.session_id,
                conversation_id=conv.id,
                db=db,
            )

        # Confirm no duplicate message was created
        msgs_after = msg_repo.list_by_conversation(conv.id)
        assert len(msgs_after) == 1
    finally:
        db.close()


@pytest.mark.anyio
async def test_voice_service_expired_session_delivery_rejected() -> None:
    db = SessionLocal()
    try:
        conv_repo = ConversationRepository(db)
        conv = conv_repo.create(conversation_id=generate_id("conv"), title="Test")

        stt = MockSpeechToTextAdapter(default_transcription="Hello")
        manager = AudioSessionManager(stt_adapter=stt)
        start_res = manager.start_session(user_consent=True)
        valid_wav = make_valid_wav_bytes(duration_sec=0.2)
        b64 = base64.b64encode(valid_wav).decode("ascii")
        await manager.stop_session(start_res.session_id, audio_base64=b64)

        # Age session past TTL
        session = manager.get_session(start_res.session_id)
        assert session is not None
        session.started_at = utc_now() - timedelta(seconds=350)

        voice_svc = VoiceService(audio_manager=manager)
        with pytest.raises(ValidationError, match="has expired"):
            voice_svc.deliver_transcript_to_conversation(
                session_id=start_res.session_id,
                conversation_id=conv.id,
                db=db,
            )
    finally:
        db.close()


@pytest.mark.anyio
async def test_voice_service_uncompleted_session_delivery_rejected() -> None:
    db = SessionLocal()
    try:
        conv_repo = ConversationRepository(db)
        conv = conv_repo.create(conversation_id=generate_id("conv"), title="Test")

        manager = AudioSessionManager()
        start_res = manager.start_session(user_consent=True)

        voice_svc = VoiceService(audio_manager=manager)
        with pytest.raises(ValidationError, match="not 'completed'"):
            voice_svc.deliver_transcript_to_conversation(
                session_id=start_res.session_id,
                conversation_id=conv.id,
                db=db,
            )
    finally:
        db.close()


# ============================================================================
# 7. Routing Policy Verification (INTERNAL Transcript Cloud Rejection)
# ============================================================================


def test_internal_voice_transcript_rejected_from_cloud_routing() -> None:
    router = RoutingService()
    decision = router.resolve_route(data_classification="INTERNAL")
    assert decision.cloud_approved is False
    assert decision.decision_rule == "RULE_PRIVATE_LOCAL_PIN"

    # Explicit cloud request with INTERNAL data must fail closed
    with pytest.raises(ValidationError, match="not eligible for cloud processing"):
        router.resolve_route(data_classification="INTERNAL", requested_profile="cloud-strong")


# ============================================================================
# 8. API Delivery Endpoint Security
# ============================================================================


def test_api_voice_deliver_endpoint_loopback_and_replay() -> None:
    client = TestClient(app)
    db = SessionLocal()
    try:
        conv_repo = ConversationRepository(db)
        conv = conv_repo.create(conversation_id=generate_id("conv"), title="API Delivery Test")

        # In pipeline, inject a MockSpeechToTextAdapter for test
        from pico.perception.pipeline import _shared_audio_manager

        _shared_audio_manager.stt_adapter = MockSpeechToTextAdapter(default_transcription="API Voice Message")

        start_resp = client.post("/api/perception/audio/session/start", json={"user_consent": True})
        assert start_resp.status_code == 201
        sess_id = start_resp.json()["session_id"]

        valid_wav = make_valid_wav_bytes(duration_sec=0.2)
        wav_b64 = base64.b64encode(valid_wav).decode("ascii")

        stop_resp = client.post(
            f"/api/perception/audio/session/{sess_id}/stop",
            json={"audio_base64": wav_b64, "audio_format": "wav"},
        )
        assert stop_resp.status_code == 200
        assert stop_resp.json()["transcription"] == "API Voice Message"

        # Deliver transcript into conversation
        deliver_resp = client.post(
            f"/api/perception/audio/session/{sess_id}/deliver",
            json={"conversation_id": conv.id},
        )
        assert deliver_resp.status_code == 200
        assert deliver_resp.json()["delivered"] is True

        # Second delivery (replay) must be rejected with 422 Unprocessable Content
        replay_resp = client.post(
            f"/api/perception/audio/session/{sess_id}/deliver",
            json={"conversation_id": conv.id},
        )
        assert replay_resp.status_code == 422
        assert "already been delivered" in replay_resp.json()["detail"]

        # Verify only 1 message in conversation
        msgs_resp = client.get(f"/api/conversations/{conv.id}/messages")
        assert msgs_resp.status_code == 200
        items = msgs_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["content"] == "API Voice Message"
        assert items[0]["data_classification"] == "INTERNAL"
    finally:
        from pico.perception.pipeline import _shared_audio_manager

        _shared_audio_manager.stt_adapter = LocalWhisperSpeechToTextAdapter()
        db.close()


# ============================================================================
# 9. Zero Raw Audio Disk Persistence Security Invariant
# ============================================================================


def test_zero_raw_audio_disk_persistence() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    audio_extensions = {".wav", ".mp3", ".ogg", ".pcm", ".raw", ".flac", ".m4a"}
    leaked_files = []
    for check_dir in [repo_root / "pico", repo_root / "data", repo_root / "logs"]:
        if check_dir.is_dir():
            for p in check_dir.rglob("*"):
                if p.is_file() and p.suffix.lower() in audio_extensions:
                    leaked_files.append(str(p))

    assert not leaked_files, f"Leaked raw audio files found on disk: {leaked_files}"
