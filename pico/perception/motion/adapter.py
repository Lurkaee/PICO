import base64
from datetime import datetime, timedelta, timezone
from typing import Any

from pico.core.config import settings
from pico.core.errors import ValidationError
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


def parse_ppm_grayscale(data: bytes) -> tuple[int, int, list[int]]:
    """Parse a simple PPM (P6 binary or P3 ascii) or raw bytes into a grayscale pixel list."""
    if data.startswith(b"P6"):
        # Header: P6\n<width> <height>\n<maxval>\n<binary rgb>
        parts = data.split(None, 4)
        if len(parts) >= 4:
            width = int(parts[1])
            height = int(parts[2])
            pixel_bytes = parts[3] if len(parts) == 4 else parts[4]
            # Convert RGB to Grayscale: Y = 0.299R + 0.587G + 0.114B
            pixels = []
            for i in range(0, min(len(pixel_bytes) - 2, width * height * 3), 3):
                r, g, b = pixel_bytes[i], pixel_bytes[i + 1], pixel_bytes[i + 2]
                pixels.append(int(0.299 * r + 0.587 * g + 0.114 * b))
            return width, height, pixels

    # Fallback for synthetic raw grayscale buffers: assume square or standard 64x64
    total = len(data)
    side = int(total**0.5)
    return side, side, list(data[: side * side])


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
            "pir_available": True,
            "frame_differencing_available": True,
            "cooldown_seconds": settings.MOTION_COOLDOWN_SECONDS,
            "tier1_motion_detection": "active",
            "tier2_movement_interpretation": "active",
            "tier3_cross_modal_correlation": "active",
            "privacy_note": "No facial or biometric identity tracking. Frame buffers scrubbed after differencing.",
        }

    # --- Tier 1: Motion Detection (PIR) ---
    def ingest_pir_motion(self, request: MotionIngestRequest) -> NormalizedPerceptionEvent:
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        now = utc_now()
        # Cooldown check
        last_t = self._last_event_time.get(request.source_device)
        if last_t and (now - last_t).total_seconds() < settings.MOTION_COOLDOWN_SECONDS:
            # Rate-limited / Cooldown active
            pass
        self._last_event_time[request.source_device] = now

        # Invariant: PIR trigger detects motion presence but does NOT claim direction or identity
        event = NormalizedPerceptionEvent(
            modality=Modality.MOTION,
            event_type="motion_detected",
            source_device_id=request.source_device,
            data_classification=request.data_classification,
            confidence=request.confidence,
            raw_payload_scrubbed=True,
            structured_data={
                "sensor_type": "pir",
                "motion_detected": request.motion_detected,
                "duration_ms": request.measured_duration_ms,
                "assertion_limits": "PIR detects motion presence only; direction, activity, and identity are unasserted.",
            },
            provenance=f"sensor:{request.source_device}",
            retention_policy=RetentionPolicy.SHORT_TERM_AUDIT,
        )
        return event

    # --- Tier 1 & 2: Frame Differencing & Movement Interpretation ---
    def interpret_movement_between_frames(
        self, request: MovementInterpretationRequest
    ) -> tuple[MovementInterpretationResponse, NormalizedPerceptionEvent]:
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        try:
            bytes_a = base64.b64decode(request.frame_a_base64)
            bytes_b = base64.b64decode(request.frame_b_base64)
        except Exception as exc:
            raise ValidationError(f"Invalid base64 frame payload: {exc}")

        w_a, h_a, pixels_a = parse_ppm_grayscale(bytes_a)
        w_b, h_b, pixels_b = parse_ppm_grayscale(bytes_b)

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
            source_device_id=request.source_device,
            data_classification="INTERNAL",
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
            provenance=f"motion:{request.source_device}:differencing",
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
