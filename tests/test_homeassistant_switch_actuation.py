import asyncio
import hashlib
import json
import secrets
from typing import Any
import pytest
from fastapi.testclient import TestClient
import httpx
from sqlalchemy.orm import Session, sessionmaker

from pico.capabilities.base import RiskTier
from pico.capabilities.homeassistant.ha_actuation import (
    HomeAssistantSwitchActuationCapability,
    HomeAssistantSwitchActuationInput,
    HomeAssistantSwitchActuationOutput,
)
from pico.capabilities.homeassistant.ha_readiness import (
    HomeAssistantSwitchActuationReadinessCapability,
)
from pico.capabilities.registry import CapabilityRegistry, registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.homeassistant.actuation import (
    HomeAssistantActuationService,
    HomeAssistantSwitchActuationService,
    validate_actuation_preconditions,
    validate_switch_actuation_preconditions,
    validate_switch_allowlist,
)
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    HomeAssistantConnectionError,
    ReadinessErrorCategory,
    SwitchActuationPlan,
    SwitchActuationReadinessReport,
)
from pico.homeassistant.readiness import (
    HomeAssistantSwitchReadinessChecker,
    evaluate_switch_actuation_readiness,
)
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

TEST_SWITCH_A = "switch.desk_lamp_plug"
TEST_SWITCH_B = "switch.bench_test_plug"
DEFAULT_ALLOWLIST = [TEST_SWITCH_A, TEST_SWITCH_B]
MOCK_BASE_URL = "http://127.0.0.1:8123"
MOCK_TOKEN = "valid_test_token_p741"
MOCK_APPROVAL_SECRET = "valid_p741_secret_token"


@pytest.fixture(autouse=True)
def configure_ha_switch_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default fixture for P7.4.1 tests: HA enabled and Stage C switch allowlist configured."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", MOCK_BASE_URL)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", MOCK_TOKEN)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", json.dumps(DEFAULT_ALLOWLIST))
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_VERIFICATION_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_VERIFICATION_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_APPROVAL_SECRET)


def make_mock_switch_transport(
    initial_states: dict[str, str] | None = None,
    mutate_on_service: bool = True,
    service_status_code: int = 200,
    service_timeout: bool = False,
) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """Create a mock httpx transport tracking calls and state changes for switches."""
    current_states = dict(initial_states if initial_states is not None else {TEST_SWITCH_A: "off", TEST_SWITCH_B: "off"})
    state_box = {
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
                return httpx.Response(
                    200,
                    json={
                        "entity_id": entity_id,
                        "state": state_box["states"][entity_id],
                        "attributes": {"friendly_name": f"Mock {entity_id}"},
                    },
                )
            return httpx.Response(404, json={"message": "Entity not found"})

        # 2. Service execution endpoint: /api/services/switch/{service}
        if request.method == "POST" and url_path.startswith("/api/services/switch/"):
            service = url_path.split("/")[-1]
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            state_box["mutation_calls"].append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "service": service,
                "body": body,
            })

            if service_timeout:
                raise httpx.TimeoutException("Mock service call timed out")

            if service_status_code != 200:
                return httpx.Response(service_status_code, json={"message": "Error"})

            entity_id = body.get("entity_id")
            if mutate_on_service and entity_id in state_box["states"]:
                if service == "turn_on":
                    state_box["states"][entity_id] = "on"
                elif service == "turn_off":
                    state_box["states"][entity_id] = "off"

            return httpx.Response(200, json=[{"entity_id": entity_id, "state": state_box["states"].get(entity_id)}])

        # 3. Status endpoint: /api/
        if request.method == "GET" and url_path == "/api/":
            return httpx.Response(200, json={"message": "API running.", "version": "2026.3.0"})

        return httpx.Response(404, json={"message": f"Unexpected mock route: {url_path}"})

    return httpx.MockTransport(handler), state_box


# =====================================================================
# 1. Configuration & Target Validation Tests
# =====================================================================

def test_stage_c_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage C switch actuation must be disabled by default."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", False)
    with pytest.raises(AuthorizationError, match="HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED"):
        validate_switch_actuation_preconditions(target_entity_id=TEST_SWITCH_A, operation="turn_on")


