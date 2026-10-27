import asyncio
import hashlib
import json
import math
import secrets
from typing import Any
import pytest
from fastapi.testclient import TestClient
import httpx
from sqlalchemy.orm import Session, sessionmaker

from pico.capabilities.base import RiskTier
from pico.capabilities.homeassistant.ha_actuation import (
    HomeAssistantClimateActuationCapability,
    HomeAssistantClimateActuationInput,
    HomeAssistantClimateActuationOutput,
)
from pico.capabilities.registry import CapabilityRegistry, registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.homeassistant.actuation import (
    HomeAssistantActuationService,
    HomeAssistantClimateActuationService,
    HomeAssistantSwitchActuationService,
    validate_actuation_preconditions,
    validate_climate_actuation_preconditions,
    validate_switch_actuation_preconditions,
)
from pico.homeassistant.climate import (
    HomeAssistantClimateService,
    validate_climate_allowlist,
    validate_climate_grid_origins,
)
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    ClimateActuationPlan,
    ClimateActuationResult,
    ClimateReadinessReport,
)
from pico.homeassistant.readiness import evaluate_climate_readiness
from pico.homeassistant.ws_client import ALLOWED_READONLY_WS_COMMANDS
from pico.main import app
from pico.models.event_ledger import LedgerEvent
from pico.models.tool_execution import ToolExecution
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.tool_execution_repo import ToolExecutionRepository
from pico.services.execution_engine import ToolExecutionEngine


# =====================================================================
# Fixtures & Helpers
# =====================================================================

TEST_CLIMATE_A = "climate.living_room_thermostat"
TEST_CLIMATE_B = "climate.bedroom_ac"
DEFAULT_CLIMATE_ALLOWLIST = [TEST_CLIMATE_A, TEST_CLIMATE_B]
DEFAULT_GRID_ORIGINS = {
    TEST_CLIMATE_A: {"origin": 16.0, "unit": "°C"},
    TEST_CLIMATE_B: {"origin": 60.0, "unit": "°F"},
}
MOCK_BASE_URL = "http://127.0.0.1:8123"
MOCK_TOKEN = "valid_test_token_climate_actuation"
MOCK_APPROVAL_SECRET = "valid_climate_secret_token"


@pytest.fixture(autouse=True)
def configure_climate_actuation_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default fixture for P7.4.2.2 tests: HA enabled and Stage D climate actuation configured."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", MOCK_BASE_URL)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", MOCK_TOKEN)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ALLOWLIST", json.dumps(DEFAULT_CLIMATE_ALLOWLIST))
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS", json.dumps(DEFAULT_GRID_ORIGINS))
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_VERIFICATION_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_VERIFICATION_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_APPROVAL_SECRET)


def make_mock_climate_transport(
    initial_states: dict[str, dict[str, Any]] | None = None,
    service_status_code: int = 200,
    service_timeout: bool = False,
    mutate_on_service: bool = True,
) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """Create a mock httpx transport tracking calls and simulated climate states."""
    default_states: dict[str, dict[str, Any]] = {
        TEST_CLIMATE_A: {
            "entity_id": TEST_CLIMATE_A,
            "state": "heat",
            "attributes": {
                "friendly_name": "Living Room Thermostat",
                "current_temperature": 20.0,
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
                "current_temperature": 74.0,
                "temperature": 72.0,
                "min_temp": 60.0,
                "max_temp": 85.0,
                "target_temp_step": 1.0,
                "supported_features": 1,
                "unit_of_measurement": "°F",
                "hvac_modes": ["off", "cool", "fan_only"],
            },
        },
    }

    current_states = dict(initial_states if initial_states is not None else default_states)
    state_box: dict[str, Any] = {
        "states": current_states,
        "mutation_calls": [],
        "read_calls": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path

        # 1. State reading endpoint: /api/states/{entity_id}
        if request.method == "GET" and url_path.startswith("/api/states/"):
            entity_id = url_path.split("/")[-1]
            state_box["read_calls"].append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
            })
            if entity_id in state_box["states"]:
                return httpx.Response(200, json=state_box["states"][entity_id])
            return httpx.Response(404, json={"message": "Entity not found"})

        # 2. Service mutation endpoint: /api/services/climate/set_temperature
        if request.method == "POST" and url_path == "/api/services/climate/set_temperature":
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            state_box["mutation_calls"].append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body,
            })
            if service_timeout:
                raise httpx.TimeoutException("Mocked climate service call timed out")
            if service_status_code != 200:
                return httpx.Response(service_status_code, json={"message": "Service error"})

            target_id = body.get("entity_id")
            new_temp = body.get("temperature")
            if mutate_on_service and target_id in state_box["states"]:
                state_box["states"][target_id]["attributes"]["temperature"] = new_temp

            return httpx.Response(200, json=[{"entity_id": target_id, "state": state_box["states"].get(target_id, {}).get("state")}])

        # Any other path is unexpected
        return httpx.Response(404, json={"message": f"Unhandled path: {url_path}"})

    transport = httpx.MockTransport(handler)
    return transport, state_box


