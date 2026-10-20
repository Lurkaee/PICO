import pytest
from pydantic import ValidationError as PydanticValidationError

from pico.core.errors import ValidationError
from pico.perception.contracts import EnvironmentalIngestRequest, Modality
from pico.perception.environmental.adapter import EnvironmentalAdapter


def test_environmental_reading_normalization() -> None:
    adapter = EnvironmentalAdapter()
    req = EnvironmentalIngestRequest(
        source_device="sensor:living_room_multisensor",
        protocol="mqtt",
        temperature_c=22.4,
        humidity_percent=45.2,
        ambient_light_lux=320.0,
        contact_state="closed",
        air_quality_aqi=42,
        battery_level_percent=95.0,
        data_classification="INTERNAL",
    )
    event, is_duplicate = adapter.ingest_reading(req)

    assert is_duplicate is False
    assert event.modality == Modality.ENVIRONMENTAL
    assert event.event_type == "environmental_reading"
    assert event.source_device_id == "sensor:living_room_multisensor"
    assert event.data_classification == "INTERNAL"

    readings = event.structured_data["readings"]
    assert readings["temperature_c"] == 22.4
    assert readings["humidity_percent"] == 45.2
    assert readings["contact_state"] == "closed"


def test_environmental_bounds_validation() -> None:
    adapter = EnvironmentalAdapter()

    # 1. Invalid contact state rejected
    with pytest.raises(ValidationError) as exc_contact:
        adapter.ingest_reading(
            EnvironmentalIngestRequest(
                source_device="sensor:door",
                contact_state="unlatched_halfway",
            )
        )
    assert "Must be 'open' or 'closed'" in str(exc_contact.value)

    # 2. Temperature out of bounds rejected by Pydantic schema
    with pytest.raises((ValidationError, PydanticValidationError)):
        EnvironmentalIngestRequest(
            source_device="sensor:temp",
            temperature_c=150.0,  # Max allowed is 100.0
        )

    # 3. No metrics provided rejected
    with pytest.raises(ValidationError) as exc_empty:
        adapter.ingest_reading(
            EnvironmentalIngestRequest(
                source_device="sensor:empty",
            )
        )
    assert "contains no valid metric readings" in str(exc_empty.value)


def test_environmental_deduplication_cooldown() -> None:
    adapter = EnvironmentalAdapter()
    req1 = EnvironmentalIngestRequest(
        source_device="sensor:balcony_temp",
        temperature_c=18.5,
        humidity_percent=60.0,
    )
    # First reading: not duplicate
    event1, is_dup1 = adapter.ingest_reading(req1)
    assert is_dup1 is False

    # Immediate second reading with identical values: detected as duplicate
    req2 = EnvironmentalIngestRequest(
        source_device="sensor:balcony_temp",
        temperature_c=18.5,
        humidity_percent=60.0,
    )
    event2, is_dup2 = adapter.ingest_reading(req2)
    assert is_dup2 is True
    assert event2.structured_data["is_deduplicated"] is True

    # Third reading with changed temperature: not duplicate
    req3 = EnvironmentalIngestRequest(
        source_device="sensor:balcony_temp",
        temperature_c=19.2,
        humidity_percent=60.0,
    )
    event3, is_dup3 = adapter.ingest_reading(req3)
    assert is_dup3 is False


def test_environmental_read_only_mode() -> None:
    adapter = EnvironmentalAdapter()
    status = adapter.get_status()
    assert status["mode"] == "read_only_sensing"
    assert status["home_assistant_connected"] is False  # Deferred to P7
    # Verify adapter has no device control methods
    assert not hasattr(adapter, "turn_on_device")
    assert not hasattr(adapter, "toggle_switch")
