"""Audio payload and format validation for PICO voice runtime (Milestone P11.3).

Enforces:
1. Maximum size limit (default 10 MB) to prevent memory exhaustion / DoS.
2. Structural RIFF/WAVE verification without synthetic bypasses:
   - Declared RIFF and WAVE magic headers.
   - 'fmt ' chunk presence, size, and PCM encoding validation (format 1 only).
   - Valid channel count (1 or 2 channels for speech).
   - Supported sample rate (8000 Hz to 48000 Hz).
   - Consistent block alignment and byte rate calculations.
   - 'data' chunk presence, declared bounds, and truncation detection.
   - Minimum (0.05s) and maximum (120s) recording duration limits.
3. Safe integer and corrupted chunk handling without crashes or partial parsing.
"""

from __future__ import annotations

import struct

from pico.core.errors import ValidationError

MIN_WAV_HEADER_BYTES = 44
MAX_AUDIO_PAYLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_DURATION_SECONDS = 120.0
MIN_DURATION_SECONDS = 0.05


def validate_audio_payload(
    audio_bytes: bytes | bytearray,
    max_bytes: int = MAX_AUDIO_PAYLOAD_BYTES,
    expected_format: str = "wav",
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
) -> dict[str, float | int | str]:
    """Validate audio payload size, structural integrity, and format.

    Args:
        audio_bytes: Raw binary payload.
        max_bytes: Maximum allowed byte length.
        expected_format: Expected container/audio format ('wav', 'pcm', 'raw').
        max_duration_seconds: Maximum allowed duration in seconds.

    Returns:
        Dictionary of validated audio metadata (channels, sample_rate, duration_seconds, etc.).

    Raises:
        ValidationError: If the payload is empty, corrupted, truncated, or out of bounds.
    """
    if not audio_bytes:
        raise ValidationError("Audio payload is empty.")

    payload_len = len(audio_bytes)
    if payload_len > max_bytes:
        raise ValidationError(f"Audio payload size ({payload_len} bytes) exceeds limit of {max_bytes} bytes.")

    fmt = (expected_format or "").lower().strip()
    if fmt == "wav":
        return _validate_wav_structure(audio_bytes, max_duration_seconds=max_duration_seconds)
    elif fmt in ("pcm", "raw"):
        if payload_len < 2:
            raise ValidationError("PCM audio payload is too small.")
        if payload_len % 2 != 0:
            raise ValidationError("PCM 16-bit audio payload must have an even byte count.")
        return {
            "format": fmt,
            "byte_count": payload_len,
        }
    else:
        if payload_len < 16:
            raise ValidationError(f"Audio payload too small for format '{fmt}'.")
        return {
            "format": fmt,
            "byte_count": payload_len,
        }


