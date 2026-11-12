"""Audio session manager and speech adapter interfaces for PICO voice runtime (Milestone P11.3).

Architectural Invariants:
1. Push-to-talk only: No background or wake-word listening without explicit local user consent.
2. Ephemeral audio memory: Mutable bytearray buffers are actively zeroed in finally blocks on
   success, failure, cancellation, and shutdown. Raw audio is never persisted to disk.
3. Truthful device reporting: microphone_active is True ONLY while genuine capture device is recording.
4. Privacy classification: Audio perception data defaults to INTERNAL and is strictly pinned locally.
5. Fail closed: When local STT runtime is uninstalled, stop_session reports capability unavailable
   rather than fabricating recognized speech from untrusted inputs.
"""

from __future__ import annotations

import base64
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.perception.audio.capture import (
    AudioCaptureError,
    BaseAudioCaptureDevice,
    zeroize_bytearray,
)
from pico.perception.audio.validation import validate_audio_payload
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

AUDIO_SESSION_TTL_SECONDS = 300.0  # 5 minutes


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BaseSpeechToTextAdapter(ABC):
    """Abstract interface for local speech-to-text recognition engines."""

    is_mock: bool = False
    is_simulated: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """Report whether local STT runtime and model weights are installed."""

    @abstractmethod
    def get_provider_name(self) -> str:
        """Report descriptive provider/model name."""

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes | bytearray, audio_format: str = "wav") -> tuple[str, float]:
        """Transcribe validated audio bytes. Returns (transcription_text, confidence_score)."""


class BaseTextToSpeechAdapter(ABC):
    """Abstract interface for local text-to-speech synthesis engines."""

    is_mock: bool = False
    is_simulated: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """Report whether local TTS runtime is installed."""

    @abstractmethod
    def get_provider_name(self) -> str:
        """Report descriptive provider name."""

    @abstractmethod
    async def synthesize(self, text: str, voice_id: str | None = None, speed: float = 1.0) -> tuple[bytes, float]:
        """Synthesize text to audio. Returns (audio_bytes, duration_seconds)."""


class LocalWhisperSpeechToTextAdapter(BaseSpeechToTextAdapter):
    """Production local STT adapter (planned for Milestone P11.3.2).

    Honest capability reporting: reports unavailable until local speech model
    weights and runtime dependencies are qualified and installed.
    """

    is_mock: bool = False
    is_simulated: bool = False

    def is_available(self) -> bool:
        return False

    def get_provider_name(self) -> str:
        return "local_whisper (unavailable)"

    async def transcribe(self, audio_bytes: bytes | bytearray, audio_format: str = "wav") -> tuple[str, float]:
        raise ValidationError(
            "Speech transcription runtime is unavailable. Audio transcription "
            "requires an installed local STT engine (planned for Milestone P11.3.2)."
        )


class MockSpeechToTextAdapter(BaseSpeechToTextAdapter):
    """Deterministic in-memory speech-to-text adapter for test injection ONLY.

    Never used in production flow. Requires explicit injection into AudioSessionManager.
    """

    is_mock: bool = True
    is_simulated: bool = True

    def __init__(self, default_transcription: str = "Hello PICO, check system status.") -> None:
        self.default_transcription = default_transcription

    def is_available(self) -> bool:
        return True

    def get_provider_name(self) -> str:
        return "mock_stt"

    async def transcribe(self, audio_bytes: bytes | bytearray, audio_format: str = "wav") -> tuple[str, float]:
        if not audio_bytes:
            raise ValidationError("Audio payload is empty.")
        return self.default_transcription, 0.95


class MockTextToSpeechAdapter(BaseTextToSpeechAdapter):
    is_mock: bool = True
    is_simulated: bool = True

    def is_available(self) -> bool:
        return True

    def get_provider_name(self) -> str:
        return "mock_tts"

    async def synthesize(self, text: str, voice_id: str | None = None, speed: float = 1.0) -> tuple[bytes, float]:
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
        return False

    def get_provider_name(self) -> str:
        return "local_piper (unavailable)"

    async def synthesize(self, text: str, voice_id: str | None = None, speed: float = 1.0) -> tuple[bytes, float]:
        raise ValidationError(
            "Local speech synthesis runtime is unavailable. Audio synthesis "
            "requires an installed local TTS runtime (planned for Milestone P11.3.4)."
        )


