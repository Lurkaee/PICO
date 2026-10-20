import pytest
from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.homeassistant.classification import (
    compute_trusted_ha_classification,
    format_homeassistant_context_items,
)
from pico.homeassistant.contracts import EntityState
from pico.services.context_compiler import ContextCompiler
from pico.services.routing_service import RoutingService


def test_homeassistant_domain_classification_policy() -> None:
    # Standard home entities default to INTERNAL
    cl_light = compute_trusted_ha_classification("light.living_room", "light")
    assert cl_light == "INTERNAL"

    cl_temp = compute_trusted_ha_classification("sensor.temperature", "sensor")
    assert cl_temp == "INTERNAL"

    # Security and physical access domains default to SENSITIVE
    cl_lock = compute_trusted_ha_classification("lock.front_door", "lock")
    assert cl_lock == "SENSITIVE"

    cl_alarm = compute_trusted_ha_classification("alarm_control_panel.home", "alarm_control_panel")
    assert cl_alarm == "SENSITIVE"

    cl_cam = compute_trusted_ha_classification("camera.driveway", "camera")
    assert cl_cam == "SENSITIVE"

    cl_person = compute_trusted_ha_classification("person.ayush", "person")
    assert cl_person == "SENSITIVE"

    # Door and window binary sensors default to SENSITIVE
    cl_door = compute_trusted_ha_classification(
        "binary_sensor.front_door", "binary_sensor", attributes={"device_class": "door"}
    )
    assert cl_door == "SENSITIVE"

    cl_window = compute_trusted_ha_classification(
        "binary_sensor.living_room_window", "binary_sensor", attributes={"device_class": "window"}
    )
    assert cl_window == "SENSITIVE"


def test_caller_cannot_downgrade_home_state_to_public() -> None:
    # Attempt to downgrade light to PUBLIC: clamped to INTERNAL
    cl1 = compute_trusted_ha_classification(
        entity_id="light.kitchen",
        domain="light",
        caller_classification="PUBLIC",
    )
    assert cl1 == "INTERNAL"

    # Attempt to downgrade lock to PUBLIC or INTERNAL: clamped to SENSITIVE
    cl2 = compute_trusted_ha_classification(
        entity_id="lock.back_door",
        domain="lock",
        caller_classification="PUBLIC",
    )
    assert cl2 == "SENSITIVE"

    cl3 = compute_trusted_ha_classification(
        entity_id="lock.back_door",
        domain="lock",
        caller_classification="INTERNAL",
    )
    assert cl3 == "SENSITIVE"

    # Invalid classification fails closed to baseline
    cl4 = compute_trusted_ha_classification(
        entity_id="light.desk",
        domain="light",
        caller_classification="UNKNOWN_ADVERSARIAL_TAG",
    )
    assert cl4 == "INTERNAL"

    # Elevated caller classification is respected
    cl5 = compute_trusted_ha_classification(
        entity_id="light.desk",
        domain="light",
        caller_classification="SECRET",
    )
    assert cl5 == "SECRET"


def test_homeassistant_observations_pin_routing_to_local_privacy_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Create entity state with trusted classification
    entity = EntityState(
        entity_id="light.study",
        domain="light",
        state="on",
        friendly_name="Study Light",
        data_classification="INTERNAL",
    )

    items = format_homeassistant_context_items([entity])
    assert len(items) == 1
    assert items[0].source_provenance == "ha:light.study"
    assert items[0].priority == 45
    assert items[0].data_classification == "INTERNAL"

    compiler = ContextCompiler()
    compiled = compiler.compile(items=items)
    assert compiled.effective_classification == "INTERNAL"

    # Central routing gate: pinned to local execution even if cloud is approved
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "ollama")
    router = RoutingService()
    decision = router.resolve_route(
        task_category="general_chat",
        data_classification=compiled.effective_classification,
        cloud_approved=True,
    )
    assert decision.selected_profile == "local-small"
    assert decision.provider == "ollama"
    assert decision.decision_rule == "RULE_PRIVATE_LOCAL_PIN"

    # Explicit cloud request fails closed
    with pytest.raises(ValidationError) as exc:
        router.resolve_route(
            task_category="reasoning",
            data_classification=compiled.effective_classification,
            requested_profile="cloud-strong",
            cloud_approved=True,
        )
    assert "not eligible for cloud processing" in str(exc.value).lower()
