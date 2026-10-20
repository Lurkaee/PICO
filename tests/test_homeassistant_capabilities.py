import pytest
from pico.capabilities.base import ExecutionContext, RiskTier
from pico.capabilities.homeassistant.ha_capabilities import (
    HomeAssistantGetEntityStateCapability,
    HomeAssistantListEntitiesCapability,
    HomeAssistantOverviewCapability,
    HomeAssistantStatusCapability,
)
from pico.capabilities.registry import registry
from pico.homeassistant.service import HomeAssistantService


def test_homeassistant_capabilities_registered() -> None:
    expected_caps = [
        "homeassistant.status",
        "homeassistant.list_entities",
        "homeassistant.get_entity_state",
        "homeassistant.overview",
    ]
    for cap_id in expected_caps:
        assert registry.has(cap_id), f"Expected capability {cap_id} to be registered."
        cap = registry.get(cap_id)
        assert cap.metadata.risk_tier == RiskTier.READ_ONLY
        assert cap.metadata.consequential is False


def test_absolute_read_only_boundary_no_actuator_capabilities() -> None:
    # Ensure no write/service/actuator capabilities exist in P7.1/P7.2
    for cap in registry.list():
        cap_id = cap.metadata.capability_id
        assert not cap_id.startswith("homeassistant.set_"), f"Unexpected write capability: {cap_id}"
        assert not cap_id.startswith("homeassistant.turn_"), f"Unexpected write capability: {cap_id}"
        assert not cap_id.startswith("homeassistant.toggle"), f"Unexpected write capability: {cap_id}"
        assert not cap_id.startswith("homeassistant.call_service"), f"Unexpected write capability: {cap_id}"


@pytest.mark.anyio
async def test_homeassistant_status_capability_execution_and_verification() -> None:
    cap = HomeAssistantStatusCapability()
    ctx = ExecutionContext(execution_id="exec_ha_status")

    res = await cap.execute({}, ctx)
    assert res.success is True
    assert res.output_data is not None
    assert "connection_state" in res.output_data

    # Independent verifier
    v_res = await cap.verify({}, res.output_data, {}, ctx)
    assert v_res.passed is True

    # Verifier fails if physical device connectivity is claimed
    tampered = dict(res.output_data)
    tampered["physical_device_connectivity_claimed"] = True
    v_fail = await cap.verify({}, tampered, {}, ctx)
    assert v_fail.passed is False
