import base64
import pytest
from pico.core.errors import AuthorizationError, ValidationError
from pico.perception.audio.adapter import (
    AudioSessionManager,
    LocalTextToSpeechAdapter,
    LocalWhisperSpeechToTextAdapter,
    MockSpeechToTextAdapter,
    MockTextToSpeechAdapter,
)
from pico.perception.contracts import AudioCaptureState, Modality


@pytest.mark.anyio
async def test_audio_session_lifecycle_push_to_talk() -> None:
    manager = AudioSessionManager()

    # 1. Capture defaults to not active
    status = manager.get_status()
    assert status["microphone_active"] is False
    assert status["active_sessions_count"] == 0

    # 2. Starting without consent must fail closed with AuthorizationError
    with pytest.raises(AuthorizationError):
        manager.start_session(user_consent=False)

    # 3. Explicit consent starts push-to-talk session
    start_res = manager.start_session(user_consent=True, source_device="mic:headset")
    assert start_res.microphone_active is True
    assert start_res.state == AudioCaptureState.CAPTURING
    session_id = start_res.session_id

    # Verify status reflects active microphone
    assert manager.get_status()["microphone_active"] is True

    # 4. Stop session with mock audio payload
    audio_data = b"TEXT:Hello PICO transcribe test"
    audio_b64 = base64.b64encode(audio_data).decode("ascii")
    stop_res, event = await manager.stop_session(
        session_id=session_id,
        audio_base64=audio_b64,
        audio_format="wav",
    )
    assert stop_res.state == AudioCaptureState.COMPLETED
    assert stop_res.transcription == "Hello PICO transcribe test"
    assert stop_res.confidence >= 0.9
    assert stop_res.data_classification == "INTERNAL"

    # Event contract checks
    assert event.modality == Modality.AUDIO
    assert event.event_type == "speech_transcribed"
    assert event.raw_payload_scrubbed is True
    assert "transcription" in event.structured_data
    # Invariant: Raw audio bytes are NEVER stored in event object
    assert "audio_bytes" not in event.structured_data

    # Microphone is now inactive
    assert manager.get_status()["microphone_active"] is False


@pytest.mark.anyio
async def test_audio_session_cancellation() -> None:
    manager = AudioSessionManager()
    start_res = manager.start_session(user_consent=True)
    session_id = start_res.session_id

    cancel_res = manager.cancel_session(session_id)
    assert cancel_res.state == AudioCaptureState.CANCELLED
    assert cancel_res.microphone_active is False
    assert manager.get_status()["microphone_active"] is False


@pytest.mark.anyio
async def test_audio_oversized_payload_rejected() -> None:
    manager = AudioSessionManager()
    start_res = manager.start_session(user_consent=True)
    session_id = start_res.session_id

    # Create oversized payload exceeding 10MB limit
    oversized = b"0" * (10 * 1024 * 1024 + 1024)
    oversized_b64 = base64.b64encode(oversized).decode("ascii")

    with pytest.raises(ValidationError) as exc_info:
        await manager.stop_session(session_id=session_id, audio_base64=oversized_b64)
    assert "exceeds limit" in str(exc_info.value)


@pytest.mark.anyio
async def test_audio_honest_local_runtime_availability() -> None:
    whisper_adapter = LocalWhisperSpeechToTextAdapter()
    tts_adapter = LocalTextToSpeechAdapter()

    # In our minimal environment without openai-whisper or pyttsx3 installed:
    # Availability must report False honestly and fail closed without silent cloud fallback
    assert whisper_adapter.is_available() is False
    assert "unavailable" in whisper_adapter.get_provider_name()

    assert tts_adapter.is_available() is False
    assert "unavailable" in tts_adapter.get_provider_name()

    # Attempting to transcribe on unavailable local adapter must fail closed
    with pytest.raises(ValidationError) as exc:
        await whisper_adapter.transcribe(b"dummy")
    assert "fails closed" in str(exc.value)


@pytest.mark.anyio
async def test_audio_synthesis_with_mock_adapter() -> None:
    manager = AudioSessionManager(tts_adapter=MockTextToSpeechAdapter())
    res = await manager.synthesize_speech("PICO system online.")
    assert res.audio_base64 is not None
    assert res.duration_seconds > 0
    assert res.synthesizer_provider == "mock_tts"
