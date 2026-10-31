import asyncio
import hashlib
import json
import math
import secrets
from typing import Any
import pytest
from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session, sessionmaker

from pico.capabilities.base import RiskTier
from pico.capabilities.homeassistant.ha_actuation import (
    HomeAssistantClimateActuationCapability,
    HomeAssistantClimateActuationInput,
    HomeAssistantClimateActuationOutput,
    HomeAssistantClimateDualSetpointActuationCapability,
    HomeAssistantClimateDualSetpointActuationInput,
    HomeAssistantClimateDualSetpointActuationOutput,
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
    validate_climate_dual_setpoint_actuation_preconditions,
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
    ClimateDualSetpointActuationPlan,
    ClimateDualSetpointActuationResult,
    ClimateReadinessReport,
    ReadinessCheckStatus,
    ReadinessErrorCategory,
)
from pico.api.homeassistant import ClimateDualSetpointProposeRequest
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
            new_low = body.get("target_temp_low")
            new_high = body.get("target_temp_high")
            if mutate_on_service and target_id in state_box["states"]:
                if new_temp is not None:
                    state_box["states"][target_id]["attributes"]["temperature"] = new_temp
                if new_low is not None:
                    state_box["states"][target_id]["attributes"]["target_temp_low"] = new_low
                if new_high is not None:
                    state_box["states"][target_id]["attributes"]["target_temp_high"] = new_high

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
    # Unauthenticated read fails closed with 403
    unauth_resp = client.get(f"/api/homeassistant/climate/execution/{exec_id}")
    assert unauth_resp.status_code == 403

    status_resp = client.get(
        f"/api/homeassistant/climate/execution/{exec_id}",
        headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
    )
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


# =====================================================================
# 5b. Finding C Readiness & Security Audit Tests
# =====================================================================

@pytest.mark.anyio
async def test_readiness_reports_actuation_enabled_and_truthful_summary_when_all_prerequisites_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_climate_readiness reports actuation_enabled=True and truthful summary when all 5 prerequisites are met."""
    transport, _ = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)

        report = await evaluate_climate_readiness(check_live=False, readonly_client=ro_client)
        assert report.actuation_enabled is True
        assert report.ready_for_observation is True
        assert "Climate setpoint actuation is enabled (approval still required)." in report.summary

        # Check items present and passing
        check_names = {c.name: c.status for c in report.checks}
        assert check_names["integration_configuration"] == ReadinessCheckStatus.PASS
        assert check_names["climate_awareness_configuration"] == ReadinessCheckStatus.PASS
        assert check_names["climate_allowlist"] == ReadinessCheckStatus.PASS
        assert check_names["target_climate_entity_membership"] == ReadinessCheckStatus.PASS
        assert check_names["climate_grid_origins"] == ReadinessCheckStatus.PASS
        assert check_names["actuation_boundary"] == ReadinessCheckStatus.PASS


@pytest.mark.anyio
@pytest.mark.parametrize(
    "missing_setting,value,expected_summary_part",
    [
        ("HOME_ASSISTANT_ENABLED", False, "NOT READY"),
        ("HOME_ASSISTANT_ACCESS_TOKEN", "", "NOT READY"),
        ("HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False, "NOT READY"),
        ("HOME_ASSISTANT_ACTUATION_ENABLED", False, "Climate awareness is ready (physical actuation disabled)."),
        ("HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", False, "Climate awareness is ready (physical actuation disabled)."),
    ],
)
async def test_readiness_actuation_disabled_for_each_missing_prerequisite(
    missing_setting: str,
    value: Any,
    expected_summary_part: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ANY of the 5 required prerequisites is missing, actuation_enabled MUST be False."""
    transport, _ = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)

        monkeypatch.setattr(settings, missing_setting, value)
        report = await evaluate_climate_readiness(check_live=False, readonly_client=ro_client)
        assert report.actuation_enabled is False
        assert expected_summary_part in report.summary