class AudioSession:
    """State record for a single push-to-talk audio session."""

    def __init__(self, session_id: str, source_device: str, user_consent: bool) -> None:
        self.session_id = session_id
        self.source_device = source_device
        self.user_consent = user_consent
        self.state = AudioCaptureState.CAPTURING
        self.started_at = utc_now()
        self.completed_at: datetime | None = None
        self.transcription: str | None = None
        self.confidence: float = 0.0
        self.delivered: bool = False
        self.delivered_at: datetime | None = None


class AudioSessionManager:
    """Manages explicit push-to-talk audio capture sessions.

    Non-negotiable invariants:
    1. Push-to-talk only: No background or wake-word listening without explicit activation.
    2. Microphone capture starts ONLY after explicit local user consent.
    3. Ephemeral audio memory: Mutable bytearray buffers are zeroed on all lifecycle exits.
    4. Audio data is classified INTERNAL and NEVER sent to cloud services.
    5. Truthful status: microphone_active is True ONLY while genuine capture device is recording.
    6. Concurrency: Enforces single active recording session policy.
    """

    def __init__(
        self,
        stt_adapter: BaseSpeechToTextAdapter | None = None,
        tts_adapter: BaseTextToSpeechAdapter | None = None,
        capture_device: BaseAudioCaptureDevice | None = None,
    ) -> None:
        self.stt_adapter = stt_adapter or LocalWhisperSpeechToTextAdapter()
        self.tts_adapter = tts_adapter or LocalTextToSpeechAdapter()
        self.capture_device = capture_device
        self._active_sessions: dict[str, AudioSession] = {}

    def get_session(self, session_id: str) -> AudioSession | None:
        return self._active_sessions.get(session_id)

    def prune_expired_sessions(self) -> None:
        """Prune stale sessions older than AUDIO_SESSION_TTL_SECONDS to avoid memory leaks."""
        now = utc_now()
        expired_ids = [
            sid
            for sid, s in self._active_sessions.items()
            if (now - s.started_at).total_seconds() > AUDIO_SESSION_TTL_SECONDS
            and s.state != AudioCaptureState.CAPTURING
        ]
        for sid in expired_ids:
            self._active_sessions.pop(sid, None)

    def get_status(self) -> dict[str, Any]:
        mic_active = self.capture_device.is_active() if self.capture_device else False
        phys_connected = self.capture_device.is_available() if self.capture_device else False
        return {
            "enabled": settings.PERCEPTION_ENABLED,
            "physical_microphone_connected": phys_connected,
            "microphone_active": mic_active,
            "active_sessions_count": sum(
                1 for s in self._active_sessions.values() if s.state == AudioCaptureState.CAPTURING
            ),
            "stt_available": self.stt_adapter.is_available() and not getattr(self.stt_adapter, "is_mock", False),
            "stt_provider": self.stt_adapter.get_provider_name(),
            "stt_is_mock": getattr(self.stt_adapter, "is_mock", False),
            "tts_available": self.tts_adapter.is_available() and not getattr(self.tts_adapter, "is_mock", False),
            "tts_provider": self.tts_adapter.get_provider_name(),
            "tts_is_mock": getattr(self.tts_adapter, "is_mock", False),
            "live_speech_capture_available": phys_connected,
            "api_endpoint_available": True,
            "max_capture_seconds": settings.AUDIO_CAPTURE_MAX_SECONDS,
            "max_payload_bytes": settings.AUDIO_MAX_PAYLOAD_BYTES,
            "capture_device": self.capture_device.get_device_info() if self.capture_device else None,
            "graphical_indicator_note": (
                "Observable microphone_active status contract available. Physical capture active."
                if mic_active
                else "Observable microphone_active status contract available. Physical microphone driver is not active."
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

        self.prune_expired_sessions()

        # Enforce single active recording session policy
        active_captures = [
            s
            for s in self._active_sessions.values()
            if s.state in (AudioCaptureState.CAPTURING, AudioCaptureState.PROCESSING)
        ]
        if active_captures:
            raise AudioCaptureError(
                "A recording session is already active. Complete or cancel the active session first."
            )

        clean_device = validate_trusted_source_device(Modality.AUDIO, source_device)
        session_id = f"aud_{uuid.uuid4().hex[:16]}"
        session = AudioSession(session_id=session_id, source_device=clean_device, user_consent=user_consent)

        mic_active = False
        if self.capture_device:
            self.capture_device.start_capture()
            mic_active = self.capture_device.is_active()

        self._active_sessions[session_id] = session

        message = (
            "Push-to-talk recording session initialized (microphone active)."
            if mic_active
            else "Push-to-talk session initialized (hardware driver inactive; client payload required)."
        )

        return AudioSessionStartResponse(
            session_id=session_id,
            state=session.state,
            microphone_active=mic_active,
            started_at=session.started_at,
            message=message,
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

        # Retrieve audio payload into a mutable bytearray
        audio_buffer: bytearray | None = None
        if audio_base64:
            try:
                decoded = base64.b64decode(audio_base64)
                audio_buffer = bytearray(decoded)
            except Exception as e:
                session.state = AudioCaptureState.FAILED
                raise ValidationError(f"Invalid base64 audio payload: {e}") from e
        elif self.capture_device and self.capture_device.is_active():
            audio_buffer = self.capture_device.stop_capture()
        else:
            session.state = AudioCaptureState.FAILED
            raise ValidationError("Audio payload is empty or missing. Transcription requires an audio payload.")

        if not audio_buffer:
            session.state = AudioCaptureState.FAILED
            raise ValidationError("Audio payload is empty or missing. Transcription requires an audio payload.")

        start_t = time.perf_counter()
        elapsed = 0.0

        try:
            # Enforce size limits and structural format validity first
            validate_audio_payload(
                audio_buffer,
                max_bytes=settings.AUDIO_MAX_PAYLOAD_BYTES,
                expected_format=audio_format,
            )

            # Honest capability verification: fail closed if production STT runtime is unavailable
            if not self.stt_adapter.is_available():
                session.state = AudioCaptureState.FAILED
                if self.capture_device and self.capture_device.is_active():
                    self.capture_device.cancel_capture()
                raise ValidationError("Speech transcription runtime is unavailable. No local STT engine is installed.")

            # Transcribe through STT adapter
            transcription, confidence = await self.stt_adapter.transcribe(
                audio_bytes=audio_buffer,
                audio_format=audio_format,
            )
            elapsed = time.perf_counter() - start_t
            session.transcription = transcription
            session.confidence = confidence
            session.state = AudioCaptureState.COMPLETED
            session.completed_at = utc_now()
        except Exception as exc:
            session.state = AudioCaptureState.FAILED
            if not isinstance(exc, ValidationError):
                raise ValidationError(f"Speech transcription failed: {exc}") from exc
            raise
        finally:
            # INVARIANT: Zero mutable audio buffer in place on all exits
            zeroize_bytearray(audio_buffer)

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

        if self.capture_device and self.capture_device.is_active():
            self.capture_device.cancel_capture()

        session.state = AudioCaptureState.CANCELLED
        session.completed_at = utc_now()

        return AudioSessionStartResponse(
            session_id=session_id,
            state=session.state,
            microphone_active=False,
            started_at=session.started_at,
            message="Audio session cancelled and temporary resources zeroed.",
        )

    def shutdown(self) -> None:
        """Cancel all active sessions, release hardware capture devices, and zero memory buffers."""
        for session_id in list(self._active_sessions.keys()):
            try:
                self.cancel_session(session_id)
            except Exception:
                pass
        if self.capture_device and self.capture_device.is_active():
            try:
                self.capture_device.cancel_capture()
            except Exception:
                pass
        self._active_sessions.clear()

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
