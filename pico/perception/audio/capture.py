"""Local audio capture driver abstraction for PICO voice runtime (Milestone P11.3).

Provides an extensible, local-first hardware abstraction for microphone capture:
- BaseAudioCaptureDevice: formal interface for recording lifecycle.
- MockAudioCaptureDevice: deterministic synthetic audio buffer capture for testing.
- LocalAudioCaptureDevice: local host microphone capture via sounddevice.

Security & Invariants:
1. Capture starts ONLY after explicit local user consent.
2. Mutable audio buffers are zeroed and released in finally blocks on success, failure,
   cancellation, and shutdown; zero raw audio files are persisted to disk.
3. Microphone active state is reported truthfully based on confirmed driver activation.
4. Bounded frame buffering prevents unbounded memory growth.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import wave
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from pico.core.errors import ValidationError

logger = logging.getLogger(__name__)

# Maximum audio payload size in memory (10 MB ~ 5 minutes of 16kHz 16-bit mono PCM)
MAX_CAPTURE_BUFFER_BYTES = 10 * 1024 * 1024


def zeroize_bytearray(buf: bytearray | None) -> None:
    """Best-effort in-place zeroization of mutable Python bytearray buffers.

    Notice: While this reliably zeroes the memory owned by this mutable buffer,
    Python's runtime, garbage collector, OS memory manager, and native PortAudio
    drivers may retain transient copies or immutable references. Guaranteed
    hardware-level zeroization of all process memory is not achievable in userland Python.
    """
    if buf is not None:
        try:
            buf[:] = b"\x00" * len(buf)
        except Exception:
            pass
        buf.clear()


class AudioCaptureError(ValidationError):
    """Raised when an audio capture hardware or driver operation fails."""


class BaseAudioCaptureDevice(ABC):
    """Abstract base class for all audio capture devices in PICO."""

    @abstractmethod
    def is_available(self) -> bool:
        """Report whether the underlying audio hardware and driver are available."""

    @abstractmethod
    def is_active(self) -> bool:
        """Report whether microphone recording is currently active in hardware/driver."""

    @abstractmethod
    def start_capture(self, sample_rate: int = 16000, channels: int = 1) -> None:
        """Start microphone capture session. Must raise if already active or unavailable."""

    @abstractmethod
    def stop_capture(self) -> bytearray:
        """Stop microphone capture and return captured audio as a mutable WAV bytearray."""

    @abstractmethod
    def cancel_capture(self) -> None:
        """Abort capture immediately, discarding and zeroing all buffered audio."""

    @abstractmethod
    def get_device_info(self) -> dict[str, Any]:
        """Return device identification, driver type, and configuration metadata."""


class MockAudioCaptureDevice(BaseAudioCaptureDevice):
    """Synthetic in-memory audio capture device for reproducible, offline tests.

    Never accesses physical microphone hardware. Allows test suites to inject
    deterministic audio buffers or simulate driver failures without hardware.
    """

    def __init__(
        self,
        device_name: str = "mock:synthetic_mic",
        simulate_hardware_available: bool = True,
    ) -> None:
        self.device_name = device_name
        self.simulate_hardware_available = simulate_hardware_available
        self._is_active = False
        self._buffer = bytearray()
        self._sample_rate = 16000
        self._channels = 1
        self._start_time: float | None = None
        self._injected_payload: bytearray | None = None

    def is_available(self) -> bool:
        return self.simulate_hardware_available

    def is_active(self) -> bool:
        return self._is_active

    def inject_payload(self, data: bytes | bytearray) -> None:
        """Pre-load a synthetic audio payload to be returned on next stop_capture."""
        self._injected_payload = bytearray(data)

    def start_capture(self, sample_rate: int = 16000, channels: int = 1) -> None:
        if not self.simulate_hardware_available:
            raise AudioCaptureError("Audio capture device is unavailable or disconnected.")
        if self._is_active:
            raise AudioCaptureError("Audio capture device is already active.")

        self._sample_rate = sample_rate
        self._channels = channels
        self._buffer = bytearray()
        self._is_active = True
        self._start_time = time.monotonic()

    def stop_capture(self) -> bytearray:
        if not self._is_active:
            raise AudioCaptureError("Audio capture device is not currently recording.")

        self._is_active = False
        duration = time.monotonic() - (self._start_time or time.monotonic())
        self._start_time = None

        if self._injected_payload is not None:
            result = bytearray(self._injected_payload)
            zeroize_bytearray(self._injected_payload)
            self._injected_payload = None
            zeroize_bytearray(self._buffer)
            return result

        # Synthesize a standard 16-bit PCM mono WAV header with silence frames
        out = io.BytesIO()
        num_frames = max(1600, int(self._sample_rate * min(max(duration, 0.1), 10.0)))
        with wave.open(out, "wb") as wf:
            wf.setnchannels(self._channels)
            wf.setsampwidth(2)  # 16-bit PCM
            wf.setframerate(self._sample_rate)
            wf.writeframes(b"\x00\x00" * num_frames)

        raw_wav = bytearray(out.getvalue())
        zeroize_bytearray(self._buffer)
        return raw_wav

    def cancel_capture(self) -> None:
        """Cancel capture and zero internal memory buffers immediately."""
        self._is_active = False
        self._start_time = None
        zeroize_bytearray(self._injected_payload)
        self._injected_payload = None
        zeroize_bytearray(self._buffer)

    def get_device_info(self) -> dict[str, Any]:
        return {
            "device_name": self.device_name,
            "driver_type": "mock_synthetic",
            "is_available": self.simulate_hardware_available,
            "is_active": self._is_active,
            "sample_rate": self._sample_rate,
            "channels": self._channels,
        }


class LocalAudioCaptureDevice(BaseAudioCaptureDevice):
    """Local-first audio capture device performing genuine host OS microphone capture via sounddevice.

    Invariants:
    1. Lazy initialization: does not open stream or hold device locks until start_capture.
    2. Bounded buffering: frames are collected into a pre-bounded buffer up to MAX_CAPTURE_BUFFER_BYTES.
    3. Thread-safe lifecycle transitions protected by reentrant lock.
    4. Mutable frame buffer is actively zeroed upon cancellation, stop, and shutdown.
    5. Clean error propagation: PortAudio hardware/permission errors raise AudioCaptureError.
    """

    def __init__(
        self,
        device_index: int | None = None,
        max_buffer_bytes: int = MAX_CAPTURE_BUFFER_BYTES,
        stream_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.device_index = device_index
        self.max_buffer_bytes = max_buffer_bytes
        self._stream_factory = stream_factory
        self._is_active = False
        self._sample_rate = 16000
        self._channels = 1
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._stream: Any = None
        self._overflow_dropped = False

    def is_available(self) -> bool:
        """Check if sounddevice is installed and at least one physical input device is detected."""
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            if self.device_index is not None:
                dev = sd.query_devices(self.device_index)
                return bool(isinstance(dev, dict) and dev.get("max_input_channels", 0) > 0)

            # Check if any input device is available
            if isinstance(devices, list):
                return any(d.get("max_input_channels", 0) > 0 for d in devices if isinstance(d, dict))
            elif isinstance(devices, dict):
                return bool(devices.get("max_input_channels", 0) > 0)
            return False
        except Exception:
            return False

    def is_active(self) -> bool:
        with self._lock:
            return self._is_active

    def _audio_callback(self, indata: bytes, frames: int, time_info: Any, status: Any) -> None:
        """Callback executed by audio driver thread for each captured PCM audio block."""
        if status:
            logger.debug("Audio stream callback status warning: %s", status)

        with self._lock:
            if not self._is_active:
                return

            if len(self._buffer) + len(indata) <= self.max_buffer_bytes:
                self._buffer.extend(indata)
            else:
                self._overflow_dropped = True

    def start_capture(self, sample_rate: int = 16000, channels: int = 1) -> None:
        with self._lock:
            if self._is_active:
                raise AudioCaptureError("Microphone is already actively recording.")

            if not self.is_available() and self._stream_factory is None:
                raise AudioCaptureError("Local microphone capture is unavailable: no input device or driver error.")

            self._sample_rate = sample_rate
            self._channels = channels
            self._buffer = bytearray()
            self._overflow_dropped = False

            try:
                if self._stream_factory is not None:
                    # Use injected stream factory (e.g. for deterministic unit testing)
                    self._stream = self._stream_factory(
                        samplerate=sample_rate,
                        channels=channels,
                        dtype="int16",
                        callback=self._audio_callback,
                    )
                else:
                    import sounddevice as sd

                    self._stream = sd.RawInputStream(
                        samplerate=sample_rate,
                        channels=channels,
                        dtype="int16",
                        device=self.device_index,
                        callback=self._audio_callback,
                    )

                self._stream.start()
                self._is_active = True
            except Exception as e:
                self._is_active = False
                zeroize_bytearray(self._buffer)
                if self._stream is not None:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                raise AudioCaptureError(f"Failed to open audio input stream: {e}") from e

    def stop_capture(self) -> bytearray:
        with self._lock:
            if not self._is_active:
                raise AudioCaptureError("Microphone is not currently recording.")

            self._is_active = False

            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception as e:
                    logger.warning("Error stopping audio input stream: %s", e)
                finally:
                    self._stream = None

            raw_pcm = bytearray(self._buffer)
            zeroize_bytearray(self._buffer)

            # Package 16-bit PCM frames into a standard WAV format container
            out = io.BytesIO()
            try:
                with wave.open(out, "wb") as wf:
                    wf.setnchannels(self._channels)
                    wf.setsampwidth(2)  # 16-bit PCM
                    wf.setframerate(self._sample_rate)
                    wf.writeframes(raw_pcm)
                return bytearray(out.getvalue())
            finally:
                zeroize_bytearray(raw_pcm)

    def cancel_capture(self) -> None:
        with self._lock:
            self._is_active = False

            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                finally:
                    self._stream = None

            zeroize_bytearray(self._buffer)

    def get_device_info(self) -> dict[str, Any]:
        return {
            "device_name": f"sounddevice_input_{self.device_index if self.device_index is not None else 'default'}",
            "driver_type": "sounddevice",
            "is_available": self.is_available(),
            "is_active": self._is_active,
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "max_buffer_bytes": self.max_buffer_bytes,
        }