# =====================================================================
# 1. Configuration & Enablement Tests
# =====================================================================

def test_climate_actuation_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED is False by default on pristine settings."""
    from pico.core.config import Settings
    pristine = Settings(_env_file=None)
    assert pristine.HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED is False


def test_missing_integration_gate_blocks_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME_ASSISTANT_ENABLED=False raises AuthorizationError."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", False)
    with pytest.raises(AuthorizationError, match="HOME_ASSISTANT_ENABLED=false"):
        validate_climate_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, target_temperature=22.0)


def test_missing_access_token_blocks_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing or empty access token raises AuthorizationError."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", "")
    with pytest.raises(AuthorizationError, match="access token is missing"):
        validate_climate_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, target_temperature=22.0)


def test_missing_actuation_gate_blocks_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME_ASSISTANT_ACTUATION_ENABLED=False raises AuthorizationError."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", False)
    with pytest.raises(AuthorizationError, match="HOME_ASSISTANT_ACTUATION_ENABLED=true"):
        validate_climate_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, target_temperature=22.0)


def test_missing_climate_awareness_gate_blocks_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=False raises AuthorizationError."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False)
    with pytest.raises(AuthorizationError, match="HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=true"):
        validate_climate_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, target_temperature=22.0)


def test_missing_climate_actuation_gate_blocks_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED=False raises AuthorizationError."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", False)
    with pytest.raises(AuthorizationError, match="HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED=true"):
        validate_climate_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, target_temperature=22.0)


def test_switch_or_light_enablement_does_not_enable_climate_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling Stage C switch actuation does not authorize climate actuation."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", False)
    with pytest.raises(AuthorizationError, match="HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED=true"):
        validate_climate_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, target_temperature=22.0)


def test_unallowlisted_climate_entity_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A climate.* entity not in the Stage D allowlist is rejected with ValidationError."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ALLOWLIST", json.dumps([TEST_CLIMATE_A]))
    with pytest.raises(ValidationError, match="not in the Stage D climate allowlist"):
        validate_climate_actuation_preconditions(target_entity_id="climate.unauthorized_thermostat", target_temperature=22.0)


def test_non_climate_domain_rejected() -> None:
    """Entities from non-climate domains (light, switch) are rejected with ValidationError."""
    with pytest.raises(ValidationError, match="Only the 'climate' domain is permitted"):
        validate_climate_actuation_preconditions(target_entity_id="switch.living_room_plug", target_temperature=22.0)
    with pytest.raises(ValidationError, match="Only the 'climate' domain is permitted"):
        validate_climate_actuation_preconditions(target_entity_id="light.desk_lamp", target_temperature=22.0)


def test_wildcard_and_whitespace_variations_rejected() -> None:
    """Wildcards and whitespace variations are rejected fail-closed."""
    for bad_id in [
        f" {TEST_CLIMATE_A}",
        f"{TEST_CLIMATE_A} ",
        "climate.*",
        "climate.living_room_?",
        "climate/living_room",
    ]:
        with pytest.raises(ValidationError):
            validate_climate_actuation_preconditions(target_entity_id=bad_id, target_temperature=22.0)


def test_no_generic_service_execution_capability_registered() -> None:
    """Verify that no generic call_service capability exists."""
    assert not registry.has("homeassistant.call_service")
    assert not registry.has("homeassistant.climate_call_service")
    assert registry.has("homeassistant.allowlisted_climate_setpoint_actuate")


def test_ws_command_allowlist_remains_read_only() -> None:
    """WebSocket allowlist remains strictly read-only."""
    assert "call_service" not in ALLOWED_READONLY_WS_COMMANDS
    assert ALLOWED_READONLY_WS_COMMANDS == {"get_states", "get_config", "ping"}


# =====================================================================
# 2. Temperature Feasibility & Pre-Dispatch Boundary Tests
# =====================================================================

@pytest.mark.anyio
async def test_valid_requested_temperature_creates_ready_plan() -> None:
    """Valid setpoint aligned with server grid origin and in compatible mode produces ready plan."""
    transport, state_box = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        plan = await service.create_plan(
            target_entity_id=TEST_CLIMATE_A,
            target_temperature=22.5,
            target_unit="°C",
        )
        assert plan.state_readiness == "ready"
        assert plan.is_mutation_needed is True
        assert plan.requested_temperature == 22.5
        assert plan.entity_id == TEST_CLIMATE_A
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_already_satisfied_setpoint_skips_mutation() -> None:
    """When reported target temperature already matches requested temperature, mutation is skipped."""
    transport, state_box = make_mock_climate_transport()
    # In initial state, TEST_CLIMATE_A has temperature=21.0
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        plan = await service.create_plan(
            target_entity_id=TEST_CLIMATE_A,
            target_temperature=21.0,
            target_unit="°C",
        )
        assert plan.state_readiness == "already_satisfied"
        assert plan.is_mutation_needed is False

        # Dispatch with skip_if_already_in_state=True issues zero mutations
        res = await service.dispatch_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temperature=21.0,
            target_unit="°C",
        )
        assert res["dispatched"] is False
        assert res["dispatch_status"] == "already_satisfied"
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_out_of_range_temperature_blocks_dispatch() -> None:
    """Target temperature below min_temp or above max_temp blocks dispatch with zero mutations."""
    transport, state_box = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        # min_temp is 16.0, request 14.0
        res = await service.dispatch_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temperature=14.0,
            target_unit="°C",
        )
        assert res["dispatched"] is False
        assert res["dispatch_status"] == "blocked_precondition"
        assert "target_out_of_range" in res["reason"]
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_incompatible_operating_mode_blocks_dispatch() -> None:
    """Incompatible operating modes (off, fan_only, auto, heat_cool, dry) block dispatch."""
    for bad_mode in ["off", "fan_only", "auto", "heat_cool", "dry"]:
        states = {
            TEST_CLIMATE_A: {
                "entity_id": TEST_CLIMATE_A,
                "state": bad_mode,
                "attributes": {
                    "current_temperature": 20.0,
                    "temperature": 21.0,
                    "min_temp": 16.0,
                    "max_temp": 28.0,
                    "target_temp_step": 0.5,
                    "supported_features": 1,
                    "unit_of_measurement": "°C",
                    "hvac_modes": [bad_mode],
                },
            }
        }
        transport, state_box = make_mock_climate_transport(initial_states=states)
        async with httpx.AsyncClient(transport=transport) as http_client:
            ro_client = HomeAssistantClient(http_client=http_client)
            service = HomeAssistantClimateActuationService(
                readonly_client=ro_client,
                http_client=http_client,
            )

            res = await service.dispatch_actuation(
                target_entity_id=TEST_CLIMATE_A,
                target_temperature=22.0,
                target_unit="°C",
            )
            assert res["dispatched"] is False
            assert res["dispatch_status"] == "blocked_precondition"
            assert "mode_incompatible" in res["reason"]
            assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_step_misalignment_blocks_dispatch() -> None:
    """A temperature that does not align with the step grid relative to server origin blocks dispatch."""
    transport, state_box = make_mock_climate_transport()
    # Step is 0.5, origin is 16.0. 22.3 is misaligned (offset 6.3 % 0.5 != 0)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        res = await service.dispatch_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temperature=22.3,
            target_unit="°C",
        )
        assert res["dispatched"] is False
        assert res["dispatch_status"] == "blocked_precondition"
        assert "target_step_mismatch" in res["reason"]
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_unconfigured_grid_origin_blocks_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entity without configured server grid origin fails closed as step_alignment_unknown."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS", "{}")
    transport, state_box = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        res = await service.dispatch_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temperature=22.0,
            target_unit="°C",
        )
        assert res["dispatched"] is False
        assert res["dispatch_status"] == "blocked_precondition"
        assert "step_alignment_unknown" in res["reason"]
        assert len(state_box["mutation_calls"]) == 0


def test_client_attempt_to_supply_grid_origin_is_rejected() -> None:
    """Client input model rejects any attempt to supply grid_origin."""
    with pytest.raises(ValueError, match="forbidden"):
        HomeAssistantClimateActuationInput.model_validate({
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.0,
            "grid_origin": 22.0,
        })


def test_invalid_temperature_values_fail_closed() -> None:
    """NaN, infinity, and boolean temperature values fail closed with ValidationError."""
    for bad_temp in [float("nan"), float("inf"), float("-inf"), True, False]:
        with pytest.raises(ValidationError):
            validate_climate_actuation_preconditions(
                target_entity_id=TEST_CLIMATE_A,
                target_temperature=bad_temp,
            )


# =====================================================================
# 3. Authorization, Digest Integrity & Atomic Claim Tests
# =====================================================================

def test_capability_metadata() -> None:
    """Verify capability metadata conforms to PICO consequential action requirements."""
    cap = registry.get("homeassistant.allowlisted_climate_setpoint_actuate")
    assert cap.metadata.risk_tier == RiskTier.HIGH
    assert cap.metadata.consequential is True
    assert cap.metadata.required_permission == "homeassistant:actuate"
    assert cap.metadata.required_classification == "INTERNAL"
    assert cap.metadata.max_retries == 0
    assert cap.metadata.timeout_seconds == 15.0


def test_no_action_before_approval(db_session: Session) -> None:
    """Execution starts in awaiting_approval status and performs zero actions."""
    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
        input_data={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.5,
            "target_unit": "°C",
        },
        caller_permissions={"homeassistant:actuate"},
    )
    assert execution.status == "awaiting_approval"


@pytest.mark.anyio
async def test_untrusted_caller_cannot_execute_without_permission(db_session: Session) -> None:
    """Execution fails closed if caller lacks homeassistant:actuate permission."""
    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
        input_data={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.5,
            "target_unit": "°C",
        },
        caller_permissions={"homeassistant:actuate"},
    )
    engine.approve(
        execution.id,
        approver="operator@pico.local",
        approval_token=MOCK_APPROVAL_SECRET,
    )
    with pytest.raises(AuthorizationError, match="Unauthorized: Caller lacks required permission"):
        await engine.execute(execution.id, caller_permissions={"read_only"})


@pytest.mark.anyio
async def test_tampered_input_digest_invalidates_approval(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modifying execution input or digest after approval triggers triple-digest failure and blocks mutation."""
    transport, state_box = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantClimateActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_setpoint_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temperature": 22.5,
                "target_unit": "°C",
            },
            caller_permissions={"homeassistant:actuate"},
        )

        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        # Tamper with input digest in DB
        repo = ToolExecutionRepository(db_session)
        exec_record = repo.get_by_id(execution.id)
        assert exec_record is not None
        exec_record.input_digest = "tampered_climate_digest_99999"
        db_session.commit()

        with pytest.raises(AuthorizationError, match="Approval invalidated"):
            await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})

        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_replay_prevention_blocks_duplicate_dispatch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatching the same execution ID more than once raises AuthorizationError."""
    transport, state_box = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        # First dispatch succeeds
        res1 = await service.dispatch_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temperature=22.5,
            target_unit="°C",
            execution_id="exec_climate_001",
        )
        assert res1["dispatched"] is True
        assert len(state_box["mutation_calls"]) == 1

        # Second dispatch with identical execution_id raises AuthorizationError
        with pytest.raises(AuthorizationError, match="already been dispatched"):
            await service.dispatch_actuation(
                target_entity_id=TEST_CLIMATE_A,
                target_temperature=22.5,
                target_unit="°C",
                execution_id="exec_climate_001",
            )

        # Still only 1 mutation call occurred
        assert len(state_box["mutation_calls"]) == 1


# =====================================================================
# 4. Physical Dispatch Boundary & Verification Tests
# =====================================================================

@pytest.mark.anyio
async def test_successful_execution_dispatches_service_and_verifies(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end execution dispatches climate.set_temperature and verifies reported target temperature."""
    transport, state_box = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantClimateActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_setpoint_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temperature": 22.5,
                "target_unit": "°C",
            },
            caller_permissions={"homeassistant:actuate"},
        )

        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        completed_exec = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert completed_exec.status == "succeeded"
        assert completed_exec.verification_status == "passed"
        assert completed_exec.output_json["verification_status"] == "verified"
        assert completed_exec.output_json["reported_temperature"] == 22.5
        assert completed_exec.output_json["physical_device_connectivity_claimed"] is False

        # Verify mutation egress details
        assert len(state_box["mutation_calls"]) == 1
        mutation = state_box["mutation_calls"][0]
        assert mutation["url"] == f"{MOCK_BASE_URL}/api/services/climate/set_temperature"
        assert mutation["body"] == {
            "entity_id": TEST_CLIMATE_A,
            "temperature": 22.5,
        }


