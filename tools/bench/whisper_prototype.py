"""PICO Whisper Qualification Prototype (Milestone P11.3.2 Stage 2).

Provides a self-contained Python wrapper for the native C-ABI whisper.cpp shim.
This is a benchmark and qualification prototype ONLY. It does NOT alter or
activate the production LocalWhisperSpeechToTextAdapter.

Invariants:
1. Validates model file size and cryptographic SHA-256 digest before initialization.
2. In-memory PCM float conversion with best-effort buffer zeroization.
3. Zero raw-audio disk persistence.
4. Fail-closed error handling without synthetic production fallback.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import logging
import os
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Known canonical model registry (ADR 0005)
CANONICAL_MODELS: dict[str, dict[str, Any]] = {
    "ggml-base.en-q8_0.bin": {
        "size": 81781811,
        "sha256": "a4d4a0768075e13cfd7e19df3ae2dbc4a68d37d36a7dad45e8410c9a34f8c87e",
    },
    "ggml-tiny.en-q5_1.bin": {
        "size": 32166155,
        "sha256": "c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b",
    },
    "ggml-base.en.bin": {
        "size": 147964211,
        "sha256": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
    },
}

DEFAULT_RUNTIME_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%\PICO\runtime\bin"))
DEFAULT_MODEL_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%\PICO\models\whisper"))


class WhisperPrototypeError(Exception):
    """Base exception for native whisper prototype operations."""


class WhisperModelVerificationError(WhisperPrototypeError):
    """Raised when model weight file is missing, truncated, or fails SHA-256 validation."""


class WhisperRuntimeError(WhisperPrototypeError):
    """Raised when native runtime initialization or inference fails."""


def verify_model_artifact(
    model_path: Path | str,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify model artifact exists and matches its expected size and SHA-256 digest.

    Returns:
        dict with verification metadata.
    Raises:
        WhisperModelVerificationError if file is missing, corrupted, or unrecognized.
    """
    p = Path(model_path)
    if not p.is_file():
        raise WhisperModelVerificationError(f"Model file not found: {p}")

    actual_size = p.stat().st_size
    filename = p.name

    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    actual_sha = h.hexdigest().lower()

    # Determine trusted metadata
    target_size: int | None = None
    target_sha: str | None = None

    if expected_size is not None and expected_sha256 is not None:
        target_size = expected_size
        target_sha = expected_sha256.lower().strip()
    elif filename in CANONICAL_MODELS:
        target_size = CANONICAL_MODELS[filename]["size"]
        target_sha = CANONICAL_MODELS[filename]["sha256"]
    else:
        raise WhisperModelVerificationError(
            f"Unknown model artifact '{filename}' cannot be verified without explicit trusted expected size and SHA-256 metadata."
        )

    if actual_size != target_size:
        raise WhisperModelVerificationError(
            f"Model size mismatch for '{filename}': actual {actual_size} != expected {target_size} bytes."
        )
    if actual_sha != target_sha:
        raise WhisperModelVerificationError(
            f"Cryptographic digest mismatch for '{filename}': actual {actual_sha} != expected {target_sha}."
        )

    return {
        "path": str(p),
        "filename": filename,
        "size_bytes": actual_size,
        "sha256": actual_sha,
        "is_canonical": filename in CANONICAL_MODELS,
    }