def test_empty_or_missing_allowlist_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty or whitespace-only allowlist fails closed."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", "[]")
    with pytest.raises(ValidationError, match="empty"):
        validate_switch_allowlist()

    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", "")
    with pytest.raises(ValidationError):
        validate_switch_allowlist()


def test_malformed_allowlist_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed JSON or non-list allowlist fails closed."""
    for malformed in ["{not-json}", "123", '{"key": "value"}', '["valid", 123]']:
        monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", malformed)
        with pytest.raises(ValidationError):
            validate_switch_allowlist()


def test_non_switch_entities_in_allowlist_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only switch.* domain entities may be configured in Stage C allowlist."""
    for invalid_entity in ["light.lamp", "climate.ac", "cover.garage", "lock.front_door", "sensor.temp"]:
        monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", json.dumps([invalid_entity]))
        with pytest.raises(ValidationError, match="switch"):
            validate_switch_allowlist()


def test_invalid_entity_id_format_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid entity ID characters, wildcards, regex, or whitespace rejected."""
    for bad_id in [
        "switch.*",
        "switch.fan*",
        "switch.desk plug",
        "switch.desk/plug",
        "switch.",
        ".switch",
        "switch.desk!plug",
        "switch.desk$plug",
    ]:
        monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", json.dumps([bad_id]))
        with pytest.raises(ValidationError):
            validate_switch_allowlist()


def test_unlisted_switch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid switch domain entity that is not in the allowlist is rejected."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", json.dumps([TEST_SWITCH_A]))
    with pytest.raises(ValidationError, match="allowlist"):
        validate_switch_actuation_preconditions(target_entity_id="switch.unauthorized_plug", operation="turn_on")


def test_lookalike_and_whitespace_variations_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace variations or case differences cannot bypass exact match."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", json.dumps([TEST_SWITCH_A]))
    for variation in [
        f" {TEST_SWITCH_A}",
        f"{TEST_SWITCH_A} ",
        TEST_SWITCH_A.upper(),
        f"{TEST_SWITCH_A}\n",
    ]:
        with pytest.raises(ValidationError):
            validate_switch_actuation_preconditions(target_entity_id=variation, operation="turn_on")


def test_p73_light_actuation_retains_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7.3 single-light capability does not accept switch entities or Stage C settings."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", "light.test_lamp")
    with pytest.raises(ValidationError, match="does not match configured test light"):
        validate_actuation_preconditions(target_entity_id=TEST_SWITCH_A, operation="turn_on")


def test_no_generic_service_execution_capability_registered() -> None:
    """Verify that neither homeassistant.call_service nor generic execution capabilities exist."""
    assert not registry.has("homeassistant.call_service")
    assert not registry.has("homeassistant.execute_service")
    assert not registry.has("homeassistant.service_call")
    assert registry.has("homeassistant.allowlisted_switch_actuate")
    assert registry.has("homeassistant.test_light_actuate")


def test_ws_command_allowlist_remains_read_only() -> None:
    """Read-only WebSocket allowlist must not contain call_service or mutations."""
    assert "call_service" not in ALLOWED_READONLY_WS_COMMANDS
    assert "execute_service" not in ALLOWED_READONLY_WS_COMMANDS
    assert ALLOWED_READONLY_WS_COMMANDS == {"get_states", "get_config", "ping"}


# =====================================================================
# 2. Authorization & Integrity Tests
# =====================================================================

def test_capability_metadata() -> None:
    """Verify Stage C capability metadata security properties."""
    cap = registry.get("homeassistant.allowlisted_switch_actuate")
    assert cap is not None
    assert cap.metadata.risk_tier == RiskTier.HIGH
    assert cap.metadata.consequential is True
    assert cap.metadata.required_permission == "homeassistant:actuate"
    assert cap.metadata.required_classification == "INTERNAL"
    assert cap.metadata.max_retries == 0
    assert cap.metadata.timeout_seconds == 15.0


def test_no_action_before_approval(db_session: Session) -> None:
    """Execution cannot run while awaiting approval."""
    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="homeassistant.allowlisted_switch_actuate",
        input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
        caller_permissions={"homeassistant:actuate"},
    )
    assert execution.status == "awaiting_approval"


@pytest.mark.anyio
async def test_untrusted_caller_cannot_grant_own_permission(db_session: Session) -> None:
    """Caller without required homeassistant:actuate permission cannot execute."""
    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="homeassistant.allowlisted_switch_actuate",
        input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
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
async def test_tampering_with_target_or_state_invalidates_approval(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modifying execution input after approval causes digest mismatch and fails execution."""
    transport, state_box = make_mock_switch_transport()
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )

        # Approve original execution
        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        # Tamper with input digest in DB
        repo = ToolExecutionRepository(db_session)
        exec_record = repo.get_by_id(execution.id)
        assert exec_record is not None
        exec_record.input_digest = "tampered_digest_99999"
        db_session.commit()

        # Attempt to execute tampered execution fails closed
        with pytest.raises(AuthorizationError, match="Approval invalidated"):
            await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})

        # Ensure zero mutation calls occurred
        assert len(state_box["mutation_calls"]) == 0


