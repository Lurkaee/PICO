import base64
import struct
from datetime import datetime, timedelta, timezone
from typing import Any

from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.perception.classification import (
    compute_trusted_perception_classification,
    validate_trusted_source_device,
)
from pico.perception.contracts import (
    Modality,
    MotionIngestRequest,
    MovementDirection,
    MovementInterpretationRequest,
    MovementInterpretationResponse,
    MovementMagnitude,
    MovementProgression,
    NormalizedPerceptionEvent,
    RetentionPolicy,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def decode_frame_grayscale(data: bytes, image_format: str) -> tuple[int, int, list[int]]:
    """Decode supported frame formats (PPM or uncompressed 24-bit BMP) to grayscale pixels.

    Strictly validates headers, bounds, and payload integrity.
    Rejects malformed, unsupported, or arbitrary compressed payloads (such as PNG/JPEG).
    """
    if not data:
        raise ValidationError("Frame payload cannot be empty.")
    if len(data) > settings.VISION_MAX_IMAGE_BYTES:
        raise ValidationError(
            f"Frame payload ({len(data)} bytes) exceeds maximum allowed size of {settings.VISION_MAX_IMAGE_BYTES} bytes."
        )

    fmt = (image_format or "").strip().lower()

    # 1. Reject unsupported formats explicitly before processing
    if fmt in ("png", "jpg", "jpeg", "webp", "gif"):
        raise ValidationError(
            f"Unsupported format '{fmt}' for direct frame-differencing. Supported decoded formats are: 'ppm', 'bmp'. "
            f"PNG/JPEG decoding requires external imaging libraries not available in base runtime."
        )

    # 2. PPM decoder (P6 binary or P3 ascii)
    if fmt == "ppm" or data.startswith(b"P6") or data.startswith(b"P3"):
        tokens: list[bytes] = []
        pos = 0
        data_len = len(data)
        while pos < data_len and len(tokens) < 4:
            # skip whitespace
            while pos < data_len and data[pos : pos + 1] in b" \t\r\n":
                pos += 1
            if pos >= data_len:
                break
            # check for comment
            if data[pos : pos + 1] == b"#":
                while pos < data_len and data[pos : pos + 1] not in b"\r\n":
                    pos += 1
                continue
            token_start = pos
            while pos < data_len and data[pos : pos + 1] not in b" \t\r\n#":
                pos += 1
            tokens.append(data[token_start:pos])

        if len(tokens) < 4:
            raise ValidationError("Malformed PPM frame: incomplete header.")

        magic = tokens[0]
        if magic not in (b"P6", b"P3"):
            raise ValidationError(f"Invalid PPM magic '{magic.decode('ascii', errors='replace')}'. Expected P6 or P3.")

        try:
            width = int(tokens[1])
            height = int(tokens[2])
            maxval = int(tokens[3])
        except ValueError:
            raise ValidationError("Malformed PPM header: invalid dimensions or maxval.")

        if width <= 0 or height <= 0 or maxval <= 0:
            raise ValidationError(f"Invalid PPM dimensions: {width}x{height}, maxval={maxval}.")

        if width > settings.VISION_MAX_DIMENSION or height > settings.VISION_MAX_DIMENSION:
            raise ValidationError(
                f"PPM dimensions ({width}x{height}) exceed maximum allowed dimension of {settings.VISION_MAX_DIMENSION}px."
            )

        # Skip single whitespace delimiter after maxval
        while pos < data_len and data[pos : pos + 1] in b" \t\r\n":
            pos += 1

        pixel_data = data[pos:]
        expected_pixels = width * height

        if magic == b"P6":
            expected_bytes = expected_pixels * 3
            if len(pixel_data) < expected_bytes:
                raise ValidationError(
                    f"Malformed PPM (P6): truncated pixel data. Expected {expected_bytes} bytes, got {len(pixel_data)}."
                )
            pixels: list[int] = []
            for i in range(0, expected_bytes, 3):
                r, g, b = pixel_data[i], pixel_data[i + 1], pixel_data[i + 2]
                pixels.append(int(0.299 * r + 0.587 * g + 0.114 * b))
            return width, height, pixels
        else:
            # P3 ASCII
            pixel_tokens = pixel_data.split()
            if len(pixel_tokens) < expected_pixels * 3:
                raise ValidationError("Malformed PPM (P3): truncated ASCII pixel tokens.")
            pixels = []
            try:
                for i in range(0, expected_pixels * 3, 3):
                    r = int(pixel_tokens[i])
                    g = int(pixel_tokens[i + 1])
                    b = int(pixel_tokens[i + 2])
                    pixels.append(int(0.299 * r + 0.587 * g + 0.114 * b))
            except ValueError:
                raise ValidationError("Malformed PPM (P3): non-numeric pixel values.")
            return width, height, pixels

    # 3. BMP decoder (uncompressed 24-bit BMP)
    if fmt == "bmp" or data.startswith(b"BM"):
        if not data.startswith(b"BM"):
            raise ValidationError("Malformed BMP payload: missing BM signature.")
        if len(data) < 54:
            raise ValidationError("Malformed BMP payload: truncated header (less than 54 bytes).")

        offset = struct.unpack("<I", data[10:14])[0]
        dib_header_size = struct.unpack("<I", data[14:18])[0]
        if dib_header_size < 40:
            raise ValidationError("Unsupported BMP header format (expected standard BITMAPINFOHEADER >= 40 bytes).")

        width, height = struct.unpack("<ii", data[18:26])
        planes, bpp = struct.unpack("<HH", data[26:30])
        compression = struct.unpack("<I", data[30:34])[0]

        if compression != 0:
            raise ValidationError(
                f"Compressed BMP format (compression={compression}) is unsupported for frame differencing. "
                f"Provide uncompressed 24-bit BMP or PPM."
            )
        if bpp != 24:
            raise ValidationError(
                f"Unsupported BMP bit depth: {bpp}-bit. Frame differencing requires uncompressed 24-bit BMP or PPM."
            )
        if width <= 0 or height == 0:
            raise ValidationError(f"Invalid BMP dimensions: {width}x{height}.")

        abs_height = abs(height)
        if width > settings.VISION_MAX_DIMENSION or abs_height > settings.VISION_MAX_DIMENSION:
            raise ValidationError(
                f"BMP dimensions ({width}x{abs_height}) exceed maximum allowed dimension of {settings.VISION_MAX_DIMENSION}px."
            )

        row_stride = (width * 3 + 3) & ~3
        total_needed = offset + row_stride * abs_height
        if len(data) < total_needed:
            raise ValidationError(
                f"Malformed BMP payload: truncated pixel data. Expected {total_needed} bytes, got {len(data)}."
            )

        pixels = [0] * (width * abs_height)
        bottom_up = height > 0

        for row_idx in range(abs_height):
            y = (abs_height - 1 - row_idx) if bottom_up else row_idx
            row_start = offset + row_idx * row_stride
            for x in range(width):
                px_offset = row_start + x * 3
                b = data[px_offset]
                g = data[px_offset + 1]
                r = data[px_offset + 2]
                pixels[y * width + x] = int(0.299 * r + 0.587 * g + 0.114 * b)

        return width, abs_height, pixels

    # 4. Any other unrecognized format
    raise ValidationError(
        f"Unsupported or unrecognized format '{fmt}' for frame-differencing. Supported decoded formats are: 'ppm', 'bmp'."
    )


class MotionAdapter:
    """Multi-tiered motion detection and movement interpretation engine.

    Tier 1: Motion Detection (PIR triggers and frame-differencing pixel changes).
    Tier 2: Movement Interpretation (Centroid displacement, direction, and magnitude).
    Tier 3: Cross-Modal Interpretation (Correlating motion with environmental and audio signals).
    """

    def __init__(self) -> None:
        self._last_event_time: dict[str, datetime] = {}

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": settings.PERCEPTION_ENABLED,
            "physical_pir_connected": False,  # Physical hardware GPIO PIR sensor not attached
            "pir_available": False,  # Physical PIR sensor unavailable
            "frame_differencing_available": True,
            "motion_api_available": True,
            "cooldown_seconds": settings.MOTION_COOLDOWN_SECONDS,
            "supported_frame_formats": ["ppm", "bmp"],
            "tier1_motion_detection": "active",
            "tier2_movement_interpretation": "active",
            "tier3_cross_modal_correlation": "active",
            "privacy_note": "No facial or biometric identity tracking. Frame buffers scrubbed after differencing.",
        }

    # --- Tier 1: Motion Detection (PIR) ---
    def ingest_pir_motion(self, request: MotionIngestRequest) -> NormalizedPerceptionEvent:
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        clean_device = validate_trusted_source_device(Modality.MOTION, request.source_device)
        effective_class = compute_trusted_perception_classification(
            modality=Modality.MOTION,
            source_device=clean_device,
            caller_classification=request.data_classification,
        )

        now = utc_now()
        last_t = self._last_event_time.get(clean_device)
        is_cooldown_suppressed = False
        remaining_seconds = 0.0

        if last_t and (now - last_t).total_seconds() < settings.MOTION_COOLDOWN_SECONDS:
            is_cooldown_suppressed = True
            remaining_seconds = round(settings.MOTION_COOLDOWN_SECONDS - (now - last_t).total_seconds(), 2)
        else:
            self._last_event_time[clean_device] = now

        # Invariant: PIR trigger detects motion presence but does NOT claim direction or identity
        event = NormalizedPerceptionEvent(
            modality=Modality.MOTION,
            event_type="motion_detected",
            source_device_id=clean_device,
            data_classification=effective_class,
            confidence=request.confidence,
            raw_payload_scrubbed=True,
            structured_data={
                "sensor_type": "pir",
                "motion_detected": request.motion_detected,
                "duration_ms": request.measured_duration_ms,
                "assertion_limits": "PIR detects motion presence only; direction, activity, and identity are unasserted.",
                "cooldown_suppressed": is_cooldown_suppressed,
                "cooldown_seconds_remaining": remaining_seconds,
                "persisted": not is_cooldown_suppressed,
            },
            provenance=f"sensor:{clean_device}",
            retention_policy=RetentionPolicy.SHORT_TERM_AUDIT,
        )
        return event

    # --- Tier 1 & 2: Frame Differencing & Movement Interpretation ---
    def interpret_movement_between_frames(
        self, request: MovementInterpretationRequest
    ) -> tuple[MovementInterpretationResponse, NormalizedPerceptionEvent]:
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        clean_device = validate_trusted_source_device(Modality.MOTION, request.source_device)
        effective_class = compute_trusted_perception_classification(
            modality=Modality.MOTION,
            source_device=clean_device,
        )

        try:
            bytes_a = base64.b64decode(request.frame_a_base64)
            bytes_b = base64.b64decode(request.frame_b_base64)
        except Exception as exc:
            raise ValidationError(f"Invalid base64 frame payload: {exc}")

        w_a, h_a, pixels_a = decode_frame_grayscale(bytes_a, request.image_format)
        w_b, h_b, pixels_b = decode_frame_grayscale(bytes_b, request.image_format)

        # Release raw binary payloads
        del bytes_a, bytes_b

        if w_a != w_b or h_a != h_b or len(pixels_a) != len(pixels_b) or len(pixels_a) == 0:
            raise ValidationError(
                f"Frame dimensions must match: Frame A ({w_a}x{h_a}) vs Frame B ({w_b}x{h_b})."
            )

        total_pixels = len(pixels_a)
        changed_pixels = 0
        diff_threshold = int(request.sensitivity_threshold * 255)

        # Calculate centroids of changed regions
        sum_x_a, sum_y_a, count_a = 0, 0, 0
        sum_x_b, sum_y_b, count_b = 0, 0, 0

        for idx in range(total_pixels):
            val_a = pixels_a[idx]
            val_b = pixels_b[idx]
            diff = abs(val_b - val_a)
            if diff > diff_threshold:
                changed_pixels += 1
                x = idx % w_a
                y = idx // w_a
                sum_x_b += x
                sum_y_b += y
                count_b += 1

            # Track foreground in frame A for comparison centroid
            if val_a > diff_threshold:
                x = idx % w_a
                y = idx // w_a
                sum_x_a += x
                sum_y_a += y
                count_a += 1

        delta_ratio = round(changed_pixels / total_pixels, 4)
        motion_detected = delta_ratio >= request.sensitivity_threshold

        centroid_a = (sum_x_a // count_a, sum_y_a // count_a) if count_a > 0 else (w_a // 2, h_a // 2)
        centroid_b = (sum_x_b // count_b, sum_y_b // count_b) if count_b > 0 else centroid_a

        # Determine Tier 2 Movement Characteristics
        direction = MovementDirection.STATIONARY
        magnitude = MovementMagnitude.NONE
        progression = MovementProgression.STOPPED

        if motion_detected:
            # Magnitude classification
            if delta_ratio < 0.05:
                magnitude = MovementMagnitude.SUBTLE
            elif delta_ratio <= 0.25:
                magnitude = MovementMagnitude.MODERATE
            else:
                magnitude = MovementMagnitude.SIGNIFICANT

            # Centroid displacement vector
            dx = centroid_b[0] - centroid_a[0]
            dy = centroid_b[1] - centroid_a[1]
            margin_x = max(2, w_a // 20)
            margin_y = max(2, h_a // 20)

            if abs(dx) > margin_x or abs(dy) > margin_y:
                progression = MovementProgression.CONTINUING
                if abs(dx) >= abs(dy):
                    direction = MovementDirection.RIGHT if dx > 0 else MovementDirection.LEFT
                else:
                    direction = MovementDirection.DOWN if dy > 0 else MovementDirection.UP
            else:
                # Centroid stationary despite pixel delta -> Approaching or Receding based on changed region density
                direction = MovementDirection.APPROACHING if delta_ratio > 0.15 else MovementDirection.STATIONARY
                progression = MovementProgression.STARTED
        else:
            magnitude = MovementMagnitude.NONE
            direction = MovementDirection.STATIONARY
            progression = MovementProgression.STOPPED

        confidence = round(min(0.95, max(0.5, delta_ratio * 2.0 if motion_detected else 0.90)), 2)
        evidence = (
            f"Frame differencing ({w_a}x{h_a}): delta_ratio={delta_ratio}, "
            f"centroid_displacement=({centroid_b[0]-centroid_a[0]}, {centroid_b[1]-centroid_a[1]})."
        )

        event = NormalizedPerceptionEvent(
            modality=Modality.MOTION,
            event_type="movement_interpreted",
            source_device_id=clean_device,
            data_classification=effective_class,
            confidence=confidence,
            raw_payload_scrubbed=True,
            structured_data={
                "motion_detected": motion_detected,
                "direction": direction.value,
                "magnitude": magnitude.value,
                "progression": progression.value,
                "delta_pixel_ratio": delta_ratio,
                "centroid_a": list(centroid_a),
                "centroid_b": list(centroid_b),
                "evidence": evidence,
            },
            provenance=f"motion:{clean_device}:differencing",
            retention_policy=RetentionPolicy.SHORT_TERM_AUDIT,
        )

        response = MovementInterpretationResponse(
            event_id=event.event_id,
            motion_detected=motion_detected,
            direction=direction,
            magnitude=magnitude,
            progression=progression,
            delta_pixel_ratio=delta_ratio,
            centroid_a=centroid_a,
            centroid_b=centroid_b,
            confidence=confidence,
            evidence=evidence,
            data_classification="INTERNAL",
        )
        return response, event


class CrossModalCorrelator:
    """Tier 3: Correlates events across modalities within a temporal window."""

    def correlate(
        self,
        target_event: NormalizedPerceptionEvent,
        candidates: list[NormalizedPerceptionEvent],
        time_window_seconds: float = 5.0,
    ) -> NormalizedPerceptionEvent | None:
        """Find candidates within the temporal window and synthesize a cross-modal observation."""
        correlated_events: list[NormalizedPerceptionEvent] = []
        target_time = target_event.timestamp

        for cand in candidates:
            if cand.event_id == target_event.event_id:
                continue
            delta_sec = abs((target_time - cand.timestamp).total_seconds())
            if delta_sec <= time_window_seconds:
                # Correlate across different modalities or distinct sensors
                if cand.modality != target_event.modality or cand.source_device_id != target_event.source_device_id:
                    correlated_events.append(cand)

        if not correlated_events:
            return None

        correlated_ids = [target_event.event_id] + [c.event_id for c in correlated_events]
        modalities = sorted(list({target_event.modality.value} | {c.modality.value for c in correlated_events}))

        synthesis_notes = (
            f"Cross-modal correlation: {target_event.event_type} on {target_event.source_device_id} "
            f"co-occurred with {len(correlated_events)} event(s) across {modalities} within {time_window_seconds}s."
        )

        # Confidence is bounded and reflects observation uncertainty
        avg_conf = (target_event.confidence + sum(c.confidence for c in correlated_events)) / (1 + len(correlated_events))
        correlation_confidence = round(min(0.92, avg_conf * 0.95), 2)

        return NormalizedPerceptionEvent(
            modality=Modality.MOTION if target_event.modality == Modality.MOTION else target_event.modality,
            event_type="cross_modal_correlation",
            source_device_id=f"correlation:{target_event.source_device_id}",
            data_classification="INTERNAL",
            confidence=correlation_confidence,
            raw_payload_scrubbed=True,
            structured_data={
                "target_event_id": target_event.event_id,
                "correlated_event_ids": [c.event_id for c in correlated_events],
                "time_window_seconds": time_window_seconds,
                "underlying_modalities": modalities,
                "synthesized_observation": synthesis_notes,
                "uncertainty_label": "Correlation is an observational inference, not an authorized causal fact.",
            },
            provenance="pipeline:cross_modal_correlator",
            retention_policy=RetentionPolicy.SHORT_TERM_AUDIT,
            parent_event_ids=correlated_ids,
        )