class PicoWhisperEngine:
    """Opaque native C-ABI wrapper for whisper.cpp."""

    def __init__(
        self,
        model_path: Path | str,
        runtime_dir: Path | str | None = None,
        verify_digest: bool = True,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        n_threads: int = 4,
    ) -> None:
        if n_threads < 1 or n_threads > 4:
            raise ValueError(f"n_threads must be strictly between 1 and 4 (inclusive), got {n_threads}.")
        self.default_threads = n_threads
        self.model_path = Path(model_path)
        self.runtime_dir = Path(runtime_dir) if runtime_dir else DEFAULT_RUNTIME_DIR

        if verify_digest:
            self.model_meta = verify_model_artifact(
                self.model_path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
        else:
            if not self.model_path.is_file():
                raise WhisperModelVerificationError(f"Model file not found: {self.model_path}")
            self.model_meta = {"path": str(self.model_path)}

        self._ctx: ctypes.c_void_p | None = None
        self._lock = threading.RLock()
        self._lib = self._load_library()
        self._init_context()

    def _load_library(self) -> ctypes.CDLL:
        shim_dll = self.runtime_dir / "pico_whisper_shim.dll"
        if not shim_dll.is_file():
            raise WhisperRuntimeError(
                f"Native whisper shim DLL not found at: {shim_dll}. "
                "Build and stage the runtime using tools/whisper_shim/build_shim.ps1."
            )

        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(self.runtime_dir))

        try:
            lib = ctypes.CDLL(str(shim_dll))
        except Exception as exc:
            raise WhisperRuntimeError(f"Failed to load native DLL '{shim_dll}': {exc}") from exc

        # Set up C signatures
        lib.pico_whisper_version.argtypes = []
        lib.pico_whisper_version.restype = ctypes.c_char_p

        lib.pico_whisper_system_info.argtypes = []
        lib.pico_whisper_system_info.restype = ctypes.c_char_p

        lib.pico_whisper_init.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.pico_whisper_init.restype = ctypes.c_int

        lib.pico_whisper_transcribe.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.pico_whisper_transcribe.restype = ctypes.c_int

        lib.pico_whisper_free.argtypes = [ctypes.c_void_p]
        lib.pico_whisper_free.restype = None

        return lib

    def _init_context(self) -> None:
        ctx = ctypes.c_void_p()
        path_bytes = str(self.model_path).encode("utf-8")
        status = self._lib.pico_whisper_init(path_bytes, ctypes.byref(ctx))
        if status != 0 or not ctx.value:
            raise WhisperRuntimeError(f"Failed to initialize native whisper context (error code {status}).")
        self._ctx = ctx

    @property
    def version(self) -> str:
        ver = self._lib.pico_whisper_version()
        return ver.decode("utf-8") if ver else "unknown"

    @property
    def system_info(self) -> str:
        info = self._lib.pico_whisper_system_info()
        return info.decode("utf-8") if info else ""

    def transcribe_samples(
        self,
        samples: list[float] | tuple[float, ...] | Any,
        n_threads: int | None = None,
        return_breakdown: bool = False,
    ) -> tuple[str, float] | tuple[str, float, dict[str, float]]:
        """Transcribe normalized float32 samples in RAM.

        Returns (transcript_text, wall_clock_seconds) by default, or
        (transcript_text, wall_clock_seconds, breakdown_dict) if return_breakdown=True.
        """
        effective_threads = self.default_threads if n_threads is None else n_threads
        if effective_threads < 1 or effective_threads > 4:
            raise ValueError(f"n_threads must be strictly between 1 and 4 (inclusive), got {effective_threads}.")

        n_samples = len(samples)
        if n_samples == 0:
            if return_breakdown:
                return "", 0.0, {
                    "c_buf_prep_seconds": 0.0,
                    "native_inference_seconds": 0.0,
                    "total_seconds": 0.0,
                }
            return "", 0.0

        t_entry = time.perf_counter()
        t_cbuf_start = time.perf_counter()
        c_float_array = (ctypes.c_float * n_samples)(*samples)
        c_buf_prep_seconds = time.perf_counter() - t_cbuf_start

        buf_size = 16384
        out_buf = ctypes.create_string_buffer(buf_size)
        out_len = ctypes.c_size_t()

        native_elapsed = 0.0
        try:
            with self._lock:
                if self._ctx is None:
                    raise WhisperRuntimeError("Context is closed or uninitialized.")

                t_infer_start = time.perf_counter()
                status = self._lib.pico_whisper_transcribe(
                    self._ctx,
                    c_float_array,
                    n_samples,
                    effective_threads,
                    out_buf,
                    buf_size,
                    ctypes.byref(out_len),
                )
                native_elapsed = time.perf_counter() - t_infer_start

            if status != 0:
                raise WhisperRuntimeError(f"Native transcription failed with error code {status}.")

            text = out_buf.value.decode("utf-8", errors="replace")
            total_seconds = time.perf_counter() - t_entry

            if return_breakdown:
                breakdown = {
                    "c_buf_prep_seconds": c_buf_prep_seconds,
                    "native_inference_seconds": native_elapsed,
                    "total_seconds": total_seconds,
                }
                return text, native_elapsed, breakdown
            return text, native_elapsed
        finally:
            # Best-effort memory zeroization for application-owned mutable buffers:
            # 1. In-place zeroize ctypes C float array
            for i in range(n_samples):
                c_float_array[i] = 0.0
            # 2. If caller passed a mutable list, zeroize elements
            if isinstance(samples, list):
                for i in range(len(samples)):
                    samples[i] = 0.0

    def transcribe_pcm(
        self,
        samples: list[float] | tuple[float, ...] | Any,
        n_threads: int | None = None,
    ) -> str:
        """Convenience method returning transcript text for normalized PCM float samples."""
        text, _ = self.transcribe_samples(samples, n_threads=n_threads)
        return text

    def transcribe_wav_bytes(
        self,
        wav_bytes: bytes | bytearray,
        n_threads: int | None = None,
        return_breakdown: bool = False,
    ) -> tuple[str, float, float] | tuple[str, float, float, dict[str, float]]:
        """Extract and transcribe 16 kHz 16-bit mono PCM from a WAV byte buffer.

        Returns (transcript_text, end_to_end_seconds, audio_duration_seconds) by default,
        or (transcript_text, end_to_end_seconds, audio_duration_seconds, breakdown_dict) if return_breakdown=True.
        """
        effective_threads = self.default_threads if n_threads is None else n_threads
        if effective_threads < 1 or effective_threads > 4:
            raise ValueError(f"n_threads must be strictly between 1 and 4 (inclusive), got {effective_threads}.")

        t_entry = time.perf_counter()
        t0 = time.perf_counter()
        with wave.open(io.BytesIO(bytes(wav_bytes)), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            n_frames = wf.getnframes()
            raw_pcm = wf.readframes(n_frames)
        wav_parse_seconds = time.perf_counter() - t0

        if channels != 1 or sample_rate != 16000 or sampwidth != 2:
            raise WhisperRuntimeError(
                f"WAV payload non-compliant: channels={channels} (req 1), "
                f"rate={sample_rate} (req 16000), width={sampwidth} (req 2)."
            )

        duration = n_frames / sample_rate
        num_samples = n_frames

        t1 = time.perf_counter()
        int16_samples = struct.unpack(f"<{num_samples}h", raw_pcm)
        # Normalize int16 [-32768, 32767] to float32 [-1.0, 1.0]
        float_samples = [s / 32768.0 for s in int16_samples]
        pcm_norm_seconds = time.perf_counter() - t1

        try:
            transcript, native_proc_time, sample_breakdown = self.transcribe_samples(
                float_samples,
                n_threads=effective_threads,
                return_breakdown=True,
            )
            total_end_to_end_seconds = time.perf_counter() - t_entry

            if return_breakdown:
                breakdown = {
                    "wav_parse_seconds": wav_parse_seconds,
                    "pcm_norm_seconds": pcm_norm_seconds,
                    "c_buf_prep_seconds": sample_breakdown["c_buf_prep_seconds"],
                    "native_inference_seconds": native_proc_time,
                    "end_to_end_seconds": total_end_to_end_seconds,
                }
                return transcript, total_end_to_end_seconds, duration, breakdown
            return transcript, total_end_to_end_seconds, duration
        finally:
            # Best-effort scrub of intermediate mutable float list
            for i in range(len(float_samples)):
                float_samples[i] = 0.0
            # If caller passed a mutable bytearray, zeroize it
            if isinstance(wav_bytes, bytearray):
                for i in range(len(wav_bytes)):
                    wav_bytes[i] = 0

    def close(self) -> None:
        """Deterministic cleanup of native memory."""
        with self._lock:
            if self._ctx is not None:
                ctx_to_free = self._ctx
                self._ctx = None
                if hasattr(self, "_lib") and self._lib is not None:
                    self._lib.pico_whisper_free(ctx_to_free)

    def __enter__(self) -> PicoWhisperEngine:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
        except Exception:
            pass