@pytest.mark.anyio
async def test_readiness_reports_invalid_climate_grid_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_climate_readiness fails closed when HOME_ASSISTANT_CLIMATE_GRID_ORIGINS is invalid."""
    transport, _ = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)

        # Negative origin
        bad_origins = {TEST_CLIMATE_A: {"origin": -5.0, "unit": "°C"}}
        monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS", json.dumps(bad_origins))
        report = await evaluate_climate_readiness(check_live=False, readonly_client=ro_client)

        assert report.ready_for_observation is False
        check_dict = {c.name: c for c in report.checks}
        grid_check = check_dict["climate_grid_origins"]
        assert grid_check.status == ReadinessCheckStatus.FAIL
        assert grid_check.error_category == ReadinessErrorCategory.INVALID_ENTITY
        assert "NOT READY" in report.summary
        assert "climate_grid_origins" in report.summary


@pytest.mark.anyio
async def test_readiness_reports_empty_climate_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_climate_readiness reports NOT_CONFIGURED when climate allowlist is empty."""
    transport, _ = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)

        monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ALLOWLIST", "[]")
        report = await evaluate_climate_readiness(check_live=False, readonly_client=ro_client)

        assert report.ready_for_observation is False
        check_dict = {c.name: c for c in report.checks}
        assert check_dict["climate_allowlist"].status == ReadinessCheckStatus.NOT_CONFIGURED


# =====================================================================
# 5c. Security Audit: Approval & Configuration Binding Tests
# =====================================================================