def test_stage_c_cannot_be_activated_merely_by_p73(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling P7.3 light actuation while Stage C is disabled must reject switch execution."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", False)

    engine = ToolExecutionEngine(db_session, registry)
    with pytest.raises(ValidationError, match="HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED"):
        engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )


# =====================================================================
# 3. Dispatch & Verification Tests
# =====================================================================

@pytest.mark.anyio
async def test_successful_switch_turn_on(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid turn_on dispatches exactly one POST to /api/services/switch/turn_on and verifies state."""
    transport, state_box = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "off"})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        res = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert res.status == "succeeded"
        assert res.verification_status == "passed"
        assert res.output_json["success"] is True
        assert res.output_json["reported_state"] == "on"
        assert res.output_json["dispatch_status"] == "accepted"

        # Exactly 1 mutation call
        assert len(state_box["mutation_calls"]) == 1
        call = state_box["mutation_calls"][0]
        assert call["service"] == "turn_on"
        assert call["body"] == {"entity_id": TEST_SWITCH_A}

        # Verify no credential leakage in output_json
        assert MOCK_TOKEN not in json.dumps(res.output_json)
        assert MOCK_APPROVAL_SECRET not in json.dumps(res.output_json)


@pytest.mark.anyio
async def test_successful_switch_turn_off(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid turn_off dispatches exactly one POST to /api/services/switch/turn_off and verifies state."""
    transport, state_box = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "on"})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "off"},
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        res = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert res.status == "succeeded"
        assert res.verification_status == "passed"
        assert res.output_json["success"] is True
        assert res.output_json["reported_state"] == "off"
        assert res.output_json["dispatch_status"] == "accepted"

        assert len(state_box["mutation_calls"]) == 1
        call = state_box["mutation_calls"][0]
        assert call["service"] == "turn_off"
        assert call["body"] == {"entity_id": TEST_SWITCH_A}