@pytest.mark.anyio
async def test_dispatch_timeout_never_retries(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch timeout reports timeout_uncertain and issues zero duplicate retries."""
    transport, state_box = make_mock_climate_transport(service_timeout=True)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantClimateActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_setpoint_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temperature": 22.5,
                "target_unit": "°C",
            },
            caller_permissions={"homeassistant:actuate"},
        )

        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        # Execute handles timeout_uncertain without retry
        completed_exec = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        # Verification fails because state never updated
        assert completed_exec.status == "verification_failed"
        assert completed_exec.verification_status == "failed"
        assert len(state_box["mutation_calls"]) == 1


@pytest.mark.anyio
async def test_verification_failure_when_state_does_not_update(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When service responds HTTP 200 but reported target temperature remains unchanged, verification fails."""
    transport, state_box = make_mock_climate_transport(mutate_on_service=False)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantClimateActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_setpoint_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temperature": 22.5,
                "target_unit": "°C",
            },
            caller_permissions={"homeassistant:actuate"},
        )

        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        completed_exec = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert completed_exec.status == "verification_failed"
        assert completed_exec.verification_status == "failed"
        assert completed_exec.verification_details_json["reported_temperature"] == 21.0


# =====================================================================
# 5. REST API & Integration Route Tests
# =====================================================================

