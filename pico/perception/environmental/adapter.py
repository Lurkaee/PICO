from datetime import datetime, timezone
from typing import Any

from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.perception.contracts import (
    EnvironmentalIngestRequest,
    Modality,
    NormalizedPerceptionEvent,
    RetentionPolicy,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EnvironmentalAdapter:
    """Manages environmental sensor readings (temperature, humidity, light, contact, AQI).

    Non-negotiable invariants:
    1. Read-only awareness: Strictly observes telemetry. Never mutates devices or operates actuators.
    2. Input schema validation: Type, range, and unit bounds strictly validated.
    3. Classification: Defaults to INTERNAL (contact and presence data are private).
    4. Honest availability: Reports MQTT/ESP32 broker connectivity state truthfully.
    """

    def __init__(self) -> None:
        self._last_readings: dict[str, dict[str, Any]] = {}
        self._last_reading_time: dict[str, datetime] = {}

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": settings.PERCEPTION_ENABLED,
            "mqtt_available": False,  # Live external broker requires configured endpoint
            "esp32_gateway_available": True,  # HTTP ingestion endpoint is active
            "home_assistant_connected": False,  # Deferred to Milestone P7
            "active_sensor_count": len(self._last_readings),
            "cooldown_seconds": settings.ENVIRONMENTAL_COOLDOWN_SECONDS,
            "supported_metrics": [
                "temperature_c",
                "humidity_percent",
                "ambient_light_lux",
                "contact_state",
                "air_quality_aqi",
                "battery_level_percent",
            ],
            "mode": "read_only_sensing",
        }

    def ingest_reading(self, request: EnvironmentalIngestRequest) -> tuple[NormalizedPerceptionEvent, bool]:
        """Ingest and validate environmental sensor reading.

        Returns (NormalizedPerceptionEvent, is_duplicate).
        """
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        if not request.source_device:
            raise ValidationError("Sensor source_device identifier is mandatory.")

        # Validate contact state if present
        if request.contact_state and request.contact_state.lower() not in ("open", "closed"):
            raise ValidationError(
                f"Invalid contact_state '{request.contact_state}'. Must be 'open' or 'closed'."
            )

        # Check if any reading was provided
        metrics = {
            "temperature_c": request.temperature_c,
            "humidity_percent": request.humidity_percent,
            "ambient_light_lux": request.ambient_light_lux,
            "contact_state": request.contact_state.lower() if request.contact_state else None,
            "air_quality_aqi": request.air_quality_aqi,
            "battery_level_percent": request.battery_level_percent,
        }
        active_metrics = {k: v for k, v in metrics.items() if v is not None}
        if not active_metrics:
            raise ValidationError("Environmental sensor message contains no valid metric readings.")

        # Deduplication and Cooldown logic
        now = utc_now()
        last_t = self._last_reading_time.get(request.source_device)
        last_val = self._last_readings.get(request.source_device)
        is_duplicate = False

        if last_t and (now - last_t).total_seconds() < settings.ENVIRONMENTAL_COOLDOWN_SECONDS:
            # Check if values are identical to previous reading
            if last_val == active_metrics:
                is_duplicate = True

        self._last_reading_time[request.source_device] = now
        self._last_readings[request.source_device] = active_metrics

        event = NormalizedPerceptionEvent(
            modality=Modality.ENVIRONMENTAL,
            event_type="environmental_reading",
            source_device_id=request.source_device,
            data_classification=request.data_classification or "INTERNAL",
            confidence=1.0,
            raw_payload_scrubbed=True,
            structured_data={
                "protocol": request.protocol,
                "readings": active_metrics,
                "is_deduplicated": is_duplicate,
            },
            provenance=f"sensor:{request.protocol}:{request.source_device}",
            retention_policy=RetentionPolicy.SHORT_TERM_AUDIT,
        )
        return event, is_duplicate