@pytest.mark.anyio
async def test_post_approval_grid_origin_change_invalidates_execution_with_zero_mutations(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modifying HOME_ASSISTANT_CLIMATE_GRID_ORIGINS after approval invalidates execution authorization with ZERO mutations."""
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

        # Bound origin in proposal snapshot was 16.0
        assert execution.input_json["expected_grid_origin"] == 16.0
        assert execution.input_json["expected_grid_origin_unit"] == "°C"

        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        # Alter server configuration after approval was granted
        modified_grid_origins = {
            TEST_CLIMATE_A: {"origin": 17.0, "unit": "°C"},
            TEST_CLIMATE_B: {"origin": 60.0, "unit": "°F"},
        }
        monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS", json.dumps(modified_grid_origins))

        # Execution attempt must fail closed before dispatch
        with pytest.raises(AuthorizationError, match="modified after approval was granted"):
            await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})

        # Zero mutation egress
        assert len(state_box["mutation_calls"]) == 0

        # Ledger event authorization_denied recorded
        ledger_repo = EventLedgerRepository(db_session)
        events = ledger_repo.list_events(execution_id=execution.id)
        event_types = [e.event_type for e in events]
        assert "authorization_denied" in event_types


@pytest.mark.anyio
async def test_post_approval_grid_origin_unit_change_invalidates_execution_with_zero_mutations(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modifying the configured grid origin unit after approval invalidates execution with ZERO mutations."""
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

        # Alter unit in server configuration after approval
        modified_grid_origins = {
            TEST_CLIMATE_A: {"origin": 16.0, "unit": "°F"},
            TEST_CLIMATE_B: {"origin": 60.0, "unit": "°F"},
        }
        monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS", json.dumps(modified_grid_origins))

        # Execution attempt must fail closed before dispatch
        with pytest.raises(AuthorizationError, match="modified after approval was granted"):
            await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})

        # Zero mutation egress
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_post_approval_removal_from_allowlist_invalidates_execution_with_zero_mutations(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing target entity from HOME_ASSISTANT_CLIMATE_ALLOWLIST after approval invalidates execution with ZERO mutations."""
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

        # Remove TEST_CLIMATE_A from allowlist
        monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ALLOWLIST", json.dumps([TEST_CLIMATE_B]))

        with pytest.raises(AuthorizationError, match="not allowlisted at dispatch"):
            await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})

        # Zero mutation egress
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_persistent_replay_prevention_across_restarts_and_instances(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic compare-and-set persistent claim prevents replay across process restarts and new engine instances."""
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

        # First execution succeeds
        completed_exec = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert completed_exec.status == "succeeded"
        assert len(state_box["mutation_calls"]) == 1

        # Confirm the execution has persisted consumed-ticket metadata and approval evidence
        exec_repo = ToolExecutionRepository(db_session)
        persisted_exec = exec_repo.get_by_id(execution.id)
        assert persisted_exec is not None
        assert persisted_exec.metadata_json is not None
        assert persisted_exec.metadata_json.get("dispatch_consumed") is True
        assert "dispatched_at" in persisted_exec.metadata_json
        assert persisted_exec.approved_by == "authenticated_token_holder"
        assert persisted_exec.approval_input_digest == execution.input_digest

        # Restore only the execution status to 'queued' to pass the preliminary status gate,
        # preserving the consumed-ticket metadata and all other relevant approval evidence.
        exec_repo.update_status(execution_id=execution.id, status="queued")
        queued_exec = exec_repo.get_by_id(execution.id)
        assert queued_exec is not None
        assert queued_exec.status == "queued"
        assert queued_exec.metadata_json.get("dispatch_consumed") is True

        # Simulate process restart: fresh engine and fresh capability/service instance
        fresh_service = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        fresh_cap = HomeAssistantClimateActuationCapability(service=fresh_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_setpoint_actuate": fresh_cap},
        )
        fresh_engine = ToolExecutionEngine(db_session, registry)

        # Attempt to replay the executed proposal on fresh engine instance.
        # Assert specifically that the persistent consumed-ticket guard raises AuthorizationError.
        with pytest.raises(
            AuthorizationError,
            match=f"Approval for execution '{execution.id}' has already been consumed and cannot be replayed",
        ):
            await fresh_engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})

        # Verify that an authorization_denied event is persisted with the replay-denial reason
        ledger_repo = EventLedgerRepository(db_session)
        events = ledger_repo.list_events(execution_id=execution.id)
        denied_events = [e for e in events if e.event_type == "authorization_denied"]
        assert len(denied_events) == 1
        assert denied_events[0].payload_json.get("reason") == "Replay attempt: approval ticket already consumed"

        # Assert that the replay attempt generates zero additional physical mutation calls (remains strictly 1)
        assert len(state_box["mutation_calls"]) == 1

        # Preserve and verify the original successful execution evidence and consumed-ticket state
        final_exec = exec_repo.get_by_id(execution.id)
        assert final_exec is not None
        assert final_exec.metadata_json.get("dispatch_consumed") is True
        assert final_exec.approval_input_digest == execution.input_digest
        assert final_exec.approved_by == "authenticated_token_holder"