def test_api_climate_propose_route_creates_execution() -> None:
    """POST /api/homeassistant/climate/propose creates execution in awaiting_approval."""
    client = TestClient(app)
    resp = client.post(
        "/api/homeassistant/climate/propose",
        headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
        json={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.5,
            "target_unit": "°C",
            "reason": "Operator requested daytime heating",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["capability_id"] == "homeassistant.allowlisted_climate_setpoint_actuate"
    assert data["status"] == "awaiting_approval"
    assert data["input_data"]["target_temperature"] == 22.5

    # Inspect status via GET /api/homeassistant/climate/execution/{id}
    exec_id = data["id"]
    status_resp = client.get(f"/api/homeassistant/climate/execution/{exec_id}")
    assert status_resp.status_code == 200
    assert status_resp.json()["id"] == exec_id


def test_api_climate_propose_rejects_missing_or_bad_token() -> None:
    """POST /api/homeassistant/climate/propose requires valid X-Pico-Approval-Token."""
    client = TestClient(app)
    # Missing token
    resp1 = client.post(
        "/api/homeassistant/climate/propose",
        json={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.5,
        },
    )
    assert resp1.status_code == 403

    # Wrong token
    resp2 = client.post(
        "/api/homeassistant/climate/propose",
        headers={"X-Pico-Approval-Token": "bad_token_123"},
        json={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.5,
        },
    )
    assert resp2.status_code == 403


def test_api_climate_propose_rejects_client_grid_origin() -> None:
    """POST /api/homeassistant/climate/propose rejects client-supplied grid_origin with 422."""
    client = TestClient(app)
    resp = client.post(
        "/api/homeassistant/climate/propose",
        headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
        json={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.5,
            "grid_origin": 16.0,
        },
    )
    assert resp.status_code == 422


def test_api_climate_propose_rejects_unknown_fields() -> None:
    """POST /api/homeassistant/climate/propose rejects unexpected fields with 422."""
    client = TestClient(app)
    resp = client.post(
        "/api/homeassistant/climate/propose",
        headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
        json={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.5,
            "unrecognized_field": "exploit",
        },
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_readiness_reports_actuation_enabled_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """evaluate_climate_readiness reports actuation_enabled=True only when both flags are enabled."""
    transport, _ = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)

        # Both enabled
        monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
        monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", True)
        report_enabled = await evaluate_climate_readiness(check_live=False, readonly_client=ro_client)
        assert report_enabled.actuation_enabled is True

        # Climate actuation disabled
        monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", False)
        report_disabled = await evaluate_climate_readiness(check_live=False, readonly_client=ro_client)
        assert report_disabled.actuation_enabled is False


# =====================================================================
# 6. Regressions: Light & Switch Actuation Boundaries Unaffected
# =====================================================================

def test_p73_light_actuation_boundary_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Light actuation preconditions still strictly accept light domain only."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", "light.test_lamp")
    with pytest.raises(ValidationError, match="does not match configured test light"):
        validate_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, operation="turn_on")


def test_stage_c_switch_actuation_boundary_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch actuation preconditions still strictly accept switch domain only."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", json.dumps(["switch.desk_lamp_plug"]))
    with pytest.raises(ValidationError, match="Only the 'switch' domain is permitted"):
        validate_switch_actuation_preconditions(target_entity_id=TEST_CLIMATE_A, operation="turn_on")