@pytest.mark.anyio
async def test_already_satisfied_state_requires_no_mutation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If switch is already in the requested state, avoid physical mutation dispatch."""
    transport, state_box = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "on"})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        res = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert res.status == "succeeded"
        assert res.verification_status == "passed"
        assert res.output_json["dispatch_status"] == "already_satisfied"

        # Zero mutation calls were dispatched!
        assert len(state_box["mutation_calls"]) == 0


def test_toggle_rejected() -> None:
    """'toggle' is strictly prohibited and fails input validation."""
    with pytest.raises(Exception):
        HomeAssistantSwitchActuationInput(
            target_entity_id=TEST_SWITCH_A,
            target_state="toggle",  # type: ignore
        )


@pytest.mark.anyio
async def test_http_success_with_unchanged_state_fails_verification(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Home Assistant HTTP returns 200 but reported state does not change, verification fails."""
    # mutate_on_service=False leaves state as "off" despite 200 response
    transport, state_box = make_mock_switch_transport(
        initial_states={TEST_SWITCH_A: "off"},
        mutate_on_service=False,
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        res = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert res.status == "verification_failed"
        assert res.verification_status == "failed"
        # Exactly one mutation dispatch took place, no re-dispatch
        assert len(state_box["mutation_calls"]) == 1


@pytest.mark.anyio
async def test_dispatch_timeout_creates_no_second_mutation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout during dispatch causes execution failure with zero retries."""
    transport, state_box = make_mock_switch_transport(
        initial_states={TEST_SWITCH_A: "off"},
        service_timeout=True,
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(
            execution.id,
            approver="operator@pico.local",
            approval_token=MOCK_APPROVAL_SECRET,
        )

        res = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert res.status == "verification_failed"
        # Exactly 1 attempt made, never retried
        assert len(state_box["mutation_calls"]) == 1


# =====================================================================
# 4. API & Readiness Endpoint Tests
# =====================================================================

def test_api_switch_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/homeassistant/switch/plan generates a structured plan without mutation."""
    transport, state_box = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "off"})
    original_client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(
        HomeAssistantSwitchActuationService,
        "_get_http_client",
        lambda self: asyncio.sleep(0, result=original_client),
    )
    monkeypatch.setattr(
        HomeAssistantClient,
        "_get_http_client",
        lambda self: asyncio.sleep(0, result=original_client),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/homeassistant/switch/plan",
            json={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["entity_id"] == TEST_SWITCH_A
        assert data["requested_state"] == "on"
        assert data["current_state"] == "off"
        assert data["is_mutation_needed"] is True
        assert len(state_box["mutation_calls"]) == 0


def test_api_switch_propose_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/homeassistant/switch/propose creates execution and GET /api/homeassistant/switch/{id} retrieves it."""
    with TestClient(app) as client:
        # 1. Propose without header fails
        res_no_auth = client.post(
            "/api/homeassistant/switch/propose",
            json={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
        )
        assert res_no_auth.status_code == 403

        # 2. Propose with valid header succeeds
        res_auth = client.post(
            "/api/homeassistant/switch/propose",
            headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
            json={"target_entity_id": TEST_SWITCH_A, "target_state": "on", "reason": "Test proposal"},
        )
        assert res_auth.status_code == 201
        execution_data = res_auth.json()
        execution_id = execution_data["id"]
        assert execution_data["capability_id"] == "homeassistant.allowlisted_switch_actuate"
        assert execution_data["status"] == "awaiting_approval"

        # 3. Retrieve switch execution status
        # Unauthenticated read fails closed with 403 under Workstream C
        res_unauth = client.get(f"/api/homeassistant/switch/{execution_id}")
        assert res_unauth.status_code == 403

        # Authenticated read with valid token succeeds
        res_status = client.get(
            f"/api/homeassistant/switch/{execution_id}",
            headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
        )
        assert res_status.status_code == 200
        assert res_status.json()["id"] == execution_id

        # 4. Proposing for unlisted entity fails
        res_unlisted = client.post(
            "/api/homeassistant/switch/propose",
            headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
            json={"target_entity_id": "switch.unlisted_one", "target_state": "on"},
        )
        assert res_unlisted.status_code in (400, 422)


def test_api_switch_execution_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/homeassistant/switch/{id} returns 404 for non-switch executions."""
    with TestClient(app) as client:
        # Propose a light actuation
        res_light = client.post(
            "/api/homeassistant/actuation/propose",
            headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
            json={"target_state": "on"},
        )
        assert res_light.status_code == 201
        light_exec_id = res_light.json()["id"]

        # Requesting via switch endpoint must fail with 404
        res_isolated = client.get(
            f"/api/homeassistant/switch/{light_exec_id}",
            headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
        )
        assert res_isolated.status_code == 404


def test_api_switch_readiness_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/homeassistant/switch/readiness offline makes zero network calls."""
    transport, state_box = make_mock_switch_transport()
    original_client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(
        HomeAssistantClient,
        "_get_http_client",
        lambda self: asyncio.sleep(0, result=original_client),
    )

    with TestClient(app) as client:
        response = client.get("/api/homeassistant/switch/readiness?check_live=false")
        assert response.status_code == 200
        data = response.json()
        assert data["stage_c_enabled"] is True
        assert data["actuation_enabled"] is True
        assert set(data["allowlisted_entities"]) == set(DEFAULT_ALLOWLIST)
        assert data["live_check_performed"] is False
        assert len(state_box["read_calls"]) == 0
        assert len(state_box["mutation_calls"]) == 0


def test_api_switch_readiness_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/homeassistant/switch/readiness?check_live=true performs read-only checks."""
    transport, state_box = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "off"})
    original_client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(
        HomeAssistantClient,
        "_get_http_client",
        lambda self: asyncio.sleep(0, result=original_client),
    )

    with TestClient(app) as client:
        response = client.get(f"/api/homeassistant/switch/readiness?check_live=true&target_entity_id={TEST_SWITCH_A}")
        assert response.status_code == 200
        data = response.json()
        assert data["live_check_performed"] is True
        assert data["live_passed"] is True
        assert data["reported_state"] == "off"
        assert len(state_box["read_calls"]) >= 1
        assert len(state_box["mutation_calls"]) == 0