def _validate_wav_structure(
    data: bytes | bytearray,
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
) -> dict[str, float | int | str]:
    """Strict parser for uncompressed PCM RIFF/WAVE files."""
    data_len = len(data)
    if data_len < MIN_WAV_HEADER_BYTES:
        raise ValidationError(
            f"Audio payload too short for valid WAV format ({data_len} bytes < {MIN_WAV_HEADER_BYTES} bytes)."
        )

    # 1. RIFF chunk header
    riff_id = data[:4]
    if riff_id != b"RIFF":
        raise ValidationError("Audio payload missing valid 'RIFF' header for WAV format.")

    declared_riff_size = struct.unpack("<I", data[4:8])[0]
    if declared_riff_size < 36:
        raise ValidationError(f"Corrupted WAV: declared RIFF size too small ({declared_riff_size} bytes).")

    # Guard against truncated file: data must contain at least the declared size + 8 header bytes
    if data_len < declared_riff_size + 8:
        raise ValidationError(
            f"Truncated WAV payload: file size ({data_len} bytes) is less than declared RIFF size "
            f"({declared_riff_size + 8} bytes)."
        )

    wave_id = data[8:12]
    if wave_id != b"WAVE":
        raise ValidationError("Audio payload missing 'WAVE' format identifier.")

    # 2. Iterate chunks
    offset = 12
    fmt_found = False
    data_found = False

    channels = 0
    sample_rate = 0
    bits_per_sample = 0
    block_align = 0
    audio_data_size = 0

    while offset + 8 <= data_len:
        chunk_id = bytes(data[offset : offset + 4])
        chunk_size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        offset += 8

        # Integer overflow / bounds guard
        if offset + chunk_size > data_len:
            raise ValidationError(
                f"Malformed WAV chunk '{chunk_id.decode('ascii', errors='replace')}': "
                f"declared size {chunk_size} exceeds file length."
            )

        if chunk_id == b"fmt ":
            fmt_found = True
            if chunk_size < 16:
                raise ValidationError(f"Malformed 'fmt ' chunk: size {chunk_size} < 16 bytes.")

            audio_format = struct.unpack("<H", data[offset : offset + 2])[0]
            if audio_format != 1:
                # 1 = WAVE_FORMAT_PCM
                raise ValidationError(
                    f"Unsupported WAV encoding format: {audio_format}. Only uncompressed PCM (format 1) is accepted."
                )

            channels = struct.unpack("<H", data[offset + 2 : offset + 4])[0]
            if channels < 1 or channels > 2:
                raise ValidationError(
                    f"Unsupported WAV channel count: {channels}. Only mono (1) or stereo (2) speech audio is accepted."
                )

            sample_rate = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
            if sample_rate < 8000 or sample_rate > 48000:
                raise ValidationError(
                    f"Unsupported WAV sample rate: {sample_rate} Hz. Supported range is 8000 Hz to 48000 Hz."
                )

            byte_rate = struct.unpack("<I", data[offset + 8 : offset + 12])[0]
            block_align = struct.unpack("<H", data[offset + 12 : offset + 14])[0]
            bits_per_sample = struct.unpack("<H", data[offset + 14 : offset + 16])[0]

            if bits_per_sample not in (8, 16, 24, 32):
                raise ValidationError(f"Unsupported WAV bits per sample: {bits_per_sample}.")

            # Alignment consistency checks
            expected_block_align = channels * (bits_per_sample // 8)
            if block_align != expected_block_align:
                raise ValidationError(
                    f"Inconsistent WAV block alignment: declared {block_align}, expected {expected_block_align}."
                )

            expected_byte_rate = sample_rate * block_align
            if byte_rate != expected_byte_rate:
                raise ValidationError(
                    f"Inconsistent WAV byte rate: declared {byte_rate}, expected {expected_byte_rate}."
                )

        elif chunk_id == b"data":
            if not fmt_found:
                raise ValidationError("Malformed WAV: 'data' chunk appeared before 'fmt ' chunk.")

            data_found = True
            audio_data_size = chunk_size

            if audio_data_size == 0:
                raise ValidationError("WAV 'data' chunk is empty (0 bytes).")

            # Check that available bytes in payload match declared data size
            available_data = data_len - offset
            if available_data < audio_data_size:
                raise ValidationError(
                    f"Truncated WAV audio data: declared {audio_data_size} bytes, available {available_data} bytes."
                )

        # Chunks are padded to word boundary (even size)
        padding = chunk_size % 2
        offset += chunk_size + padding

    if not fmt_found:
        raise ValidationError("WAV payload missing required 'fmt ' chunk.")

    if not data_found:
        raise ValidationError("WAV payload missing required 'data' chunk.")

    # 3. Duration calculations
    num_samples = audio_data_size // block_align
    duration_seconds = num_samples / sample_rate

    if duration_seconds < MIN_DURATION_SECONDS:
        raise ValidationError(
            f"Audio recording duration ({duration_seconds:.3f}s) is too short "
            f"(minimum {MIN_DURATION_SECONDS}s required)."
        )

    if duration_seconds > max_duration_seconds:
        raise ValidationError(
            f"Audio recording duration ({duration_seconds:.1f}s) exceeds maximum allowed limit "
            f"({max_duration_seconds:.1f}s)."
        )

    return {
        "format": "wav",
        "channels": channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits_per_sample,
        "duration_seconds": duration_seconds,
        "audio_data_size": audio_data_size,
    }
