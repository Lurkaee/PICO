"""Adversarial and functional test suite for Milestone P7.4.2.1:
Read-Only Climate Awareness & Safe Setpoint Feasibility.

Requirements verified:
1. Allowlist and configuration:
   - Planning and awareness disabled by default.
   - Empty or malformed allowlist rejected.
   - Malformed entity IDs rejected.
   - Non-climate domains rejected.
   - Unlisted climate entities rejected before querying Home Assistant.
   - Whitespace variations and lookalike IDs cannot bypass exact allowlist membership.
   - Invalid or unexpected attributes are bounded and sanitized.

2. State and temperature validation:
   - Valid climate response with valid units and bounds produces a feasible plan for a valid target within range.
   - Missing, invalid, nonnumeric, non-finite, or contradictory bounds fail closed.
   - Missing or ambiguous units do not produce a false feasibility claim.
   - Unit mismatch between requested unit and entity's unit produces unit_unknown / rejection.
   - Missing, unknown, and unavailable entity states are represented accurately (state_unavailable, state_unknown).
   - A target below the reported minimum or above the reported maximum is rejected (target_out_of_range).
   - Malformed supported-mode metadata does not lead to invented device capabilities.
   - A response for a different entity is rejected.
   - A failed GET, HTTP error, timeout, authentication failure, or missing entity produces an honest structured failure.
   - A successful HTTP response alone is never presented as proof of physical-device connectivity.

3. Strict zero-mutation guarantee:
   - Every climate readiness request produces zero POST/PUT/PATCH/DELETE requests.
   - Every climate observation request produces zero mutation calls.
   - Every feasibility-plan request produces zero mutation calls, regardless of success or failure.
   - No climate service executor or generic service-call capability is registered.
   - The Home Assistant read-only WebSocket allowlist is unchanged.
   - Existing P7.3 light actuation and P7.4.1 Stage C switch actuation tests continue to pass.

4. Privacy and integration:
   - Credentials and sensitive raw attributes do not leak through response schemas, logs, or event-ledger entries.
   - Read-only capability permissions cannot be escalated by caller input or model output.
   - Offline readiness performs zero network requests.
   - Live readiness uses read-only calls only.
   - Route-level API endpoints (plan, observe, readiness) and CLI runner behave deterministically.
"""

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from pico.capabilities.base import RiskTier
from pico.capabilities.homeassistant.ha_readiness import (
    HomeAssistantClimateReadinessCapability,
)
from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import EntityNotFoundError, ValidationError
from pico.homeassistant.climate import (
    HomeAssistantClimateService,
    parse_temperature,
    parse_temperature_unit,
    validate_climate_allowlist,
    validate_climate_grid_origins,
    validate_dual_mode_compatibility,
    validate_dual_temperature_capability,
    validate_temperature_step,
)
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    ClimateDualSetpointPlan,
    ClimateObservation,
    ClimateReadinessReport,
    ClimateSetpointPlan,
    EntityState,
    ReadinessCheckStatus,
    ReadinessErrorCategory,
)
from pico.homeassistant.service import HomeAssistantService
from pico.homeassistant.readiness import (
    HomeAssistantClimateReadinessChecker,
    evaluate_climate_readiness,
    main as readiness_cli_main,
)
from pico.homeassistant.sanitizer import sanitize_entity_attributes
from pico.homeassistant.ws_client import ALLOWED_READONLY_WS_COMMANDS
from pico.main import app

TEST_CLIMATE_A = "climate.living_room_thermostat"
TEST_CLIMATE_B = "climate.bedroom_ac"
DEFAULT_CLIMATE_ALLOWLIST = [TEST_CLIMATE_A, TEST_CLIMATE_B]
MOCK_BASE_URL = "http://127.0.0.1:8123"
MOCK_TOKEN = "valid_test_token_climate_p7421"
MOCK_APPROVAL_SECRET = "valid_climate_secret_token"


@pytest.fixture(autouse=True)
def configure_climate_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default fixture for P7.4.2.1 tests: HA enabled and Stage D climate allowlist configured."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", MOCK_BASE_URL)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", MOCK_TOKEN)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ALLOWLIST", json.dumps(DEFAULT_CLIMATE_ALLOWLIST))
    # Actuation settings must remain untouched/disabled
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", False)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_APPROVAL_SECRET)


def make_mock_climate_transport(
    entity_payloads: dict[str, dict[str, Any]] | None = None,
    simulate_timeout: bool = False,
    simulate_status_code: int = 200,
) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """Create a mock httpx transport tracking calls and returning simulated climate entity data."""
    default_payloads: dict[str, dict[str, Any]] = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "friendly_name": "Living Room Thermostat",
                "current_temperature": 20.5,
                "temperature": 21.0,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 1,
                "unit_of_measurement": "°C",
                "hvac_modes": ["off", "heat", "cool", "auto"],
            },
        },
        TEST_CLIMATE_B: {
            "entity_id": TEST_CLIMATE_B,
            "state": "cool",
            "attributes": {
                "friendly_name": "Bedroom AC",
                "current_temperature": 72.0,
                "temperature": 70.0,
                "min_temp": 60.0,
                "max_temp": 85.0,
                "target_temp_step": 1.0,
                "supported_features": 1,
                "unit_of_measurement": "°F",
                "hvac_modes": ["off", "cool", "fan_only"],
            },
        },
    }

    payloads = dict(entity_payloads if entity_payloads is not None else default_payloads)
    call_log = {
        "read_calls": [],
        "mutation_calls": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path

        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            call_log["mutation_calls"].append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
            })
            return httpx.Response(500, json={"error": "Mutation not permitted in read-only climate slice."})

        call_log["read_calls"].append({
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
        })

        if simulate_timeout:
            raise httpx.ReadTimeout("Simulated timeout connecting to Home Assistant.")

        if simulate_status_code != 200:
            return httpx.Response(simulate_status_code, json={"message": "Simulated error"})

        if request.method == "GET" and url_path == "/api/":
            return httpx.Response(200, json={"message": "API running."})

        if request.method == "GET" and url_path.startswith("/api/states/"):
            entity_id = url_path.split("/")[-1]
            if entity_id in payloads:
                return httpx.Response(200, json=payloads[entity_id])
            return httpx.Response(404, json={"message": f"Entity '{entity_id}' not found."})

        if request.method == "GET" and url_path == "/api/states":
            return httpx.Response(200, json=list(payloads.values()))

        return httpx.Response(404, json={"message": "Not found."})

    return httpx.MockTransport(handler), call_log


def make_mock_climate_client(
    entity_payloads: dict[str, dict[str, Any]] | None = None,
    simulate_timeout: bool = False,
    simulate_status_code: int = 200,
) -> tuple[HomeAssistantClient, dict[str, Any]]:
    """Create a HomeAssistantClient backed by the mock climate transport."""
    transport, call_log = make_mock_climate_transport(
        entity_payloads=entity_payloads,
        simulate_timeout=simulate_timeout,
        simulate_status_code=simulate_status_code,
    )
    http_client = httpx.AsyncClient(transport=transport)
    client = HomeAssistantClient(http_client=http_client)
    return client, call_log


# =====================================================================
# 1. Allowlist & Configuration Validation
# =====================================================================

@pytest.mark.anyio
async def test_climate_awareness_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planning and awareness must be disabled by default."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False)
    service = HomeAssistantClimateService()
    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 22.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "disabled"