# =====================================================================
# 5. P7.4.1.2 Truthful State Preflight & Fail-Closed Dispatch Tests
# =====================================================================

@pytest.mark.anyio
async def test_failed_preflight_state_read_produces_explicit_unavailable_plan() -> None:
    """Preflight read failure produces state_readiness='state_unavailable' and is_mutation_needed=False."""
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "Internal error"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantSwitchActuationService(readonly_client=ro_client, http_client=http_client)
        plan = await service.create_plan(target_entity_id=TEST_SWITCH_A, target_state="on")

        assert plan.entity_id == TEST_SWITCH_A
        assert plan.requested_state == "on"
        assert plan.current_state == "unavailable"
        assert plan.state_readiness == "state_unavailable"
        assert plan.is_mutation_needed is False
        assert any("state_unavailable" in p for p in plan.preconditions)


@pytest.mark.anyio
async def test_unknown_state_never_classified_as_already_satisfied() -> None:
    """An 'unknown' state reported by Home Assistant is classified as state_unknown, never already_satisfied."""
    transport, _ = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "unknown"})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantSwitchActuationService(readonly_client=ro_client, http_client=http_client)
        plan = await service.create_plan(target_entity_id=TEST_SWITCH_A, target_state="on")

        assert plan.current_state == "unknown"
        assert plan.state_readiness == "state_unknown"
        assert plan.is_mutation_needed is False
        assert plan.state_readiness != "already_satisfied"


@pytest.mark.anyio
async def test_failed_pre_dispatch_read_produces_zero_physical_mutations() -> None:
    """Pre-dispatch state read failure blocks execution and makes zero mutation requests."""
    def failing_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/api/states/"):
            return httpx.Response(503, json={"message": "Service unavailable"})
        if request.method == "POST":
            pytest.fail("Mutation endpoint must NOT be called when pre-dispatch read fails!")
        return httpx.Response(404)

    transport = httpx.MockTransport(failing_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantSwitchActuationService(readonly_client=ro_client, http_client=http_client)
        res = await service.dispatch_actuation(target_entity_id=TEST_SWITCH_A, target_state="on")

        assert res["dispatched"] is False
        assert res["dispatch_status"] == "blocked_precondition"
        assert "Precondition could not be verified" in res["reason"]


@pytest.mark.anyio
async def test_missing_entity_or_invalid_reported_state_fails_closed_zero_mutations(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing entity (404) or invalid reported state fails closed with zero physical mutations."""
    transport, state_box = make_mock_switch_transport(initial_states={})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(readonly_client=ro_client, http_client=http_client)
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(execution.id, approver="operator@pico.local", approval_token=MOCK_APPROVAL_SECRET)

        res = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert res.status == "failed"
        assert "blocked" in (res.error_message or "").lower()
        assert len(state_box["mutation_calls"]) == 0
        assert res.output_json is not None
        assert res.output_json.get("dispatch_status") == "blocked_precondition"


@pytest.mark.anyio
async def test_unknown_reported_state_fails_closed_at_execution_boundary(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An 'unknown' state reported at pre-dispatch fails closed before physical mutation."""
    transport, state_box = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "unknown"})
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantSwitchActuationService(readonly_client=ro_client, http_client=http_client)
        custom_cap = HomeAssistantSwitchActuationCapability(service=act_service)
        monkeypatch.setattr(
            registry,
            "_capabilities",
            {**registry._capabilities, "homeassistant.allowlisted_switch_actuate": custom_cap},
        )

        engine = ToolExecutionEngine(db_session, registry)
        execution = engine.submit(
            capability_id="homeassistant.allowlisted_switch_actuate",
            input_data={"target_entity_id": TEST_SWITCH_A, "target_state": "on"},
            caller_permissions={"homeassistant:actuate"},
        )
        engine.approve(execution.id, approver="operator@pico.local", approval_token=MOCK_APPROVAL_SECRET)

        res = await engine.execute(execution.id, caller_permissions={"homeassistant:actuate"})
        assert res.status == "failed"
        assert len(state_box["mutation_calls"]) == 0
        assert res.output_json.get("dispatch_status") == "blocked_precondition"


