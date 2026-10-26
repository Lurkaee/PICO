import base64
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.perception.classification import (
    compute_trusted_perception_classification,
    validate_trusted_source_device,
)
from pico.perception.contracts import (
    AudioCaptureState,
    AudioSessionStartResponse,
    AudioSessionStopResponse,
    AudioSynthesisResponse,
    Modality,
    NormalizedPerceptionEvent,
    RetentionPolicy,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BaseSpeechToTextAdapter(ABC):
    is_mock: bool = False
    is_simulated: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """Report whether local STT runtime and model weights are installed."""
        ...

    @abstractmethod
    def get_provider_name(self) -> str:
        ...

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, audio_format: str = "wav") -> tuple[str, float]:
        """Returns (transcription_text, confidence_score)."""
        ...


class BaseTextToSpeechAdapter(ABC):
    is_mock: bool = False
    is_simulated: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """Report whether local TTS runtime is installed."""
        ...

    @abstractmethod
    def get_provider_name(self) -> str:
        ...

    @abstractmethod
    async def synthesize(self, text: str, voice_id: str | None = None, speed: float = 1.0) -> tuple[bytes, float]:
        """Returns (audio_bytes, duration_seconds)."""
        ...


class MockSpeechToTextAdapter(BaseSpeechToTextAdapter):
    is_mock: bool = True
    is_simulated: bool = True

    def __init__(self, default_transcription: str = "Hello PICO, check system status.") -> None:
        self.default_transcription = default_transcription

    def is_available(self) -> bool:
        return True

    def get_provider_name(self) -> str:
        return "mock_stt"

    async def transcribe(self, audio_bytes: bytes, audio_format: str = "wav") -> tuple[str, float]:
        if not audio_bytes:
            raise ValidationError("Audio payload is empty.")
        # If payload contains a custom marker, extract it for test flexibility
        if audio_bytes.startswith(b"TEXT:"):
            return audio_bytes[5:].decode("utf-8", errors="replace"), 0.98
        return self.default_transcription, 0.95


class LocalWhisperSpeechToTextAdapter(BaseSpeechToTextAdapter):
    is_mock: bool = False
    is_simulated: bool = False

    def is_available(self) -> bool:
        # Honest capability reporting: local whisper runtime execution requires installed weights
        return False

    def get_provider_name(self) -> str:
        return "local_whisper (unavailable)"

    async def transcribe(self, audio_bytes: bytes, audio_format: str = "wav") -> tuple[str, float]:
        raise ValidationError(
            "Local Whisper speech recognition runtime is unavailable. Audio transcription "
            "requires an installed local STT runtime and model weights."
        )


class MockTextToSpeechAdapter(BaseTextToSpeechAdapter):
    is_mock: bool = True
    is_simulated: bool = True

    def is_available(self) -> bool:
        return True

    def get_provider_name(self) -> str:
        return "mock_tts"

    async def synthesize(self, text: str, voice_id: str | None = None, speed: float = 1.0) -> tuple[bytes, float]:
        # Generate minimal RIFF/WAV header (44 bytes) + silence
        wav_header = (
            b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
            b"\x80>\x00\x00\x80>\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00"
        )
        duration = max(0.5, len(text) * 0.05)
        return wav_header, duration


class LocalTextToSpeechAdapter(BaseTextToSpeechAdapter):
    is_mock: bool = False
    is_simulated: bool = False

    def is_available(self) -> bool:
        # Honest capability reporting: local pyttsx3 execution requires local audio driver
        return False

    def get_provider_name(self) -> str:
        return "local_pyttsx3 (unavailable)"

    async def synthesize(self, text: str, voice_id: str | None = None, speed: float = 1.0) -> tuple[bytes, float]:
        raise ValidationError(
            "Local speech synthesis runtime is unavailable. Audio synthesis "
            "requires an installed local TTS runtime."
        )