@pytest.mark.anyio
async def test_concurrent_climate_execution_attempts_atomic_claim_boundary(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch two concurrent execution attempts for the same approved climate setpoint execution.

    Asserts that the database compare-and-set claim results in a single winner:
    exactly one succeeds with independent verification, the other fails closed with
    AuthorizationError from the atomic-claim conflict, an authorization_denied event is
    persisted with reason 'Execution claim collision or ticket invalidation', and exactly
    one physical mutation request is dispatched.
    """
    transport, state_box = make_mock_climate_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)

        # Disjoint service and capability instances to guarantee database CAS (not in-memory lock) governs
        service1 = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        service2 = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        assert service1._dispatched_executions is not service2._dispatched_executions

        cap1 = HomeAssistantClimateActuationCapability(service=service1)
        cap2 = HomeAssistantClimateActuationCapability(service=service2)

        reg1 = CapabilityRegistry()
        reg1.register(cap1)
        reg2 = CapabilityRegistry()
        reg2.register(cap2)

        engine1 = ToolExecutionEngine(db_session, reg1)
        engine2 = ToolExecutionEngine(db_session, reg2)
        perms = {"homeassistant:actuate"}

        # Target climate A initially reports temperature=21.0, requested target is 22.5 (differs)
        item = engine1.submit(
            capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temperature": 22.5,
                "target_unit": "°C",
            },
            caller_permissions=perms,
        )
        approved = engine1.approve(
            item.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )
        assert approved.status == "queued"
        assert len(state_box["mutation_calls"]) == 0

        # Concurrently attempt execution across engine1 and engine2 with claim boundary synchronization
        barrier = asyncio.Barrier(2)
        res1, res2 = await asyncio.gather(
            engine1.execute(item.id, caller_permissions=perms, claim_barrier=barrier),
            engine2.execute(item.id, caller_permissions=perms, claim_barrier=barrier),
            return_exceptions=True,
        )

        results = [res1, res2]
        succeeded = [r for r in results if isinstance(r, ToolExecution) and r.status == "succeeded"]
        failed = [r for r in results if isinstance(r, AuthorizationError)]

        # Exact single winner: exactly one succeeded with verified status
        assert len(succeeded) == 1, f"Expected exactly 1 succeeded, got {len(succeeded)}: {results}"
        assert succeeded[0].verification_status == "passed"
        assert succeeded[0].output_json.get("verification_status") == "verified"
        assert succeeded[0].output_json.get("dispatch_status") == "accepted"

        # Exactly one competing attempt fails closed with AuthorizationError from atomic claim collision
        assert len(failed) == 1, f"Expected exactly 1 AuthorizationError, got {len(failed)}: {results}"
        err_msg = str(failed[0]).lower()
        assert "could not be claimed" in err_msg or "concurrent dispatch conflict" in err_msg

        # Losing attempt records authorization_denied event with atomic claim collision reason
        ledger_repo = EventLedgerRepository(db_session)
        events = ledger_repo.list_events(execution_id=item.id)
        denied_events = [e for e in events if e.event_type == "authorization_denied"]
        assert len(denied_events) == 1
        assert denied_events[0].payload_json.get("reason") == (
            "Atomic claim failed: concurrent dispatch conflict, stale input, or already consumed ticket"
        )

        # Exactly ONE physical mutation request dispatched to Home Assistant!
        assert len(state_box["mutation_calls"]) == 1
        assert state_box["mutation_calls"][0]["body"] == {
            "entity_id": TEST_CLIMATE_A,
            "temperature": 22.5,
        }

        # Persistent execution record and approval metadata remain consistent
        exec_repo = ToolExecutionRepository(db_session)
        final_exec = exec_repo.get_by_id(item.id)
        assert final_exec is not None
        assert final_exec.status == "succeeded"
        assert final_exec.metadata_json.get("dispatch_consumed") is True
        assert final_exec.approval_input_digest == item.input_digest
        assert final_exec.approved_by == "authenticated_token_holder"


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


# =====================================================================
# 7. Milestone P7.4.2.4: Allowlisted Dual-Setpoint Climate Actuation
# =====================================================================

def make_dual_setpoint_state(
    state: str = "heat_cool",
    supported_features: int = 3,
    low: float = 18.0,
    high: float = 24.0,
) -> dict[str, Any]:
    return {
        "entity_id": TEST_CLIMATE_A,
        "state": state,
        "attributes": {
            "friendly_name": "Living Room Thermostat",
            "current_temperature": 20.0,
            "target_temp_low": low,
            "target_temp_high": high,
            "min_temp": 16.0,
            "max_temp": 28.0,
            "target_temp_step": 0.5,
            "supported_features": supported_features,
            "unit_of_measurement": "°C",
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
        },
    }


def test_climate_dual_setpoint_actuation_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED is False by default on pristine settings."""
    from pico.core.config import Settings
    pristine = Settings(_env_file=None)
    assert pristine.HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED is False


@pytest.mark.parametrize(
    "gate,expected_match",
    [
        ("HOME_ASSISTANT_ENABLED", "HOME_ASSISTANT_ENABLED=false"),
        ("HOME_ASSISTANT_ACCESS_TOKEN", "access token is missing"),
        ("HOME_ASSISTANT_ACTUATION_ENABLED", "HOME_ASSISTANT_ACTUATION_ENABLED=true"),
        ("HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=true"),
        ("HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED=true"),
        ("HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED=true"),
    ],
)
def test_dual_setpoint_missing_prerequisite_gates_fail_closed(
    monkeypatch: pytest.MonkeyPatch, gate: str, expected_match: str
) -> None:
    """Every parent gate and the dual-setpoint gate must fail closed if missing or disabled."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)
    if gate == "HOME_ASSISTANT_ACCESS_TOKEN":
        monkeypatch.setattr(settings, gate, "")
    else:
        monkeypatch.setattr(settings, gate, False)

    with pytest.raises(AuthorizationError, match=expected_match):
        validate_climate_dual_setpoint_actuation_preconditions(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=19.0,
            target_temp_high=23.0,
        )


def test_dual_setpoint_allowlist_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entity not present in HOME_ASSISTANT_CLIMATE_ALLOWLIST raises ValidationError."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)
    with pytest.raises(ValidationError, match="is not in the Stage D climate allowlist"):
        validate_climate_dual_setpoint_actuation_preconditions(
            target_entity_id="climate.unauthorized_thermostat",
            target_temp_low=19.0,
            target_temp_high=23.0,
        )


def test_dual_setpoint_schema_validation_and_rejected_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inputs with booleans, non-finites, inverted ranges, client grid origins, and extra fields fail closed."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    # 1. Booleans
    with pytest.raises(ValueError, match="cannot be a boolean"):
        HomeAssistantClimateDualSetpointActuationInput(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=True,  # type: ignore[arg-type]
            target_temp_high=23.0,
        )

    # 2. Non-finite strings (nan, inf, 1e999)
    for bad_val in ("nan", "inf", "-inf", "1e999"):
        with pytest.raises((ValueError, ValidationError)):
            HomeAssistantClimateDualSetpointActuationInput(
                target_entity_id=TEST_CLIMATE_A,
                target_temp_low=bad_val,  # type: ignore[arg-type]
                target_temp_high=23.0,
            )

    # 3. Inverted or equal range
    with pytest.raises(ValidationError, match="must be strictly less than upper target temperature"):
        HomeAssistantClimateDualSetpointActuationInput(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=23.0,
            target_temp_high=21.0,
        )
    with pytest.raises(ValidationError, match="must be strictly less than upper target temperature"):
        HomeAssistantClimateDualSetpointActuationInput(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=22.0,
            target_temp_high=22.0,
        )

    # 4. Client-supplied grid_origin forbidden
    with pytest.raises(ValueError, match="Client-supplied 'grid_origin' is forbidden"):
        HomeAssistantClimateDualSetpointActuationInput.model_validate({
            "target_entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
            "grid_origin": 16.0,
        })

    # 5. Extra fields forbidden
    with pytest.raises((PydanticValidationError, ValidationError)):
        HomeAssistantClimateDualSetpointActuationInput.model_validate({
            "target_entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
            "extra_field": "unsupported",
        })


@pytest.mark.anyio
async def test_dual_setpoint_wrong_mode_blocks_without_mode_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-dispatch check blocks dual-setpoint actuation if entity is in 'heat' or 'cool' mode without changing mode."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    heat_state = make_dual_setpoint_state(state="heat")
    transport, state_box = make_mock_climate_transport(initial_states={TEST_CLIMATE_A: heat_state})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_svc = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        res = await act_svc.dispatch_dual_setpoint_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=19.0,
            target_temp_high=23.0,
        )

        assert res["dispatched"] is False
        assert res["dispatch_status"] == "blocked_precondition"
        assert "mode_incompatible" in res["reason"]
        # Invariant: ZERO physical mutations dispatched
        assert len(state_box["mutation_calls"]) == 0
        # Invariant: Device state remains untouched (still 'heat')
        assert state_box["states"][TEST_CLIMATE_A]["state"] == "heat"


@pytest.mark.anyio
async def test_dual_setpoint_missing_target_temp_range_feature_flag_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-dispatch check blocks dual-setpoint actuation if entity lacks TARGET_TEMPERATURE_RANGE feature flag."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    single_only_state = make_dual_setpoint_state(state="heat_cool", supported_features=1)
    transport, state_box = make_mock_climate_transport(initial_states={TEST_CLIMATE_A: single_only_state})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_svc = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        res = await act_svc.dispatch_dual_setpoint_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=19.0,
            target_temp_high=23.0,
        )

        assert res["dispatched"] is False
        assert res["dispatch_status"] == "blocked_precondition"
        assert "does not support dual-setpoint temperature range" in res["reason"]
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_dual_setpoint_unavailable_or_unknown_entity_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-dispatch check blocks dual-setpoint actuation if entity is unavailable."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    unavail_state = make_dual_setpoint_state(state="unavailable")
    transport, state_box = make_mock_climate_transport(initial_states={TEST_CLIMATE_A: unavail_state})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_svc = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        res = await act_svc.dispatch_dual_setpoint_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=19.0,
            target_temp_high=23.0,
        )

        assert res["dispatched"] is False
        assert res["dispatch_status"] == "blocked_precondition"
        assert len(state_box["mutation_calls"]) == 0


def test_dual_setpoint_propose_creates_awaiting_approval_and_zero_mutations(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """POST /api/homeassistant/climate/dual-setpoint/propose creates execution in awaiting_approval with 0 mutations."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    # 1. Missing approval token header fails with 403
    resp_unauth = client.post(
        "/api/homeassistant/climate/dual-setpoint/propose",
        json={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
        },
    )
    assert resp_unauth.status_code == 403

    # 2. Valid proposal with approval token header succeeds with 201
    resp = client.post(
        "/api/homeassistant/climate/dual-setpoint/propose",
        headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
        json={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
            "reason": "Test dual setpoint proposal",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "awaiting_approval"
    assert data["capability_id"] == "homeassistant.allowlisted_climate_dual_setpoint_actuate"
    assert data["input_data"]["target_temp_low"] == 19.0
    assert data["input_data"]["target_temp_high"] == 23.0


@pytest.mark.anyio
async def test_dual_setpoint_execution_without_approval_rejected(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """Executing a dual-setpoint execution before human approval fails closed."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    engine = ToolExecutionEngine(db_session, registry)
    item = engine.submit(
        capability_id="homeassistant.allowlisted_climate_dual_setpoint_actuate",
        input_data={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
        },
        caller_permissions={"homeassistant:actuate"},
    )
    assert item.status == "awaiting_approval"

    # Attempting to execute unapproved item raises ValidationError (in status awaiting_approval)
    with pytest.raises(ValidationError, match="is in status 'awaiting_approval'"):
        await engine.execute(item.id, caller_permissions={"homeassistant:actuate"})


@pytest.mark.anyio
async def test_dual_setpoint_successful_approved_execution_dispatches_exact_payload(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """Approved dual-setpoint actuation dispatches exactly one call with target_temp_low and target_temp_high (no temperature or hvac_mode)."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    dual_state = make_dual_setpoint_state()
    transport, state_box = make_mock_climate_transport(initial_states={TEST_CLIMATE_A: dual_state})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_svc = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        test_cap = HomeAssistantClimateDualSetpointActuationCapability(service=act_svc)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_dual_setpoint_actuate": test_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        item = engine.submit(
            capability_id="homeassistant.allowlisted_climate_dual_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temp_low": 19.0,
                "target_temp_high": 23.0,
            },
            caller_permissions={"homeassistant:actuate"},
        )

        # Approve execution
        engine.approve(
            item.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        # Execute
        res = await engine.execute(item.id, caller_permissions={"homeassistant:actuate", "admin:consequential"})
        assert res.status == "succeeded"
        assert res.verification_status == "passed"
        assert res.output_json["success"] is True
        assert res.output_json["verification_status"] == "verified"
        assert res.output_json["dispatch_status"] == "accepted"

        # Dispatches EXACTLY one mutation
        assert len(state_box["mutation_calls"]) == 1
        call_body = state_box["mutation_calls"][0]["body"]
        assert call_body == {
            "entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
        }
        # Invariant: Strictly NO temperature or hvac_mode in the payload
        assert "temperature" not in call_body
        assert "hvac_mode" not in call_body


@pytest.mark.anyio
async def test_dual_setpoint_already_satisfied_skips_physical_dispatch(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """If entity is already at requested target range, dispatch_status='already_satisfied' and 0 mutations are dispatched."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    already_state = make_dual_setpoint_state(low=19.0, high=23.0)
    transport, state_box = make_mock_climate_transport(initial_states={TEST_CLIMATE_A: already_state})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_svc = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        test_cap = HomeAssistantClimateDualSetpointActuationCapability(service=act_svc)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_dual_setpoint_actuate": test_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        item = engine.submit(
            capability_id="homeassistant.allowlisted_climate_dual_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temp_low": 19.0,
                "target_temp_high": 23.0,
            },
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(
            item.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        res = await engine.execute(item.id, caller_permissions={"homeassistant:actuate", "admin:consequential"})
        assert res.status == "succeeded"
        assert res.output_json["dispatch_status"] == "already_satisfied"
        # Invariant: 0 mutations dispatched
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_dual_setpoint_read_after_write_verification_requires_both_targets(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """Verification fails if only one target temperature matches."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    dual_state = make_dual_setpoint_state(low=19.0, high=24.0)
    # mutate_on_service=False so device state remains low=19.0, high=24.0 (lower matches requested 19.0, upper 24.0 != 23.0)
    transport, state_box = make_mock_climate_transport(
        initial_states={TEST_CLIMATE_A: dual_state},
        mutate_on_service=False,
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_svc = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        test_cap = HomeAssistantClimateDualSetpointActuationCapability(service=act_svc)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_climate_dual_setpoint_actuate": test_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        item = engine.submit(
            capability_id="homeassistant.allowlisted_climate_dual_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temp_low": 19.0,
                "target_temp_high": 23.0,
            },
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(
            item.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        res = await engine.execute(item.id, caller_permissions={"homeassistant:actuate", "admin:consequential"})
        assert res.status == "verification_failed"
        assert res.verification_status == "failed"


@pytest.mark.anyio
async def test_dual_setpoint_timeout_and_error_zero_retries(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """Network timeout results in timeout_uncertain with zero automatic retries."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    dual_state = make_dual_setpoint_state()
    transport, state_box = make_mock_climate_transport(
        initial_states={TEST_CLIMATE_A: dual_state},
        service_timeout=True,
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_svc = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )

        res = await act_svc.dispatch_dual_setpoint_actuation(
            target_entity_id=TEST_CLIMATE_A,
            target_temp_low=19.0,
            target_temp_high=23.0,
        )
        assert res["dispatch_status"] == "timeout_uncertain"
        # Exactly one dispatch attempted, zero retries
        assert len(state_box["mutation_calls"]) == 1


def test_dual_setpoint_execution_status_route_isolation(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, db_session: Session
) -> None:
    """Executions cannot be inspected across differing capability routes."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    engine = ToolExecutionEngine(db_session, registry)

    # 1. Create single-setpoint execution
    single_item = engine.submit(
        capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
        input_data={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temperature": 22.0,
        },
        caller_permissions={"homeassistant:actuate"},
    )

    # 2. Create dual-setpoint execution
    dual_item = engine.submit(
        capability_id="homeassistant.allowlisted_climate_dual_setpoint_actuate",
        input_data={
            "target_entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
        },
        caller_permissions={"homeassistant:actuate"},
    )
    db_session.commit()

    tok = "valid_climate_token_123"
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", tok)
    headers = {"X-Pico-Approval-Token": tok}

    # Inspecting dual item via single-setpoint route -> 404
    r1 = client.get(f"/api/homeassistant/climate/execution/{dual_item.id}", headers=headers)
    assert r1.status_code == 404

    # Inspecting single item via dual-setpoint route -> 404
    r2 = client.get(f"/api/homeassistant/climate/dual-setpoint/execution/{single_item.id}", headers=headers)
    assert r2.status_code == 404

    # Inspecting through correct respective routes -> 200
    r3 = client.get(f"/api/homeassistant/climate/execution/{single_item.id}", headers=headers)
    assert r3.status_code == 200
    assert r3.json()["capability_id"] == "homeassistant.allowlisted_climate_setpoint_actuate"

    r4 = client.get(f"/api/homeassistant/climate/dual-setpoint/execution/{dual_item.id}", headers=headers)
    assert r4.status_code == 200
    assert r4.json()["capability_id"] == "homeassistant.allowlisted_climate_dual_setpoint_actuate"


@pytest.mark.anyio
async def test_dual_setpoint_concurrent_atomic_claims(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """Concurrent executions of the same approved dual-setpoint ticket result in exactly 1 dispatch and 1 atomic claim failure."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)

    dual_state = make_dual_setpoint_state()
    transport, state_box = make_mock_climate_transport(initial_states={TEST_CLIMATE_A: dual_state})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service1 = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        service2 = HomeAssistantClimateActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        assert service1._dispatched_executions is not service2._dispatched_executions

        cap1 = HomeAssistantClimateDualSetpointActuationCapability(service=service1)
        cap2 = HomeAssistantClimateDualSetpointActuationCapability(service=service2)

        reg1 = CapabilityRegistry()
        reg1.register(cap1)
        reg2 = CapabilityRegistry()
        reg2.register(cap2)

        engine1 = ToolExecutionEngine(db_session, reg1)
        engine2 = ToolExecutionEngine(db_session, reg2)
        perms = {"homeassistant:actuate"}

        item = engine1.submit(
            capability_id="homeassistant.allowlisted_climate_dual_setpoint_actuate",
            input_data={
                "target_entity_id": TEST_CLIMATE_A,
                "target_temp_low": 19.0,
                "target_temp_high": 23.0,
            },
            caller_permissions=perms,
        )
        approved = engine1.approve(
            item.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )
        assert approved.status == "queued"
        assert len(state_box["mutation_calls"]) == 0

        barrier = asyncio.Barrier(2)
        res1, res2 = await asyncio.gather(
            engine1.execute(item.id, caller_permissions=perms, claim_barrier=barrier),
            engine2.execute(item.id, caller_permissions=perms, claim_barrier=barrier),
            return_exceptions=True,
        )

        results = [res1, res2]
        succeeded = [r for r in results if isinstance(r, ToolExecution) and r.status == "succeeded"]
        failed = [r for r in results if isinstance(r, AuthorizationError)]

        assert len(succeeded) == 1, f"Expected exactly 1 succeeded, got {len(succeeded)}: {results}"
        assert succeeded[0].verification_status == "passed"
        assert succeeded[0].output_json.get("verification_status") == "verified"
        assert succeeded[0].output_json.get("dispatch_status") == "accepted"

        assert len(failed) == 1, f"Expected exactly 1 AuthorizationError, got {len(failed)}: {results}"
        err_msg = str(failed[0]).lower()
        assert "could not be claimed" in err_msg or "concurrent dispatch conflict" in err_msg

        # Exactly 1 physical mutation call
        assert len(state_box["mutation_calls"]) == 1
        assert state_box["mutation_calls"][0]["body"] == {
            "entity_id": TEST_CLIMATE_A,
            "target_temp_low": 19.0,
            "target_temp_high": 23.0,
        }