@pytest.mark.anyio
async def test_climate_awareness_disabled_returns_honest_disabled_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """When disabled, feasibility plan returns disabled status without querying HA."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False)
    service = HomeAssistantClimateService()
    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 22.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "disabled"
    assert "disabled" in plan.reasons[0].lower()
    assert plan.is_mutation_performed is False


def test_empty_or_malformed_allowlist_rejected() -> None:
    """Empty or unparseable allowlist must raise ValidationError."""
    with pytest.raises(ValidationError, match="empty or unconfigured"):
        validate_climate_allowlist("[]")
    with pytest.raises(ValidationError, match="empty or unconfigured"):
        validate_climate_allowlist("")
    with pytest.raises(ValidationError, match="Malformed JSON"):
        validate_climate_allowlist("{not valid json}")
    with pytest.raises(ValidationError, match="must be a list or JSON array"):
        validate_climate_allowlist(12345)


def test_non_climate_domains_rejected() -> None:
    """Non-climate entities must be strictly rejected."""
    with pytest.raises(ValidationError, match="strictly restricted to 'climate.*' entities"):
        validate_climate_allowlist(json.dumps(["switch.desk_lamp"]))
    with pytest.raises(ValidationError, match="strictly restricted to 'climate.*' entities"):
        validate_climate_allowlist(json.dumps(["light.kitchen"]))
    with pytest.raises(ValidationError, match="strictly restricted to 'climate.*' entities"):
        validate_climate_allowlist(json.dumps(["sensor.temperature"]))


def test_wildcards_and_special_chars_rejected() -> None:
    """Wildcards, prefixes, and special characters must be rejected."""
    with pytest.raises(ValidationError, match="Wildcards or special characters"):
        validate_climate_allowlist(json.dumps(["climate.*"]))
    with pytest.raises(ValidationError, match="Wildcards or special characters"):
        validate_climate_allowlist(json.dumps(["climate.living_room*"]))
    with pytest.raises(ValidationError, match="Wildcards or special characters"):
        validate_climate_allowlist(json.dumps(["climate.?"]))


def test_whitespace_variations_rejected() -> None:
    """Unnormalized leading/trailing whitespace must be rejected."""
    with pytest.raises(ValidationError, match="Whitespace variations"):
        validate_climate_allowlist(json.dumps([" climate.living_room "]))
    with pytest.raises(ValidationError, match="Whitespace variations"):
        validate_climate_allowlist(json.dumps(["climate.living_room\n"]))


@pytest.mark.anyio
async def test_unlisted_climate_entity_rejected_before_state_query() -> None:
    """An entity not in the allowlist must return not_allowlisted with zero network requests."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility("climate.unauthorized_thermostat", 22.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "not_allowlisted"
    assert "not in HOME_ASSISTANT_CLIMATE_ALLOWLIST" in plan.reasons[0]
    # Zero network requests should be issued
    assert len(call_log["read_calls"]) == 0
    assert len(call_log["mutation_calls"]) == 0


def test_sanitizer_bounds_climate_attributes() -> None:
    """Climate attributes must pass through sanitizer while stripping sensitive tokens/GPS."""
    raw = {
        "friendly_name": "Living Room Thermostat",
        "current_temperature": 21.5,
        "temperature": 22.0,
        "min_temp": 16.0,
        "max_temp": 30.0,
        "target_temp_step": 0.5,
        "unit_of_measurement": "°C",
        "hvac_modes": ["off", "heat", "cool"],
        "latitude": 37.7749,
        "longitude": -122.4194,
        "access_token": "leak_me_secret",
        "arbitrary_unapproved_blob": "drop_this",
    }
    sanitized = sanitize_entity_attributes(raw)
    assert sanitized["current_temperature"] == 21.5
    assert sanitized["min_temp"] == 16.0
    assert sanitized["max_temp"] == 30.0
    assert sanitized["unit_of_measurement"] == "°C"
    assert "latitude" not in sanitized
    assert "longitude" not in sanitized
    assert "access_token" not in sanitized
    assert "arbitrary_unapproved_blob" not in sanitized


# =====================================================================
# 2. State & Temperature Feasibility Validation
# =====================================================================

@pytest.mark.anyio
async def test_valid_climate_response_produces_feasible_plan() -> None:
    """Valid target within reported bounds produces a feasible plan with status 'ready' when grid origin is established."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # 1. With verified server-configured grid origin established
    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=22.5,
        requested_unit="°C",
    )
    assert plan.is_feasible is True
    assert plan.feasibility_status == "ready"
    assert plan.requested_temperature == 22.5
    assert plan.grid_origin == 16.0
    assert plan.grid_origin_unit == "°C"
    assert plan.current_temperature == 20.5
    assert plan.min_temp == 16.0
    assert plan.max_temp == 28.0
    assert plan.temperature_unit == "°C"
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0
    assert len(call_log["read_calls"]) == 1

    # 2. Without grid origin (standard HA contract does not document origin): returns step_alignment_unknown
    service_no_origin = HomeAssistantClimateService(readonly_client=client)
    plan_no_origin = await service_no_origin.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=22.5,
        requested_unit="°C",
    )
    assert plan_no_origin.is_feasible is False
    assert plan_no_origin.feasibility_status == "step_alignment_unknown"
    assert "no trusted server-side step-grid origin is configured" in plan_no_origin.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_target_out_of_range_below_min_rejected() -> None:
    """Requested setpoint below reported min_temp is rejected as target_out_of_range."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=14.0,  # Below min 16.0
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_out_of_range"
    assert "below minimum" in plan.reasons[0].lower()
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_target_out_of_range_above_max_rejected() -> None:
    """Requested setpoint above reported max_temp is rejected as target_out_of_range."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=32.0,  # Above max 28.0
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_out_of_range"
    assert "above maximum" in plan.reasons[0].lower()
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_missing_or_invalid_temperature_bounds_fail_closed() -> None:
    """Missing or non-numeric min_temp or max_temp must fail closed without inventing defaults."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.0,
                "unit_of_measurement": "°C",
                # min_temp and max_temp are omitted!
            },
        }
    }
    client, _ = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 22.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "state_unknown"
    assert any("bounds" in r.lower() for r in plan.reasons)


@pytest.mark.anyio
async def test_contradictory_bounds_fail_closed() -> None:
    """Contradictory bounds (min_temp > max_temp) must fail closed as state_unknown."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.0,
                "min_temp": 30.0,
                "max_temp": 15.0,  # min > max!
                "unit_of_measurement": "°C",
            },
        }
    }
    client, _ = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 22.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "state_unknown"
    assert any("contradictory" in r.lower() for r in plan.reasons)


@pytest.mark.anyio
async def test_missing_or_ambiguous_unit_fails_closed() -> None:
    """Missing temperature unit must return unit_unknown rather than guessing."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.0,
                "min_temp": 16.0,
                "max_temp": 28.0,
                # unit_of_measurement omitted!
            },
        }
    }
    client, _ = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 22.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "unit_unknown"
    assert any("unit" in r.lower() for r in plan.reasons)


@pytest.mark.anyio
async def test_unit_mismatch_rejected() -> None:
    """Requesting °F on an entity reporting °C must fail closed with unit_unknown."""
    client, _ = make_mock_climate_client()
    service = HomeAssistantClimateService(readonly_client=client)

    # TEST_CLIMATE_A is configured with °C in mock
    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=72.0,
        requested_unit="°F",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "unit_unknown"
    assert any("conflicts" in r.lower() for r in plan.reasons)


@pytest.mark.anyio
async def test_unavailable_or_unknown_state_represented_honestly() -> None:
    """Entity reporting 'unavailable' or 'unknown' state must not be marked ready."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "unavailable",
            "attributes": {
                "min_temp": 16.0,
                "max_temp": 28.0,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, _ = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "state_unavailable"
    assert plan.is_mutation_performed is False


@pytest.mark.anyio
async def test_nonnumeric_requested_temperature_fails_closed() -> None:
    """Non-numeric or non-finite requested temperature (e.g. NaN, Inf) fails closed."""
    service = HomeAssistantClimateService()
    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, float("nan"))
    assert plan.is_feasible is False
    assert plan.feasibility_status == "state_unknown"
    assert "finite numeric" in plan.reasons[0]


@pytest.mark.anyio
async def test_network_timeout_produces_honest_state_unavailable() -> None:
    """Network timeout reading state returns state_unavailable without raising unhandled error."""
    client, _ = make_mock_climate_client(simulate_timeout=True)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 22.0)
    assert plan.is_feasible is False
    assert plan.feasibility_status == "state_unavailable"
    assert plan.is_mutation_performed is False


# =====================================================================
# 3. Strict Zero-Mutation Guarantees
# =====================================================================

@pytest.mark.anyio
async def test_every_feasibility_plan_produces_zero_mutations() -> None:
    """Feasibility planning never issues POST/PUT/PATCH/DELETE calls."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(readonly_client=client)

    # 1. Feasible plan
    await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0)
    # 2. Out of range plan
    await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 40.0)
    # 3. Mismatched unit plan
    await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 70.0, requested_unit="°F")

    assert len(call_log["mutation_calls"]) == 0
    assert len(call_log["read_calls"]) == 3