class AudioSession:
    def __init__(self, session_id: str, source_device: str, user_consent: bool) -> None:
        self.session_id = session_id
        self.source_device = source_device
        self.user_consent = user_consent
        self.state = AudioCaptureState.CAPTURING
        self.started_at = utc_now()
        self.completed_at: datetime | None = None
        self.transcription: str | None = None
        self.confidence: float = 0.0


class AudioSessionManager:
    """Manages explicit push-to-talk audio capture sessions.

    Non-negotiable invariants:
    1. Push-to-talk only: No background or wake-word listening.
    2. Microphone capture starts ONLY after explicit local user consent.
    3. Never store raw audio recordings: raw buffers are scrubbed immediately.
    4. Audio data is classified INTERNAL and NEVER sent to cloud services.
    """

    def __init__(
        self,
        stt_adapter: BaseSpeechToTextAdapter | None = None,
        tts_adapter: BaseTextToSpeechAdapter | None = None,
    ) -> None:
        self.stt_adapter = stt_adapter or (
            LocalWhisperSpeechToTextAdapter()
            if LocalWhisperSpeechToTextAdapter().is_available()
            else MockSpeechToTextAdapter()
        )
        self.tts_adapter = tts_adapter or (
            LocalTextToSpeechAdapter()
            if LocalTextToSpeechAdapter().is_available()
            else MockTextToSpeechAdapter()
        )
        self._active_sessions: dict[str, AudioSession] = {}

    def get_session(self, session_id: str) -> AudioSession | None:
        return self._active_sessions.get(session_id)

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": settings.PERCEPTION_ENABLED,
            "physical_microphone_connected": False,
            "microphone_active": False,
            "active_sessions_count": sum(
                1 for s in self._active_sessions.values() if s.state == AudioCaptureState.CAPTURING
            ),
            "stt_available": self.stt_adapter.is_available() and not getattr(self.stt_adapter, "is_mock", False),
            "stt_provider": self.stt_adapter.get_provider_name(),
            "stt_is_mock": getattr(self.stt_adapter, "is_mock", False),
            "tts_available": self.tts_adapter.is_available() and not getattr(self.tts_adapter, "is_mock", False),
            "tts_provider": self.tts_adapter.get_provider_name(),
            "tts_is_mock": getattr(self.tts_adapter, "is_mock", False),
            "live_speech_capture_available": False,
            "api_endpoint_available": True,
            "max_capture_seconds": settings.AUDIO_CAPTURE_MAX_SECONDS,
            "max_payload_bytes": settings.AUDIO_MAX_PAYLOAD_BYTES,
            "graphical_indicator_note": (
                "Observable microphone_active status contract available. "
                "Physical microphone driver is not active; hardware visual indicator requires client frontend."
            ),
        }

    def start_session(
        self,
        user_consent: bool,
        source_device: str = "mic:local",
        metadata: dict[str, Any] | None = None,
    ) -> AudioSessionStartResponse:
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        if not user_consent:
            raise AuthorizationError("Explicit user consent is mandatory for audio capture.")

        clean_device = validate_trusted_source_device(Modality.AUDIO, source_device)
        session_id = f"aud_{uuid.uuid4().hex[:16]}"
        session = AudioSession(session_id=session_id, source_device=clean_device, user_consent=user_consent)
        self._active_sessions[session_id] = session

        return AudioSessionStartResponse(
            session_id=session_id,
            state=session.state,
            microphone_active=False,
            started_at=session.started_at,
            message="Push-to-talk recording session initialized (hardware microphone driver not active; client audio payload required).",
        )

    async def stop_session(
        self,
        session_id: str,
        audio_base64: str | None = None,
        audio_format: str = "wav",
        client_duration_seconds: float | None = None,
    ) -> tuple[AudioSessionStopResponse, NormalizedPerceptionEvent]:
        session = self._active_sessions.get(session_id)
        if not session:
            raise ValidationError(f"Audio session '{session_id}' not found.")

        if session.state != AudioCaptureState.CAPTURING:
            raise ValidationError(f"Session '{session_id}' is in '{session.state.value}' state, not capturing.")

        session.state = AudioCaptureState.PROCESSING

        # Validate and decode audio bytes - mandatory payload, zero fake audio
        if not audio_base64:
            session.state = AudioCaptureState.FAILED
            raise ValidationError("Audio payload is empty or missing. Transcription requires an audio payload.")

        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as e:
            session.state = AudioCaptureState.FAILED
            raise ValidationError(f"Invalid base64 audio payload: {e}")

        if not audio_bytes:
            session.state = AudioCaptureState.FAILED
            raise ValidationError("Audio payload is empty or missing. Transcription requires an audio payload.")

        # Enforce size limits
        if len(audio_bytes) > settings.AUDIO_MAX_PAYLOAD_BYTES:
            session.state = AudioCaptureState.FAILED
            raise ValidationError(
                f"Audio payload size ({len(audio_bytes)} bytes) exceeds limit of "
                f"{settings.AUDIO_MAX_PAYLOAD_BYTES} bytes."
            )

        # Transcribe through neutral STT adapter
        start_t = time.perf_counter()
        try:
            transcription, confidence = await self.stt_adapter.transcribe(
                audio_bytes=audio_bytes,
                audio_format=audio_format,
            )
            elapsed = time.perf_counter() - start_t
            session.transcription = transcription
            session.confidence = confidence
            session.state = AudioCaptureState.COMPLETED
            session.completed_at = utc_now()
        except Exception as exc:
            session.state = AudioCaptureState.FAILED
            raise ValidationError(f"Speech transcription failed: {exc}")
        finally:
            # INVARIANT: Discard raw audio bytes immediately
            del audio_bytes

        duration_sec = client_duration_seconds or max(0.5, round(elapsed, 2))
        effective_classification = compute_trusted_perception_classification(
            modality=Modality.AUDIO,
            source_device=session.source_device,
        )

        event = NormalizedPerceptionEvent(
            modality=Modality.AUDIO,
            event_type="speech_transcribed",
            source_device_id=session.source_device,
            data_classification=effective_classification,
            confidence=session.confidence,
            raw_payload_scrubbed=True,
            structured_data={
                "transcription": session.transcription,
                "duration_seconds": duration_sec,
                "audio_format": audio_format,
                "stt_provider": self.stt_adapter.get_provider_name(),
                "stt_is_mock": getattr(self.stt_adapter, "is_mock", False),
            },
            provenance=f"audio:{session.source_device}:{session_id}",
            retention_policy=RetentionPolicy.SHORT_TERM_AUDIT,
            session_id=session_id,
        )

        response = AudioSessionStopResponse(
            session_id=session_id,
            state=session.state,
            transcription=session.transcription,
            confidence=session.confidence,
            duration_seconds=duration_sec,
            event_id=event.event_id,
            data_classification=effective_classification,
            provenance=event.provenance,
        )
        return response, event

    def cancel_session(self, session_id: str) -> AudioSessionStartResponse:
        session = self._active_sessions.get(session_id)
        if not session:
            raise ValidationError(f"Audio session '{session_id}' not found.")

        session.state = AudioCaptureState.CANCELLED
        session.completed_at = utc_now()

        return AudioSessionStartResponse(
            session_id=session_id,
            state=session.state,
            microphone_active=False,
            started_at=session.started_at,
            message="Audio session cancelled and temporary resources scrubbed.",
        )

    async def synthesize_speech(
        self,
        text: str,
        voice_id: str | None = None,
        speed: float = 1.0,
    ) -> AudioSynthesisResponse:
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        audio_bytes, duration = await self.tts_adapter.synthesize(text, voice_id=voice_id, speed=speed)
        b64 = base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else None

        return AudioSynthesisResponse(
            audio_base64=b64,
            audio_format="wav",
            duration_seconds=duration,
            synthesizer_provider=self.tts_adapter.get_provider_name(),
            is_simulated=isinstance(self.tts_adapter, MockTextToSpeechAdapter),
        )