def test_api_switch_plan_truthful_state_distinction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route-level POST /api/homeassistant/switch/plan returns accurate state_readiness."""
    # 1. State 'off', requested 'on' -> ready, is_mutation_needed=True
    transport_off, _ = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "off"})
    c_off = httpx.AsyncClient(transport=transport_off)
    monkeypatch.setattr(HomeAssistantClient, "_get_http_client", lambda self: asyncio.sleep(0, result=c_off))

    with TestClient(app) as client:
        r1 = client.post("/api/homeassistant/switch/plan", json={"target_entity_id": TEST_SWITCH_A, "target_state": "on"})
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["state_readiness"] == "ready"
        assert d1["is_mutation_needed"] is True

        # 2. State 'on', requested 'on' -> already_satisfied, is_mutation_needed=False
        transport_on, _ = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "on"})
        c_on = httpx.AsyncClient(transport=transport_on)
        monkeypatch.setattr(HomeAssistantClient, "_get_http_client", lambda self: asyncio.sleep(0, result=c_on))

        r2 = client.post("/api/homeassistant/switch/plan", json={"target_entity_id": TEST_SWITCH_A, "target_state": "on"})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["state_readiness"] == "already_satisfied"
        assert d2["is_mutation_needed"] is False

        # 3. State 'unknown', requested 'on' -> state_unknown, is_mutation_needed=False
        transport_unk, _ = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "unknown"})
        c_unk = httpx.AsyncClient(transport=transport_unk)
        monkeypatch.setattr(HomeAssistantClient, "_get_http_client", lambda self: asyncio.sleep(0, result=c_unk))

        r3 = client.post("/api/homeassistant/switch/plan", json={"target_entity_id": TEST_SWITCH_A, "target_state": "on"})
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3["state_readiness"] == "state_unknown"
        assert d3["is_mutation_needed"] is False


def test_api_switch_readiness_with_unknown_and_unavailable_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/homeassistant/switch/readiness marks unknown/unavailable states as FAIL during live checks."""
    # 1. State 'unknown'
    transport_unk, _ = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "unknown"})
    c_unk = httpx.AsyncClient(transport=transport_unk)
    monkeypatch.setattr(HomeAssistantClient, "_get_http_client", lambda self: asyncio.sleep(0, result=c_unk))

    with TestClient(app) as client:
        r_unk = client.get(f"/api/homeassistant/switch/readiness?check_live=true&target_entity_id={TEST_SWITCH_A}")
        assert r_unk.status_code == 200
        d_unk = r_unk.json()
        assert d_unk["live_check_performed"] is True
        assert d_unk["live_passed"] is False
        assert d_unk["state_readiness"] == "state_unknown"
        assert d_unk["ready_for_commissioning"] is False
        check_unk = next(c for c in d_unk["checks"] if c["name"] == "live_switch_readability")
        assert check_unk["status"] == "FAIL"
        assert check_unk["error_category"] == "STATE_UNKNOWN"

        # 2. State 'unavailable'
        transport_unavail, _ = make_mock_switch_transport(initial_states={TEST_SWITCH_A: "unavailable"})
        c_unavail = httpx.AsyncClient(transport=transport_unavail)
        monkeypatch.setattr(HomeAssistantClient, "_get_http_client", lambda self: asyncio.sleep(0, result=c_unavail))

        r_unavail = client.get(f"/api/homeassistant/switch/readiness?check_live=true&target_entity_id={TEST_SWITCH_A}")
        assert r_unavail.status_code == 200
        d_unavail = r_unavail.json()
        assert d_unavail["live_check_performed"] is True
        assert d_unavail["live_passed"] is False
        assert d_unavail["state_readiness"] == "state_unavailable"
        assert d_unavail["ready_for_commissioning"] is False
        check_unavail = next(c for c in d_unavail["checks"] if c["name"] == "live_switch_readability")
        assert check_unavail["status"] == "FAIL"
        assert check_unavail["error_category"] == "STATE_UNAVAILABLE"