@pytest.mark.anyio
async def test_every_observation_produces_zero_mutations() -> None:
    """Climate observation never issues POST/PUT/PATCH/DELETE calls."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(readonly_client=client)

    obs = await service.get_climate_observation(TEST_CLIMATE_A)
    assert obs.is_mutation_performed is False
    assert obs.physical_device_connectivity_claimed is False
    assert len(call_log["mutation_calls"]) == 0
    assert len(call_log["read_calls"]) == 1


@pytest.mark.anyio
async def test_climate_readiness_produces_zero_mutations() -> None:
    """Climate readiness evaluation (offline and live) produces zero mutation requests."""
    client, call_log = make_mock_climate_client()

    # Offline preflight
    report_offline = await evaluate_climate_readiness(check_live=False, readonly_client=client)
    assert report_offline.live_check_performed is False
    assert len(call_log["read_calls"]) == 0
    assert len(call_log["mutation_calls"]) == 0

    # Live read-only check
    report_live = await evaluate_climate_readiness(check_live=True, readonly_client=client)
    assert report_live.live_check_performed is True
    assert report_live.ready_for_observation is True
    assert len(call_log["mutation_calls"]) == 0
    assert len(call_log["read_calls"]) == 1


def test_no_climate_mutation_capability_registered() -> None:
    """No mutating climate capabilities (e.g. set_temperature, set_hvac_mode) exist in registry."""
    registered_ids = [c.metadata.capability_id for c in registry.list()]
    assert "homeassistant.climate_readiness" in registered_ids
    assert "homeassistant.climate_actuate" not in registered_ids
    assert "homeassistant.climate_set_temperature" not in registered_ids
    assert "homeassistant.call_service" not in registered_ids


def test_websocket_allowlist_preserves_strict_readonly() -> None:
    """WebSocket allowlist remains strictly read-only and does not contain call_service."""
    assert "call_service" not in ALLOWED_READONLY_WS_COMMANDS
    assert "get_states" in ALLOWED_READONLY_WS_COMMANDS
    assert "get_config" in ALLOWED_READONLY_WS_COMMANDS


# =====================================================================
# 4. Privacy, Capability & Route Integration
# =====================================================================

@pytest.mark.anyio
async def test_credentials_do_not_leak_in_schemas_or_reports() -> None:
    """Tokens and secrets must never appear in readiness report output."""
    rep = await evaluate_climate_readiness(check_live=False)
    serialized = rep.model_dump_json()
    assert MOCK_TOKEN not in serialized
    assert MOCK_APPROVAL_SECRET not in serialized


@pytest.mark.anyio
async def test_climate_readiness_capability_execution() -> None:
    """Test HomeAssistantClimateReadinessCapability via CapabilityRegistry."""
    client, _ = make_mock_climate_client()

    with patch("pico.capabilities.homeassistant.ha_readiness.evaluate_climate_readiness") as mock_eval:
        mock_eval.return_value = ClimateReadinessReport(
            ready_for_observation=True,
            target_entity_id=TEST_CLIMATE_A,
            allowlisted_entities=DEFAULT_CLIMATE_ALLOWLIST,
            climate_awareness_enabled=True,
            actuation_enabled=False,
            summary="READY: Stage D climate awareness preflight passed.",
        )
        cap = registry.get("homeassistant.climate_readiness")
        assert cap.metadata.risk_tier == RiskTier.READ_ONLY
        assert cap.metadata.required_permission == "homeassistant:read"
        assert cap.metadata.consequential is False

        from pico.capabilities.base import ExecutionContext
        ctx = ExecutionContext(execution_id="exec_test_climate", caller_permissions={"homeassistant:read"})
        res = await cap.execute({"check_live": False, "target_entity_id": TEST_CLIMATE_A}, ctx)
        assert res.success is True
        assert res.output_data["ready_for_observation"] is True


def test_api_climate_plan_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route test: POST /api/homeassistant/climate/plan rejects caller grid_origin and uses server configuration."""
    transport, call_log = make_mock_climate_transport()

    real_client_cls = httpx.AsyncClient
    with patch("pico.homeassistant.client.httpx.AsyncClient", side_effect=lambda **kwargs: real_client_cls(transport=transport)):
        with TestClient(app) as test_client:
            # 1. Caller attempting to supply grid_origin is strictly forbidden (HTTP 422)
            resp_forbidden = test_client.post(
                "/api/homeassistant/climate/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temperature": 21.5,
                    "target_unit": "°C",
                    "grid_origin": 16.0,
                },
            )
            assert resp_forbidden.status_code == 422
            err_data = resp_forbidden.json()
            assert "Client-supplied 'grid_origin' is forbidden" in str(err_data)
            assert len(call_log["mutation_calls"]) == 0

            # 2. Unconfigured entity in server settings fails closed as step_alignment_unknown
            resp_no_origin = test_client.post(
                "/api/homeassistant/climate/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temperature": 21.5,
                    "target_unit": "°C",
                },
            )
            assert resp_no_origin.status_code == 200
            data_no_origin = resp_no_origin.json()
            assert data_no_origin["is_feasible"] is False
            assert data_no_origin["feasibility_status"] == "step_alignment_unknown"
            assert data_no_origin["grid_origin"] is None
            assert len(call_log["mutation_calls"]) == 0

            # 3. With server-configured trusted grid origin
            monkeypatch.setattr(
                settings,
                "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS",
                json.dumps({TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}}),
            )
            resp_configured = test_client.post(
                "/api/homeassistant/climate/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temperature": 21.5,
                    "target_unit": "°C",
                },
            )
            assert resp_configured.status_code == 200
            data_configured = resp_configured.json()
            assert data_configured["is_feasible"] is True
            assert data_configured["feasibility_status"] == "ready"
            assert data_configured["grid_origin"] == 16.0
            assert data_configured["grid_origin_unit"] == "°C"
            assert len(call_log["mutation_calls"]) == 0


def test_api_climate_observe_route() -> None:
    """Route test: GET /api/homeassistant/climate/observe/{entity_id}."""
    transport, call_log = make_mock_climate_transport()

    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        with TestClient(app) as test_client:
            resp = test_client.get(f"/api/homeassistant/climate/observe/{TEST_CLIMATE_A}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["entity_id"] == TEST_CLIMATE_A
            assert data["current_temperature"] == 20.5
            assert data["state_readiness"] == "ready"
            assert data["is_mutation_performed"] is False
            assert len(call_log["mutation_calls"]) == 0


def test_api_climate_readiness_route() -> None:
    """Route test: GET /api/homeassistant/climate/readiness."""
    transport, call_log = make_mock_climate_transport()

    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        with TestClient(app) as test_client:
            # Offline
            resp_offline = test_client.get("/api/homeassistant/climate/readiness?check_live=false")
            assert resp_offline.status_code == 200
            data_offline = resp_offline.json()
            assert data_offline["ready_for_observation"] is True
            assert data_offline["actuation_enabled"] is False
            assert data_offline["live_check_performed"] is False

            # Live
            resp_live = test_client.get("/api/homeassistant/climate/readiness?check_live=true")
            assert resp_live.status_code == 200
            data_live = resp_live.json()
            assert data_live["ready_for_observation"] is True
            assert data_live["live_check_performed"] is True
            assert data_live["live_passed"] is True
            assert len(call_log["mutation_calls"]) == 0


def test_cli_runner_climate_offline_and_live() -> None:
    """CLI runner test with --climate flag."""
    transport, _ = make_mock_climate_transport()

    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        # Offline
        code_offline = readiness_cli_main(["--climate", "--json"])
        assert code_offline == 0

        # Live
        code_live = readiness_cli_main(["--climate", "--live", "--json"])
        assert code_live == 0


# =====================================================================
# 5. Milestone P7.4.2.1.1: Climate Feasibility Metadata & Mode Closure Tests
# =====================================================================

@pytest.mark.anyio
async def test_setpoint_aligns_with_reported_half_degree_step() -> None:
    """Requested setpoint aligned with reported 0.5 step returns ready when grid origin is established."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # With established grid origin (16.0): aligns
    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.5,
        requested_unit="°C",
    )
    assert plan.is_feasible is True
    assert plan.feasibility_status == "ready"
    assert plan.grid_origin == 16.0
    assert plan.grid_origin_unit == "°C"
    assert plan.target_temp_step == 0.5
    assert plan.supported_features == 1
    assert "aligns with reported step" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0

    # Without grid origin: fail closed as step_alignment_unknown
    service_no_origin = HomeAssistantClimateService(readonly_client=client)
    plan_no_origin = await service_no_origin.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.5,
        requested_unit="°C",
    )
    assert plan_no_origin.is_feasible is False
    assert plan_no_origin.feasibility_status == "step_alignment_unknown"
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_setpoint_aligns_with_quarter_degree_step() -> None:
    """Requested setpoint aligned with reported 0.25 step returns ready when grid origin is established."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "friendly_name": "Living Room Thermostat",
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.25,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # With established grid origin (16.0): aligns
    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.25,
        requested_unit="°C",
    )
    assert plan.is_feasible is True
    assert plan.feasibility_status == "ready"
    assert plan.grid_origin == 16.0
    assert plan.grid_origin_unit == "°C"
    assert plan.target_temp_step == 0.25
    assert len(call_log["mutation_calls"]) == 0

    # Without grid origin: returns step_alignment_unknown
    service_no_origin = HomeAssistantClimateService(readonly_client=client)
    plan_no_origin = await service_no_origin.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.25,
        requested_unit="°C",
    )
    assert plan_no_origin.is_feasible is False
    assert plan_no_origin.feasibility_status == "step_alignment_unknown"
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_setpoint_conflicts_with_half_degree_step() -> None:
    """Requested setpoint not aligned with 0.5 step returns target_step_mismatch under documented rule."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.3,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_step_mismatch"
    assert "does not align with supported step increment" in plan.reasons[0]
    assert plan.target_temp_step == 0.5
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_setpoint_conflicts_with_integer_step() -> None:
    """Requested fractional setpoint on an entity reporting 1.0 step returns target_step_mismatch."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_B: {"origin": 60.0, "unit": "°F"}},
    )

    # TEST_CLIMATE_B reports target_temp_step = 1.0, range [60.0, 85.0] °F, grid_origin = 60.0
    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_B,
        requested_temperature=72.5,
        requested_unit="°F",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_step_mismatch"
    assert "does not align with supported step increment 1.0" in plan.reasons[0]
    assert plan.target_temp_step == 1.0
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_step_grid_non_zero_offset_rule() -> None:
    """Non-zero minimum/offset case demonstrating implementation does not assume a zero-origin grid."""
    # Suppose grid origin is 16.5 and step is 1.0.
    # 17.5 aligns: (17.5 - 16.5) = 1.0, 1.0 % 1.0 == 0.
    # (Under a zero-origin assumption, 17.5 % 1.0 would falsely report mismatch).
    is_ok, status, reason = validate_temperature_step(17.5, step=1.0, grid_origin=16.5)
    assert is_ok is True
    assert status == "ready"
    assert reason is None

    # 17.0 is a genuine mismatch: (17.0 - 16.5) = 0.5, 0.5 % 1.0 != 0.
    is_mismatch, m_status, m_reason = validate_temperature_step(17.0, step=1.0, grid_origin=16.5)
    assert is_mismatch is False
    assert m_status == "target_step_mismatch"
    assert "does not align with supported step increment 1.0 from established grid origin 16.5" in m_reason


@pytest.mark.anyio
async def test_step_grid_unknown_origin_returns_step_alignment_unknown() -> None:
    """When target_temp_step is known but grid_origin is None, fail closed as step_alignment_unknown."""
    is_ok, status, reason = validate_temperature_step(21.5, step=0.5, grid_origin=None)
    assert is_ok is False
    assert status == "step_alignment_unknown"
    assert "upstream Home Assistant contract does not establish a step-grid origin" in reason


@pytest.mark.anyio
async def test_no_hidden_rounding_or_mutation_of_requested_value() -> None:
    """Planner must never silently round or modify the requested temperature."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.3,
        requested_unit="°C",
    )
    # Requested temperature must remain exactly 21.3, never rounded to 21.5 or 21.0
    assert plan.requested_temperature == 21.3
    assert plan.is_feasible is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_missing_step_metadata_returns_step_unknown() -> None:
    """Missing target_temp_step returns step_unknown rather than fabricating a default."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "supported_features": 1,
                "unit_of_measurement": "°C",
                # target_temp_step intentionally omitted
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "step_unknown"
    assert "does not report target_temp_step metadata" in plan.reasons[0]
    assert plan.target_temp_step is None
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_zero_or_negative_step_metadata_returns_step_unknown() -> None:
    """Zero or negative step metadata returns step_unknown."""
    for bad_step in (0, -0.5, -1.0):
        payloads = {
            TEST_CLIMATE_A: {
                "entity_id": TEST_CLIMATE_A,
                "state": "heat",
                "attributes": {
                    "current_temperature": 20.5,
                    "min_temp": 16.0,
                    "max_temp": 28.0,
                    "target_temp_step": bad_step,
                    "supported_features": 1,
                    "unit_of_measurement": "°C",
                },
            }
        }
        client, call_log = make_mock_climate_client(entity_payloads=payloads)
        service = HomeAssistantClimateService(readonly_client=client)

        plan = await service.assess_setpoint_feasibility(
            target_entity_id=TEST_CLIMATE_A,
            requested_temperature=21.0,
            requested_unit="°C",
        )
        assert plan.is_feasible is False
        assert plan.feasibility_status == "step_unknown"
        assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_boolean_or_string_step_metadata_returns_step_unknown() -> None:
    """Boolean or string step metadata returns step_unknown."""
    for bad_step in (True, False, "invalid_step"):
        payloads = {
            TEST_CLIMATE_A: {
                "entity_id": TEST_CLIMATE_A,
                "state": "heat",
                "attributes": {
                    "current_temperature": 20.5,
                    "min_temp": 16.0,
                    "max_temp": 28.0,
                    "target_temp_step": bad_step,
                    "supported_features": 1,
                    "unit_of_measurement": "°C",
                },
            }
        }
        client, call_log = make_mock_climate_client(entity_payloads=payloads)
        service = HomeAssistantClimateService(readonly_client=client)

        plan = await service.assess_setpoint_feasibility(
            target_entity_id=TEST_CLIMATE_A,
            requested_temperature=21.0,
            requested_unit="°C",
        )
        assert plan.is_feasible is False
        assert plan.feasibility_status == "step_unknown"
        assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_non_finite_step_metadata_returns_step_unknown() -> None:
    """Non-finite step metadata (NaN, Inf) returns step_unknown."""
    for bad_step in (float("nan"), float("inf"), -float("inf")):
        # Direct validation helper test
        is_ok, status, _ = validate_temperature_step(21.0, bad_step)
        assert is_ok is False
        assert status == "step_unknown"

        # Service level test with direct mocked entity state
        mock_service = HomeAssistantService(client=None)
        mock_entity_state = EntityState(
            entity_id=TEST_CLIMATE_A,
            domain="climate",
            state="heat",
            attributes={
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": bad_step,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        )
        with patch.object(mock_service, "get_entity_state", return_value=mock_entity_state):
            service = HomeAssistantClimateService(service=mock_service)
            plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0, "°C")
            assert plan.is_feasible is False
            assert plan.feasibility_status == "step_unknown"


@pytest.mark.anyio
async def test_missing_capability_metadata_returns_capability_unknown() -> None:
    """Entity omitting supported_features returns capability_unknown."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "unit_of_measurement": "°C",
                # supported_features omitted
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "capability_unknown"
    assert "does not report trustworthy supported_features" in plan.reasons[0]
    assert plan.supported_features is None
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_boolean_or_negative_capability_returns_capability_unknown() -> None:
    """Boolean or negative supported_features returns capability_unknown."""
    for bad_feat in (True, False, -1, -100):
        payloads = {
            TEST_CLIMATE_A: {
                "entity_id": TEST_CLIMATE_A,
                "state": "heat",
                "attributes": {
                    "current_temperature": 20.5,
                    "min_temp": 16.0,
                    "max_temp": 28.0,
                    "target_temp_step": 0.5,
                    "supported_features": bad_feat,
                    "unit_of_measurement": "°C",
                },
            }
        }
        client, call_log = make_mock_climate_client(entity_payloads=payloads)
        service = HomeAssistantClimateService(readonly_client=client)

        plan = await service.assess_setpoint_feasibility(
            target_entity_id=TEST_CLIMATE_A,
            requested_temperature=21.0,
            requested_unit="°C",
        )
        assert plan.is_feasible is False
        assert plan.feasibility_status == "capability_unknown"
        assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_unsupported_range_only_capability_rejected() -> None:
    """Entity with supported_features=2 (range only) rejects single setpoint as unsupported_capability."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 2,  # TARGET_TEMPERATURE_RANGE only
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "unsupported_capability"
    assert "only advertises dual-setpoint temperature range capability" in plan.reasons[0]
    assert plan.supported_features == 2
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_unsupported_zero_features_rejected() -> None:
    """Entity with supported_features=0 rejects single setpoint as unsupported_capability."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 0,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "unsupported_capability"
    assert plan.supported_features == 0
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_supported_dual_single_and_range_capability() -> None:
    """Entity supporting both single setpoint and range (1 | 2 = 3) passes capability check in heat mode,
    but is rejected when current mode is heat_cool."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 3,  # 1 | 2
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # In heat mode with verified grid origin, it is feasible (ready)
    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is True
    assert plan.feasibility_status == "ready"
    assert plan.supported_features == 3
    assert len(call_log["mutation_calls"]) == 0

    # In heat mode without grid origin, fails closed as step_alignment_unknown
    service_no_origin = HomeAssistantClimateService(readonly_client=client)
    plan_no_origin = await service_no_origin.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan_no_origin.is_feasible is False
    assert plan_no_origin.feasibility_status == "step_alignment_unknown"

    # In heat_cool mode, even with supported_features=3, single setpoint is mode_incompatible
    payloads[TEST_CLIMATE_A]["state"] = "heat_cool"
    client_hc, call_log_hc = make_mock_climate_client(entity_payloads=payloads)
    service_hc = HomeAssistantClimateService(
        readonly_client=client_hc,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )
    plan_hc = await service_hc.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan_hc.is_feasible is False
    assert plan_hc.feasibility_status == "mode_incompatible"
    assert "heat_cool" in plan_hc.reasons[0].lower()
    assert len(call_log_hc["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_operating_mode_auto_rejected_as_mode_incompatible() -> None:
    """Entity in 'auto' mode returns mode_incompatible because temperature cannot be set in auto mode."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "auto",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "mode_incompatible"
    assert "auto" in plan.reasons[0].lower()
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_operating_mode_heat_cool_rejected_as_mode_incompatible() -> None:
    """Entity in 'heat_cool' mode returns mode_incompatible because dual-setpoint range is required."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat_cool",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 3,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "mode_incompatible"
    assert "heat_cool" in plan.reasons[0].lower()
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_operating_mode_dry_fails_closed_as_mode_incompatible() -> None:
    """Entity in 'dry' mode returns mode_incompatible failing closed under unverified setpoint compatibility."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "dry",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "mode_incompatible"
    assert "dry" in plan.reasons[0].lower()
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_feature_bitmask_cannot_override_incompatible_mode() -> None:
    """supported_features declaring single-temperature (1 or 3) cannot override incompatible modes."""
    incompatible_modes = ("off", "fan_only", "auto", "heat_cool", "dry")
    for mode in incompatible_modes:
        payloads = {
            TEST_CLIMATE_A: {
                "entity_id": TEST_CLIMATE_A,
                "state": mode,
                "attributes": {
                    "current_temperature": 20.5,
                    "min_temp": 16.0,
                    "max_temp": 28.0,
                    "target_temp_step": 0.5,
                    "supported_features": 3,  # Has TARGET_TEMPERATURE bit
                    "unit_of_measurement": "°C",
                },
            }
        }
        client, call_log = make_mock_climate_client(entity_payloads=payloads)
        service = HomeAssistantClimateService(
            readonly_client=client,
            trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
        )

        plan = await service.assess_setpoint_feasibility(
            target_entity_id=TEST_CLIMATE_A,
            requested_temperature=21.0,
            requested_unit="°C",
        )
        assert plan.is_feasible is False
        assert plan.feasibility_status == "mode_incompatible"
        assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_operating_mode_off_rejected_as_mode_incompatible() -> None:
    """Entity in 'off' mode returns mode_incompatible."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "off",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "mode_incompatible"
    assert "off" in plan.reasons[0].lower()
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_operating_mode_fan_only_rejected_as_mode_incompatible() -> None:
    """Entity in 'fan_only' mode returns mode_incompatible."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "fan_only",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "mode_incompatible"
    assert "fan_only" in plan.reasons[0].lower()
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_operating_mode_custom_unsupported_rejected_as_mode_incompatible() -> None:
    """Entity in unrecognized operating mode returns mode_incompatible."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "purifying_air",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(readonly_client=client)

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "mode_incompatible"
    assert "purifying_air" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_operating_mode_compatible_modes() -> None:
    """Single-setpoint temperature-regulating modes (cool, heat) pass mode check when grid_origin is provided."""
    for mode in ("cool", "heat"):
        payloads = {
            TEST_CLIMATE_A: {
                "entity_id": TEST_CLIMATE_A,
                "state": mode,
                "attributes": {
                    "current_temperature": 20.5,
                    "min_temp": 16.0,
                    "max_temp": 28.0,
                    "target_temp_step": 0.5,
                    "supported_features": 1,
                    "unit_of_measurement": "°C",
                },
            }
        }
        client, call_log = make_mock_climate_client(entity_payloads=payloads)
        service = HomeAssistantClimateService(
            readonly_client=client,
            trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
        )

        plan = await service.assess_setpoint_feasibility(
            target_entity_id=TEST_CLIMATE_A,
            requested_temperature=21.0,
            requested_unit="°C",
        )
        assert plan.is_feasible is True
        assert plan.feasibility_status == "ready"
        assert len(call_log["mutation_calls"]) == 0

        # Without grid_origin, fails closed as step_alignment_unknown
        service_no_origin = HomeAssistantClimateService(readonly_client=client)
        plan_no_origin = await service_no_origin.assess_setpoint_feasibility(
            target_entity_id=TEST_CLIMATE_A,
            requested_temperature=21.0,
            requested_unit="°C",
        )
        assert plan_no_origin.is_feasible is False
        assert plan_no_origin.feasibility_status == "step_alignment_unknown"


@pytest.mark.anyio
async def test_boundary_temperatures_at_min_and_max() -> None:
    """Exact min_temp and max_temp are feasible when aligned with step and origin is established."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # TEST_CLIMATE_A has min=16.0, max=28.0, step=0.5
    # 1. At min_temp with server grid origin=16.0 -> ready
    plan_min = await service.assess_setpoint_feasibility(
        TEST_CLIMATE_A, 16.0, "°C"
    )
    assert plan_min.is_feasible is True
    assert plan_min.feasibility_status == "ready"

    # At min_temp without server grid origin -> step_alignment_unknown
    service_no_origin = HomeAssistantClimateService(readonly_client=client)
    plan_min_no_origin = await service_no_origin.assess_setpoint_feasibility(
        TEST_CLIMATE_A, 16.0, "°C"
    )
    assert plan_min_no_origin.is_feasible is False
    assert plan_min_no_origin.feasibility_status == "step_alignment_unknown"

    # 2. At max_temp with server grid origin=16.0 -> ready
    plan_max = await service.assess_setpoint_feasibility(
        TEST_CLIMATE_A, 28.0, "°C"
    )
    assert plan_max.is_feasible is True
    assert plan_max.feasibility_status == "ready"

    # 3. Just below min_temp
    plan_below = await service.assess_setpoint_feasibility(
        TEST_CLIMATE_A, 15.5, "°C"
    )
    assert plan_below.is_feasible is False
    assert plan_below.feasibility_status == "target_out_of_range"

    # 4. Just above max_temp
    plan_above = await service.assess_setpoint_feasibility(
        TEST_CLIMATE_A, 28.5, "°C"
    )
    assert plan_above.is_feasible is False
    assert plan_above.feasibility_status == "target_out_of_range"

    assert len(call_log["mutation_calls"]) == 0


def test_sanitizer_rejects_booleans_for_numeric_attributes() -> None:
    """Sanitizer must reject boolean values supplied for numeric attributes."""
    raw = {
        "friendly_name": "Test Thermostat",
        "supported_features": True,
        "target_temp_step": False,
        "min_temp": True,
        "max_temp": False,
        "temperature": True,
        "current_temperature": False,
        "unit_of_measurement": "°C",
    }
    sanitized = sanitize_entity_attributes(raw)
    assert sanitized["friendly_name"] == "Test Thermostat"
    assert sanitized["unit_of_measurement"] == "°C"
    assert "supported_features" not in sanitized
    assert "target_temp_step" not in sanitized
    assert "min_temp" not in sanitized
    assert "max_temp" not in sanitized
    assert "temperature" not in sanitized
    assert "current_temperature" not in sanitized


def test_sanitizer_accepts_valid_supported_features_and_step() -> None:
    """Sanitizer preserves valid integer supported_features and numeric target_temp_step."""
    raw = {
        "supported_features": 1,
        "target_temp_step": 0.5,
        "min_temp": 16.0,
        "max_temp": 28.0,
        "unit_of_measurement": "°C",
    }
    sanitized = sanitize_entity_attributes(raw)
    assert sanitized["supported_features"] == 1
    assert sanitized["target_temp_step"] == 0.5
    assert sanitized["min_temp"] == 16.0
    assert sanitized["max_temp"] == 28.0


# =====================================================================
# 11. Milestone P7.4.2.1.3: Trusted Climate Grid-Origin Configuration
# =====================================================================

@pytest.mark.anyio
async def test_caller_supplied_grid_origin_rejected_in_api() -> None:
    """A client cannot supply grid_origin in API requests; must receive 422 Unprocessable Entity."""
    transport, call_log = make_mock_climate_transport()
    real_client_cls = httpx.AsyncClient
    with patch("pico.homeassistant.client.httpx.AsyncClient", side_effect=lambda **kwargs: real_client_cls(transport=transport)):
        with TestClient(app) as test_client:
            resp = test_client.post(
                "/api/homeassistant/climate/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temperature": 21.5,
                    "target_unit": "°C",
                    "grid_origin": 16.0,
                },
            )
            assert resp.status_code == 422
            detail = resp.json()["detail"]
            assert any("Client-supplied 'grid_origin' is forbidden" in str(d) for d in detail)
            assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_client_origin_equal_to_requested_temp_cannot_force_ready() -> None:
    """Client supplying origin equal to requested temperature cannot forge feasibility; request is rejected."""
    transport, call_log = make_mock_climate_transport()
    real_client_cls = httpx.AsyncClient
    with patch("pico.homeassistant.client.httpx.AsyncClient", side_effect=lambda **kwargs: real_client_cls(transport=transport)):
        with TestClient(app) as test_client:
            # 21.3 is an odd fraction (misaligned with step 0.5 from any genuine 0.5-grid anchor)
            resp = test_client.post(
                "/api/homeassistant/climate/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temperature": 21.3,
                    "target_unit": "°C",
                    "grid_origin": 21.3,
                },
            )
            assert resp.status_code == 422
            assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_unconfigured_entity_fails_closed_as_step_alignment_unknown() -> None:
    """An allowlisted entity without server-configured grid origin fails closed as step_alignment_unknown."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(readonly_client=client, trusted_grid_origins={})

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.5,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "step_alignment_unknown"
    assert plan.grid_origin is None
    assert plan.grid_origin_unit is None
    assert "no trusted server-side step-grid origin is configured" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_valid_operator_configured_origin_allows_step_alignment() -> None:
    """Valid operator configuration allows step alignment calculation."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.5,
        requested_unit="°C",
    )
    assert plan.is_feasible is True
    assert plan.feasibility_status == "ready"
    assert plan.grid_origin == 16.0
    assert plan.grid_origin_unit == "°C"
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_misaligned_request_relative_to_configured_origin_returns_mismatch() -> None:
    """Request misaligned relative to configured origin returns target_step_mismatch."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.3,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_step_mismatch"
    assert "does not align with supported step increment 0.5" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


def test_grid_origin_outside_allowlist_rejected() -> None:
    """Configuring an origin for an entity not in the climate allowlist raises ValidationError."""
    allowlist = {TEST_CLIMATE_A}
    config = {
        "climate.unallowlisted_thermostat": {"origin": 16.0, "unit": "°C"}
    }
    with pytest.raises(ValidationError, match="not in HOME_ASSISTANT_CLIMATE_ALLOWLIST"):
        validate_climate_grid_origins(raw_config=config, allowlist=allowlist)


@pytest.mark.anyio
async def test_grid_origin_outside_allowlist_fails_closed_in_planner() -> None:
    """If server configuration has an unallowlisted entity, planner fails closed as step_alignment_unknown."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={"climate.not_allowlisted": {"origin": 16.0, "unit": "°C"}},
    )
    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.5, "°C")
    assert plan.is_feasible is False
    assert plan.feasibility_status == "step_alignment_unknown"
    assert "Server-side climate grid origin configuration error" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_grid_origin_unit_mismatch_returns_step_alignment_unknown() -> None:
    """Unit mismatch between configured origin (°F) and observed entity (°C) returns step_alignment_unknown."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 60.0, "unit": "°F"}},
    )

    plan = await service.assess_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        requested_temperature=21.0,
        requested_unit="°C",
    )
    assert plan.is_feasible is False
    assert plan.feasibility_status == "step_alignment_unknown"
    assert "Configured grid origin unit '°F' does not match entity's reported temperature unit '°C'" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


def test_grid_origin_validation_fail_closed_on_invalid_values() -> None:
    """Boolean, NaN, infinity, negative, and malformed origin values fail closed."""
    allowlist = {TEST_CLIMATE_A}

    # Boolean origin
    with pytest.raises(ValidationError, match="Invalid origin True"):
        validate_climate_grid_origins({TEST_CLIMATE_A: {"origin": True, "unit": "°C"}}, allowlist=allowlist)

    # NaN origin
    with pytest.raises(ValidationError, match="Non-finite origin"):
        validate_climate_grid_origins({TEST_CLIMATE_A: {"origin": float("nan"), "unit": "°C"}}, allowlist=allowlist)

    # Infinity origin
    with pytest.raises(ValidationError, match="Non-finite origin"):
        validate_climate_grid_origins({TEST_CLIMATE_A: {"origin": float("inf"), "unit": "°C"}}, allowlist=allowlist)

    # Negative origin
    with pytest.raises(ValidationError, match="negative origins are not permitted"):
        validate_climate_grid_origins({TEST_CLIMATE_A: {"origin": -5.0, "unit": "°C"}}, allowlist=allowlist)

    # Malformed unit
    with pytest.raises(ValidationError, match="Unrecognized temperature unit"):
        validate_climate_grid_origins({TEST_CLIMATE_A: {"origin": 16.0, "unit": "invalid_unit"}}, allowlist=allowlist)

    # Extra unexpected fields
    with pytest.raises(ValidationError, match="unexpected fields"):
        validate_climate_grid_origins({TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C", "extra": "forbidden"}}, allowlist=allowlist)

    # Missing fields
    with pytest.raises(ValidationError, match="missing fields"):
        validate_climate_grid_origins({TEST_CLIMATE_A: {"origin": 16.0}}, allowlist=allowlist)

    # Wildcards in entity ID
    with pytest.raises(ValidationError, match="Wildcards"):
        validate_climate_grid_origins({"climate.*": {"origin": 16.0, "unit": "°C"}}, allowlist=allowlist)

    # Non-climate domain
    with pytest.raises(ValidationError, match="Climate grid origins are strictly restricted"):
        validate_climate_grid_origins({"light.living_room": {"origin": 16.0, "unit": "°C"}}, allowlist=allowlist)

    # Malformed JSON string
    with pytest.raises(ValidationError, match="Malformed JSON"):
        validate_climate_grid_origins("not valid json", allowlist=allowlist)

    # Duplicate keys in JSON
    with pytest.raises(ValidationError, match="Duplicate entity ID"):
        validate_climate_grid_origins(
            f'{{"{TEST_CLIMATE_A}": {{"origin": 16.0, "unit": "°C"}}, "{TEST_CLIMATE_A}": {{"origin": 17.0, "unit": "°C"}}}}',
            allowlist=allowlist,
        )


@pytest.mark.anyio
async def test_invalid_or_missing_step_metadata_remains_step_unknown_with_origin() -> None:
    """Missing or invalid target_temp_step remains step_unknown even when trusted origin is configured."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "supported_features": 1,
                "unit_of_measurement": "°C",
                # step omitted
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0, "°C")
    assert plan.is_feasible is False
    assert plan.feasibility_status == "step_unknown"
    assert "does not report target_temp_step metadata" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_changing_configured_origin_changes_canonical_plan_digest() -> None:
    """Changing the server-configured origin changes the canonical plan digest."""
    client, _ = make_mock_climate_client()

    service1 = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )
    plan1 = await service1.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0, "°C")

    service2 = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.5, "unit": "°C"}},
    )
    plan2 = await service2.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0, "°C")

    assert plan1.input_digest != plan2.input_digest
    assert plan1.grid_origin == 16.0
    assert plan2.grid_origin == 16.5


@pytest.mark.anyio
async def test_friendly_name_and_model_output_cannot_override_server_configuration() -> None:
    """Attributes, friendly names, or caller payloads cannot override server origin configuration."""
    payloads = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "friendly_name": "Living Room Thermostat (origin=21.3)",  # Attempted prompt injection in name
                "grid_origin": 21.3,  # Attempted attribute injection
                "current_temperature": 20.5,
                "min_temp": 16.0,
                "max_temp": 28.0,
                "target_temp_step": 0.5,
                "supported_features": 1,
                "unit_of_measurement": "°C",
            },
        }
    }
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.3, "°C")
    # 21.3 is misaligned with step 0.5 from server origin 16.0, despite friendly_name / attribute claiming 21.3
    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_step_mismatch"
    assert plan.grid_origin == 16.0
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_zero_mutation_calls_strictly_asserted_across_all_paths() -> None:
    """Strictly assert zero mutation calls directly on mock client call log across all observation, plan, and readiness paths."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # 1. Observation
    await service.get_climate_observation(TEST_CLIMATE_A)
    # 2. Plan ready
    await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0, "°C")
    # 3. Plan mismatch
    await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.3, "°C")
    # 4. Readiness report
    await evaluate_climate_readiness(check_live=True, readonly_client=client)

    assert len(call_log["mutation_calls"]) == 0
    assert len(call_log["read_calls"]) > 0


# =====================================================================
# Milestone P7.4.2.3: Read-Only Dual-Setpoint Feasibility Planning
# =====================================================================

def make_heat_cool_payload(
    entity_id: str = TEST_CLIMATE_A,
    state: str = "heat_cool",
    current_temp: float | None = 21.0,
    target_temp_low: float | None = 20.0,
    target_temp_high: float | None = 24.0,
    min_temp: float | None = 16.0,
    max_temp: float | None = 28.0,
    step: float | None = 0.5,
    supported_features: int | None = 2,  # Bit 1 = TARGET_TEMPERATURE_RANGE
    unit: str | None = "°C",
    modes: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Helper to generate a mock climate entity payload for dual-setpoint testing."""
    attrs: dict[str, Any] = {
        "friendly_name": "Test Dual-Setpoint Climate",
        "current_temperature": current_temp,
        "target_temp_low": target_temp_low,
        "target_temp_high": target_temp_high,
        "min_temp": min_temp,
        "max_temp": max_temp,
        "target_temp_step": step,
        "supported_features": supported_features,
        "unit_of_measurement": unit,
        "hvac_modes": modes or ["heat_cool", "off"],
    }
    return {
        entity_id: {
            "entity_id": entity_id,
            "state": state,
            "attributes": attrs,
        }
    }


@pytest.mark.anyio
async def test_dual_setpoint_plan_success_valid_range_and_step() -> None:
    """Valid lower and upper target range for a supported entity already in heat_cool returns ready."""
    payloads = make_heat_cool_payload()
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.5,
        requested_unit="°C",
    )

    assert plan.is_feasible is True
    assert plan.feasibility_status == "ready"
    assert plan.is_mutation_performed is False
    assert plan.target_temp_low == 19.0
    assert plan.target_temp_high == 23.5
    assert plan.current_target_temp_low == 20.0
    assert plan.current_target_temp_high == 24.0
    assert plan.grid_origin == 16.0
    assert plan.grid_origin_unit == "°C"
    assert plan.temperature_unit == "°C"
    assert plan.hvac_mode == "heat_cool"
    assert "Guarantees metadata feasibility only" in plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_dual_setpoint_plan_success_without_requested_unit() -> None:
    """Requested unit is optional; planner succeeds and inherits reported unit."""
    payloads = make_heat_cool_payload()
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=18.0,
        target_temp_high=22.0,
        requested_unit=None,
    )

    assert plan.is_feasible is True
    assert plan.feasibility_status == "ready"
    assert plan.requested_unit is None
    assert plan.temperature_unit == "°C"
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("incompatible_mode", ["heat", "cool", "off", "auto", "fan_only", "dry"])
async def test_dual_setpoint_plan_rejects_incompatible_modes_without_switching(incompatible_mode: str) -> None:
    """Planner rejects incompatible modes and NEVER switches the entity into heat_cool."""
    payloads = make_heat_cool_payload(state=incompatible_mode)
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
        requested_unit="°C",
    )

    assert plan.is_feasible is False
    assert plan.feasibility_status == "mode_incompatible"
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "features,expected_status",
    [
        (1, "unsupported_capability"),   # Bit 0 only (single setpoint only)
        (0, "unsupported_capability"),   # Neither bit
        (None, "capability_unknown"),     # Missing supported_features
        (-1, "capability_unknown"),       # Negative / invalid
    ],
)
async def test_dual_setpoint_plan_rejects_missing_or_unsupported_features(features: int | None, expected_status: str) -> None:
    """Planner fails closed when dual-setpoint feature (Bit 1) is not explicitly supported."""
    payloads = make_heat_cool_payload(supported_features=features)
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
        requested_unit="°C",
    )

    assert plan.is_feasible is False
    assert plan.feasibility_status == expected_status
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "low,high",
    [
        (True, 24.0),           # Boolean low
        (20.0, False),          # Boolean high
        (float("inf"), 24.0),   # Infinite low
        (20.0, float("nan")),   # NaN high
        ("20.0", 24.0),         # Non-numeric string low
    ],
)
async def test_dual_setpoint_plan_rejects_boolean_and_non_finite_inputs(low: Any, high: Any) -> None:
    """Boolean, non-finite, and malformed inputs are strictly rejected without mutation."""
    payloads = make_heat_cool_payload()
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=low,
        target_temp_high=high,
        requested_unit="°C",
    )

    assert plan.is_feasible is False
    assert plan.feasibility_status == "state_unknown"
    assert "finite numeric values" in plan.reasons[0]
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "low,high",
    [
        (22.0, 22.0),  # Equal targets
        (24.0, 20.0),  # Inverted range
    ],
)
async def test_dual_setpoint_plan_rejects_equal_or_inverted_range(low: float, high: float) -> None:
    """Target range ordering (lower < upper) is strictly enforced."""
    payloads = make_heat_cool_payload()
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=low,
        target_temp_high=high,
        requested_unit="°C",
    )

    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_out_of_range"
    assert "strictly less than" in plan.reasons[0]
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "low,high,reason_substr",
    [
        (14.0, 22.0, "below minimum supported setpoint"),
        (18.0, 30.0, "above maximum supported setpoint"),
        (29.0, 30.0, "above maximum supported setpoint"),
        (14.0, 15.0, "below minimum supported setpoint"),
    ],
)
async def test_dual_setpoint_plan_rejects_out_of_bounds_targets_independently(
    low: float,
    high: float,
    reason_substr: str,
) -> None:
    """Both lower and upper targets must independently lie within [min_temp, max_temp]."""
    payloads = make_heat_cool_payload(min_temp=16.0, max_temp=28.0)
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=low,
        target_temp_high=high,
        requested_unit="°C",
    )

    assert plan.is_feasible is False
    assert plan.feasibility_status == "target_out_of_range"
    assert reason_substr in plan.reasons[0]
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_dual_setpoint_plan_rejects_unit_mismatch() -> None:
    """Mismatched or unrecognized temperature units fail closed."""
    payloads = make_heat_cool_payload(unit="°C")
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # Unit conflict (°F requested on °C entity)
    plan_conflict = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
        requested_unit="°F",
    )
    assert plan_conflict.is_feasible is False
    assert plan_conflict.feasibility_status == "unit_unknown"
    assert "conflicts with entity's reported unit" in plan_conflict.reasons[0]

    # Unrecognized unit
    plan_unknown = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
        requested_unit="UnknownUnit",
    )
    assert plan_unknown.is_feasible is False
    assert plan_unknown.feasibility_status == "unit_unknown"
    assert "unrecognized or unsupported" in plan_unknown.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("bad_step", [None, 0.0, -0.5, True, "invalid"])
async def test_dual_setpoint_plan_rejects_missing_or_invalid_step(bad_step: Any) -> None:
    """Missing, zero, negative, or non-numeric step metadata fails closed with step_unknown."""
    payloads = make_heat_cool_payload(step=bad_step)
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    plan = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
        requested_unit="°C",
    )

    assert plan.is_feasible is False
    assert plan.feasibility_status == "step_unknown"
    assert plan.is_mutation_performed is False
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_dual_setpoint_plan_rejects_missing_or_untrusted_grid_origin() -> None:
    """Without trusted server-side grid origin, step alignment cannot be verified."""
    payloads = make_heat_cool_payload(step=0.5)
    client, call_log = make_mock_climate_client(entity_payloads=payloads)

    # 1. No grid origin configured at all
    service_no_origin = HomeAssistantClimateService(readonly_client=client, trusted_grid_origins={})
    plan_no_origin = await service_no_origin.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
        requested_unit="°C",
    )
    assert plan_no_origin.is_feasible is False
    assert plan_no_origin.feasibility_status == "step_alignment_unknown"
    assert "no trusted server-side step-grid origin is configured" in plan_no_origin.reasons[0]

    # 2. Grid origin configured with conflicting unit
    service_bad_unit = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 60.0, "unit": "°F"}},
    )
    plan_bad_unit = await service_bad_unit.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
        requested_unit="°C",
    )
    assert plan_bad_unit.is_feasible is False
    assert plan_bad_unit.feasibility_status == "step_alignment_unknown"
    assert "does not match entity's reported temperature unit" in plan_bad_unit.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_dual_setpoint_plan_rejects_misaligned_targets_independently() -> None:
    """Misaligned lower target and misaligned upper target are independently caught."""
    payloads = make_heat_cool_payload(step=0.5)
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # Lower target misaligned (19.3 with step 0.5 from origin 16.0)
    plan_low_bad = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.3,
        target_temp_high=24.0,
        requested_unit="°C",
    )
    assert plan_low_bad.is_feasible is False
    assert plan_low_bad.feasibility_status == "target_step_mismatch"
    assert "19.3" in plan_low_bad.reasons[0]

    # Upper target misaligned (23.7 with step 0.5 from origin 16.0)
    plan_high_bad = await service.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.7,
        requested_unit="°C",
    )
    assert plan_high_bad.is_feasible is False
    assert plan_high_bad.feasibility_status == "target_step_mismatch"
    assert "23.7" in plan_high_bad.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_dual_setpoint_plan_digest_sensitivity() -> None:
    """Canonical SHA-256 digest is sensitive to lower target, upper target, unit, and grid origin."""
    payloads = make_heat_cool_payload()
    client, _ = make_mock_climate_client(entity_payloads=payloads)

    service1 = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )
    base_plan = await service1.assess_dual_setpoint_feasibility(TEST_CLIMATE_A, 19.0, 23.0, "°C")

    # Change lower target
    plan_diff_low = await service1.assess_dual_setpoint_feasibility(TEST_CLIMATE_A, 19.5, 23.0, "°C")
    assert base_plan.input_digest != plan_diff_low.input_digest

    # Change upper target
    plan_diff_high = await service1.assess_dual_setpoint_feasibility(TEST_CLIMATE_A, 19.0, 23.5, "°C")
    assert base_plan.input_digest != plan_diff_high.input_digest

    # Change unit
    plan_diff_unit = await service1.assess_dual_setpoint_feasibility(TEST_CLIMATE_A, 19.0, 23.0, None)
    assert base_plan.input_digest != plan_diff_unit.input_digest

    # Change grid origin
    service2 = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.5, "unit": "°C"}},
    )
    plan_diff_origin = await service2.assess_dual_setpoint_feasibility(TEST_CLIMATE_A, 19.0, 23.0, "°C")
    assert base_plan.input_digest != plan_diff_origin.input_digest


@pytest.mark.anyio
async def test_dual_setpoint_plan_rejects_unallowlisted_or_unavailable_entity() -> None:
    """Exact allowlist and availability checks are strictly enforced."""
    client, call_log = make_mock_climate_client()
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    # 1. Unallowlisted entity
    plan_unallowlisted = await service.assess_dual_setpoint_feasibility(
        target_entity_id="climate.unallowlisted_thermostat",
        target_temp_low=19.0,
        target_temp_high=23.0,
    )
    assert plan_unallowlisted.is_feasible is False
    assert plan_unallowlisted.feasibility_status == "not_allowlisted"

    # 2. Entity state unavailable
    payloads_unavailable = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "unavailable",
            "attributes": {},
        }
    }
    client_unavail, _ = make_mock_climate_client(entity_payloads=payloads_unavailable)
    service_unavail = HomeAssistantClimateService(
        readonly_client=client_unavail,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )
    plan_unavailable = await service_unavail.assess_dual_setpoint_feasibility(
        target_entity_id=TEST_CLIMATE_A,
        target_temp_low=19.0,
        target_temp_high=23.0,
    )
    assert plan_unavailable.is_feasible is False
    assert plan_unavailable.feasibility_status == "state_unavailable"
    assert len(call_log["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_single_setpoint_planner_still_rejects_heat_cool() -> None:
    """Verify regression safety: single-setpoint planner still rejects heat_cool operating mode."""
    payloads = make_heat_cool_payload(state="heat_cool", supported_features=3)
    client, call_log = make_mock_climate_client(entity_payloads=payloads)
    service = HomeAssistantClimateService(
        readonly_client=client,
        trusted_grid_origins={TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"}},
    )

    single_plan = await service.assess_setpoint_feasibility(TEST_CLIMATE_A, 21.0, "°C")
    assert single_plan.is_feasible is False
    assert single_plan.feasibility_status == "mode_incompatible"
    assert "requires a dual-setpoint temperature range" in single_plan.reasons[0]
    assert len(call_log["mutation_calls"]) == 0


def test_dual_setpoint_api_endpoint_success_and_adversarial_validation() -> None:
    """Test POST /api/homeassistant/climate/dual-setpoint/plan endpoint via TestClient."""
    payloads = make_heat_cool_payload()
    transport, call_log = make_mock_climate_transport(entity_payloads=payloads)

    real_client_cls = httpx.AsyncClient
    with patch(
        "pico.homeassistant.client.httpx.AsyncClient",
        side_effect=lambda **kwargs: real_client_cls(transport=transport),
    ):
        with patch.object(
            settings,
            "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS",
            f'{{"{TEST_CLIMATE_A}": {{"origin": 16.0, "unit": "°C"}}}}',
        ):
            client = TestClient(app)

            # 1. Valid request -> 200 OK
            resp_ok = client.post(
                "/api/homeassistant/climate/dual-setpoint/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temp_low": 19.0,
                    "target_temp_high": 23.5,
                    "target_unit": "°C",
                },
            )
            assert resp_ok.status_code == 200
            data_ok = resp_ok.json()
            assert data_ok["is_feasible"] is True
            assert data_ok["feasibility_status"] == "ready"
            assert data_ok["is_mutation_performed"] is False
            assert data_ok["target_temp_low"] == 19.0
            assert data_ok["target_temp_high"] == 23.5

            # 2. Client attempting to supply grid_origin -> 422 Unprocessable Entity
            resp_grid = client.post(
                "/api/homeassistant/climate/dual-setpoint/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temp_low": 19.0,
                    "target_temp_high": 23.5,
                    "grid_origin": 19.0,
                },
            )
            assert resp_grid.status_code == 422
            assert "Client-supplied 'grid_origin' is forbidden" in resp_grid.text

            # 3. Boolean input -> 422 Unprocessable Entity
            resp_bool = client.post(
                "/api/homeassistant/climate/dual-setpoint/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temp_low": True,
                    "target_temp_high": 23.5,
                },
            )
            assert resp_bool.status_code == 422

            # 4. Unexpected extra fields -> 422 (model_config extra='forbid')
            resp_extra = client.post(
                "/api/homeassistant/climate/dual-setpoint/plan",
                json={
                    "target_entity_id": TEST_CLIMATE_A,
                    "target_temp_low": 19.0,
                    "target_temp_high": 23.5,
                    "unexpected_field": "exploit",
                },
            )
            assert resp_extra.status_code == 422

            # 5. Remote caller without approval token -> 403 Forbidden
            with patch("pico.api.homeassistant.is_loopback_host", return_value=False):
                resp_remote = client.post(
                    "/api/homeassistant/climate/dual-setpoint/plan",
                    json={
                        "target_entity_id": TEST_CLIMATE_A,
                        "target_temp_low": 19.0,
                        "target_temp_high": 23.5,
                    },
                )
                assert resp_remote.status_code == 403

            # Zero physical mutations performed across all API tests
            assert len(call_log["mutation_calls"]) == 0
