import asyncio
import hashlib
import json
from typing import Any
import pytest
from fastapi.testclient import TestClient
import httpx
from sqlalchemy.orm import Session

from pico.capabilities.base import RiskTier
from pico.capabilities.homeassistant.ha_actuation import (
    HomeAssistantTestLightActuationCapability,
    HomeAssistantTestLightActuationInput,
    HomeAssistantTestLightActuationOutput,
)
from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.homeassistant.actuation import (
    HomeAssistantActuationService,
    validate_actuation_preconditions,
)
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    HomeAssistantConnectionError,
    TestLightActuationPlan,
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

TEST_LIGHT_ID = "light.p7_test_lamp"
MOCK_BASE_URL = "http://127.0.0.1:8123"
MOCK_TOKEN = "valid_test_token_p73"


@pytest.fixture(autouse=True)
def configure_ha_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default fixture for P7.3 tests: HA enabled and test light configured."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", MOCK_BASE_URL)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", MOCK_TOKEN)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", TEST_LIGHT_ID)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_VERIFICATION_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_VERIFICATION_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "valid_p73_secret_token")


def make_mock_ha_transport(
    initial_state: str = "off",
    mutate_on_service: bool = True,
    service_status_code: int = 200,
    service_timeout: bool = False,
) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """Create a mock httpx transport tracking calls and state changes."""
    state_box = {
        "state": initial_state,
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
            if entity_id == TEST_LIGHT_ID:
                return httpx.Response(
                    200,
                    json={
                        "entity_id": TEST_LIGHT_ID,
                        "state": state_box["state"],
                        "attributes": {"friendly_name": "P7 Test Light"},
                    },
                )
            return httpx.Response(404, json={"message": "Entity not found"})

        # 2. Service execution endpoint: /api/services/light/{service}
        if request.method == "POST" and url_path.startswith("/api/services/light/"):
            service = url_path.split("/")[-1]
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            state_box["mutation_calls"].append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body,
                "service": service,
            })

            if service_timeout:
                raise httpx.TimeoutException("Mock transport dispatch timeout")

            if service_status_code != 200:
                return httpx.Response(service_status_code, json={"message": "Service error"})

            if mutate_on_service:
                if service == "turn_on":
                    state_box["state"] = "on"
                elif service == "turn_off":
                    state_box["state"] = "off"

            return httpx.Response(200, json=[{"entity_id": TEST_LIGHT_ID, "state": state_box["state"]}])

        # 3. Status endpoint: /api/
        if request.method == "GET" and url_path == "/api/":
            return httpx.Response(200, json={"message": "API running.", "version": "2026.3.0"})

        return httpx.Response(404, json={"message": "Not found"})

    transport = httpx.MockTransport(handler)
    return transport, state_box


# =====================================================================
# 1. Configuration, Scope & Boundary Tests
# =====================================================================

def test_actuation_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Actuation must fail closed when HOME_ASSISTANT_ACTUATION_ENABLED is false."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", False)
    with pytest.raises(AuthorizationError) as exc:
        validate_actuation_preconditions(target_entity_id=TEST_LIGHT_ID, operation="turn_on")
    assert "actuation is disabled by default" in str(exc.value).lower()


def test_missing_test_light_configuration_refuses_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse actuation if HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID is not configured."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", None)
    with pytest.raises(ValidationError) as exc:
        validate_actuation_preconditions(target_entity_id=None, operation="turn_on")
    assert "not configured" in str(exc.value).lower()


def test_non_light_domain_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject configured entity if it is not in the 'light' domain."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", "switch.heater")
    with pytest.raises(ValidationError) as exc:
        validate_actuation_preconditions(operation="turn_on")
    assert "only the 'light' domain is permitted" in str(exc.value).lower()


def test_arbitrary_or_mismatched_target_rejected() -> None:
    """Reject caller-supplied target entity if it does not equal configured test light."""
    with pytest.raises(ValidationError) as exc:
        validate_actuation_preconditions(target_entity_id="light.living_room_chandelier", operation="turn_on")
    assert "does not match configured test light" in str(exc.value).lower()


def test_toggle_operation_strictly_prohibited() -> None:
    """Reject 'toggle' operation due to concurrency ambiguity."""
    with pytest.raises(ValidationError) as exc:
        validate_actuation_preconditions(target_entity_id=TEST_LIGHT_ID, operation="toggle")
    assert "toggle" in str(exc.value).lower()
    assert "prohibited" in str(exc.value).lower()


def test_unsupported_service_rejected() -> None:
    """Reject unsupported arbitrary services (e.g. set_brightness, lock, flash)."""
    with pytest.raises(ValidationError) as exc:
        validate_actuation_preconditions(target_entity_id=TEST_LIGHT_ID, operation="set_level")
    assert "unsupported operation" in str(exc.value).lower()


def test_stage_a_readonly_ws_allowlist_intact() -> None:
    """Stage A read-only WebSocket command allowlist must NOT be expanded for actuation."""
    assert ALLOWED_READONLY_WS_COMMANDS == {"get_states", "get_config", "ping"}
    assert "call_service" not in ALLOWED_READONLY_WS_COMMANDS
    assert "turn_on" not in ALLOWED_READONLY_WS_COMMANDS


def test_no_generic_service_execution_capability_registered() -> None:
    """PICO must NOT register a generic Home Assistant service execution capability."""
    assert not registry.has("homeassistant.call_service")
    assert not registry.has("homeassistant.execute_service")
    assert not registry.has("homeassistant.actuate")
    # Only our narrow, single test light capability exists
    assert registry.has("homeassistant.test_light_actuate")


# =====================================================================
# 2. Planning State Machine Tests (Read-Only Guarantee)
# =====================================================================

@pytest.mark.anyio
async def test_plan_creation_is_strictly_read_only() -> None:
    """Planning creates a structured plan using read-only checks; zero mutation calls."""
    transport, state_box = make_mock_ha_transport(initial_state="off")
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantActuationService(readonly_client=ro_client, http_client=http_client)

        plan = await service.create_plan(target_state="on")
        assert plan.entity_id == TEST_LIGHT_ID
        assert plan.requested_state == "on"
        assert plan.current_state == "off"
        assert plan.service == "turn_on"
        assert plan.is_mutation_needed is True
        assert len(plan.input_digest) == 64
        assert plan.data_classification == "INTERNAL"

        # Verification: Read was performed, zero mutation calls occurred
        assert len(state_box["read_calls"]) == 1
        assert len(state_box["mutation_calls"]) == 0


@pytest.mark.anyio
async def test_plan_creation_detects_already_in_state() -> None:
    """When light is already in requested state, plan notes no mutation is needed."""
    transport, state_box = make_mock_ha_transport(initial_state="on")
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        service = HomeAssistantActuationService(readonly_client=ro_client, http_client=http_client)

        plan = await service.create_plan(target_state="on")
        assert plan.current_state == "on"
        assert plan.is_mutation_needed is False
        assert len(state_box["mutation_calls"]) == 0


# =====================================================================
# 3. Approval, Digest Binding & Tamper Resistance Tests
# =====================================================================

@pytest.mark.anyio
async def test_consequential_actuation_requires_human_approval(db_session: Session) -> None:
    """Execution must fail closed if executed before human approval is granted."""
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"homeassistant:actuate"}

    item = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={"target_state": "on"},
        caller_permissions=perms,
    )
    assert item.status == "awaiting_approval"
    assert item.approved_by is None
    assert item.approval_input_digest is None

    # Direct execution while awaiting approval must fail closed
    with pytest.raises(Exception):
        await engine.execute(item.id, caller_permissions=perms)


@pytest.mark.anyio
async def test_caller_cannot_spoof_approval_or_override_target(db_session: Session) -> None:
    """Caller passing approved=True or a malicious target cannot bypass security."""
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"homeassistant:actuate"}

    # 1. Attemping to pass another target entity in input fails validation
    with pytest.raises(ValidationError):
        engine.submit(
            capability_id="homeassistant.test_light_actuate",
            input_data={"target_state": "on", "target_entity_id": "lock.front_door"},
            caller_permissions=perms,
        )

    # 2. Submitting with approved=True in payload remains in awaiting_approval
    item = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={"target_state": "on"},
        metadata={"approved": True, "spoofed": "yes"},
        caller_permissions=perms,
    )
    assert item.status == "awaiting_approval"
    assert item.approved_by is None


@pytest.mark.anyio
async def test_tampered_input_digest_invalidates_approval(db_session: Session) -> None:
    """If the input payload or target changes after approval, execution is rejected."""
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"homeassistant:actuate"}

    item = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={"target_state": "off"},
        caller_permissions=perms,
    )
    # Valid approval recorded for 'off'
    approved = engine.approve(item.id, approver="lead_engineer", approval_token="valid_p73_secret_token")
    assert approved.status == "queued"

    # Simulate tampering in DB before execution
    approved.input_digest = "tampered_digest_99999"
    db_session.commit()

    with pytest.raises(AuthorizationError) as exc:
        await engine.execute(item.id, caller_permissions=perms)
    assert "Approval invalidated" in str(exc.value)


# =====================================================================
# 4. End-to-End Execution, Actuation & Verification Tests
# =====================================================================

@pytest.mark.anyio
async def test_end_to_end_successful_actuation(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full workflow: Submit -> Awaiting Approval -> Approve -> Execute -> Verify -> Audit."""
    transport, state_box = make_mock_ha_transport(initial_state="off", mutate_on_service=True)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantTestLightActuationCapability(service=act_service)
        monkeypatch.setattr(registry, "_capabilities", {**registry._capabilities, "homeassistant.test_light_actuate": custom_cap})

        engine = ToolExecutionEngine(db_session, registry)
        perms = {"homeassistant:actuate"}

        # 1. Submit
        item = engine.submit(
            capability_id="homeassistant.test_light_actuate",
            input_data={"target_state": "on", "reason": "Operator evening lighting test"},
            caller_permissions=perms,
        )
        assert item.status == "awaiting_approval"

        # 2. Approve
        approved = engine.approve(
            item.id,
            approver="pico_owner",
            notes="Approved evening test.",
            approval_token="valid_p73_secret_token",
        )
        assert approved.status == "queued"

        # 3. Execute
        res = await engine.execute(item.id, caller_permissions=perms)
        assert res.status == "succeeded"
        assert res.verification_status == "passed"
        assert res.output_json["success"] is True
        assert res.output_json["reported_state"] == "on"
        assert res.output_json["dispatch_status"] == "accepted"
        assert res.output_json["physical_device_connectivity_claimed"] is False

        # 4. Verify single mutation egress call
        assert len(state_box["mutation_calls"]) == 1
        mutation = state_box["mutation_calls"][0]
        assert mutation["service"] == "turn_on"
        assert mutation["body"] == {"entity_id": TEST_LIGHT_ID}
        # Zero credentials logged in body
        assert "access_token" not in json.dumps(mutation["body"])

        # 5. Verify audit event ledger
        ledger_repo = EventLedgerRepository(db_session)
        events = ledger_repo.list_events(execution_id=item.id)
        event_types = [e.event_type for e in events]
        assert "task_submitted" in event_types
        assert "approval_required" in event_types
        assert "approval_recorded" in event_types
        assert "execution_started" in event_types
        assert "verification_started" in event_types
        assert "verification_passed" in event_types
        assert "execution_succeeded" in event_types

        # Ensure no bearer token leaked in ledger
        for ev in events:
            ev_str = json.dumps(ev.payload_json or {})
            assert MOCK_TOKEN not in ev_str


@pytest.mark.anyio
async def test_acknowledged_dispatch_with_unchanged_state_fails_verification(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If service returns 200 OK but reported state remains unchanged, verification must fail."""
    # Transport returns 200 on service call, but does NOT change state (state stays 'off')
    transport, state_box = make_mock_ha_transport(initial_state="off", mutate_on_service=False)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantTestLightActuationCapability(service=act_service)
        monkeypatch.setattr(registry, "_capabilities", {**registry._capabilities, "homeassistant.test_light_actuate": custom_cap})

        engine = ToolExecutionEngine(db_session, registry)
        perms = {"homeassistant:actuate"}

        item = engine.submit(
            capability_id="homeassistant.test_light_actuate",
            input_data={"target_state": "on"},
            caller_permissions=perms,
        )
        engine.approve(item.id, approver="alice_engineer", approval_token="valid_p73_secret_token")

        res = await engine.execute(item.id, caller_permissions=perms)
        assert res.status == "verification_failed"
        assert res.verification_status == "failed"
        assert "State verification failed" in (res.error_message or "")
        # Only 1 mutation was dispatched: verification NEVER issues a second mutation call
        assert len(state_box["mutation_calls"]) == 1


@pytest.mark.anyio
async def test_dispatch_timeout_does_not_blindly_retry(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch timeout must not trigger automatic repeat mutation calls."""
    transport, state_box = make_mock_ha_transport(service_timeout=True)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantTestLightActuationCapability(service=act_service)
        monkeypatch.setattr(registry, "_capabilities", {**registry._capabilities, "homeassistant.test_light_actuate": custom_cap})

        engine = ToolExecutionEngine(db_session, registry)
        perms = {"homeassistant:actuate"}

        item = engine.submit(
            capability_id="homeassistant.test_light_actuate",
            input_data={"target_state": "on"},
            caller_permissions=perms,
        )
        engine.approve(item.id, approver="lead_engineer", approval_token="valid_p73_secret_token")

        res = await engine.execute(item.id, caller_permissions=perms)
        # Timeout occurred on dispatch; verification observed state remained 'off'
        assert res.status == "verification_failed"
        # Zero repeat mutations
        assert len(state_box["mutation_calls"]) == 1


# =====================================================================
# 5. FastAPI HTTP Endpoints Tests
# =====================================================================

def test_api_plan_endpoint_performs_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/homeassistant/actuation/plan returns plan without mutation."""
    transport, state_box = make_mock_ha_transport(initial_state="off")

    # Patch client to use mock transport
    original_client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(
        HomeAssistantActuationService,
        "_get_http_client",
        lambda self: asyncio.sleep(0, result=original_client),
    )
    monkeypatch.setattr(
        HomeAssistantClient,
        "_get_http_client",
        lambda self: asyncio.sleep(0, result=original_client),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/homeassistant/actuation/plan",
            json={"target_state": "on"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_id"] == TEST_LIGHT_ID
        assert data["requested_state"] == "on"
        assert data["service"] == "turn_on"
        assert data["is_mutation_needed"] is True
        assert len(state_box["mutation_calls"]) == 0


def test_api_propose_and_inspect_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/homeassistant/actuation/propose creates execution in awaiting_approval with valid token."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "valid_operator_secret_123")
    with TestClient(app) as client:
        # 1. Propose actuation with valid token
        resp = client.post(
            "/api/homeassistant/actuation/propose",
            json={"target_state": "on", "reason": "Testing lamp via API"},
            headers={"X-Pico-Approval-Token": "valid_operator_secret_123"},
        )
        assert resp.status_code == 201
        data = resp.json()
        exec_id = data["id"]
        assert data["status"] == "awaiting_approval"
        assert data["capability_id"] == "homeassistant.test_light_actuate"

        # 2. Inspect status via dedicated actuation endpoint
        status_resp = client.get(f"/api/homeassistant/actuation/{exec_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "awaiting_approval"


def test_api_arbitrary_call_service_route_does_not_exist() -> None:
    """Arbitrary call_service or execute endpoints must return 404."""
    with TestClient(app) as client:
        resp = client.post("/api/homeassistant/call_service", json={"service": "light.turn_on"})
        assert resp.status_code == 404
        resp2 = client.post("/api/homeassistant/services/light/turn_on", json={})
        assert resp2.status_code == 404


# =====================================================================
# 6. Additional Adversarial Security & Authorization Gate Tests
# =====================================================================

def test_approval_secret_token_unset_fails_closed(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Physical actuation approval must fail closed when APPROVAL_SECRET_TOKEN is unset."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", None)
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"homeassistant:actuate"}

    item = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={"target_state": "on"},
        caller_permissions=perms,
    )
    with pytest.raises(AuthorizationError) as exc:
        engine.approve(item.id, approver="lead_engineer", approval_token="some_token")
    assert "fails closed without valid server-side approval_secret_token" in str(exc.value).lower()


def test_approval_missing_or_invalid_token_fails_closed(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approval fails closed when approval_token does not match APPROVAL_SECRET_TOKEN."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "real_secret_token_456")
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"homeassistant:actuate"}

    item = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={"target_state": "on"},
        caller_permissions=perms,
    )

    # Missing token
    with pytest.raises(AuthorizationError) as exc1:
        engine.approve(item.id, approver="lead_engineer", approval_token=None)
    assert "fails closed" in str(exc1.value).lower()

    # Wrong token
    with pytest.raises(AuthorizationError) as exc2:
        engine.approve(item.id, approver="lead_engineer", approval_token="wrong_token_xyz")
    assert "fails closed" in str(exc2.value).lower()


def test_approval_forged_approver_identity_fails_closed(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller passing untrusted approver string is strictly rejected."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "real_secret_token_456")
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"homeassistant:actuate"}

    item = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={"target_state": "on"},
        caller_permissions=perms,
    )
    with pytest.raises(AuthorizationError) as exc:
        engine.approve(item.id, approver="untrusted_intruder", approval_token="real_secret_token_456")
    assert "not authorized" in str(exc.value).lower()


def test_generic_execution_route_lacks_actuate_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting homeassistant.test_light_actuate via generic /api/tools/executions without token fails closed."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "secret_token_789")
    with TestClient(app) as client:
        # Without X-Pico-Approval-Token: fails 403 because homeassistant:actuate is NOT in default_perms
        resp = client.post(
            "/api/tools/executions",
            json={
                "capability_id": "homeassistant.test_light_actuate",
                "input_data": {"target_state": "on"},
            },
        )
        assert resp.status_code == 403
        assert "homeassistant:actuate" in resp.json()["detail"]


@pytest.mark.anyio
async def test_replayed_execution_attempt_fails_closed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approved execution cannot be replayed or dispatched twice."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "secret_replay_token")
    transport, state_box = make_mock_ha_transport(initial_state="off", mutate_on_service=True)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantActuationService(
            readonly_client=ro_client,
            http_client=http_client,
        )
        custom_cap = HomeAssistantTestLightActuationCapability(service=act_service)
        monkeypatch.setattr(registry, "_capabilities", {**registry._capabilities, "homeassistant.test_light_actuate": custom_cap})

        engine = ToolExecutionEngine(db_session, registry)
        perms = {"homeassistant:actuate"}

        item = engine.submit(
            capability_id="homeassistant.test_light_actuate",
            input_data={"target_state": "on"},
            caller_permissions=perms,
        )
        engine.approve(item.id, approver="alice_engineer", approval_token="secret_replay_token")

        # First execution succeeds
        res1 = await engine.execute(item.id, caller_permissions=perms)
        assert res1.status == "succeeded"
        assert len(state_box["mutation_calls"]) == 1

        # Second attempt to execute the same approved execution must fail closed
        with pytest.raises(ValidationError) as exc:
            await engine.execute(item.id, caller_permissions=perms)
        assert "cannot execute" in str(exc.value).lower()
        # Zero additional mutation calls
        assert len(state_box["mutation_calls"]) == 1


@pytest.mark.anyio
async def test_tampered_persisted_input_json_triple_digest_agreement(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Triple-digest agreement: If persisted input_json is mutated after approval, execution aborts without mutation."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "triple_digest_token")
    transport, state_box = make_mock_ha_transport(initial_state="off", mutate_on_service=True)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ro_client = HomeAssistantClient(http_client=http_client)
        act_service = HomeAssistantActuationService(readonly_client=ro_client, http_client=http_client)
        custom_cap = HomeAssistantTestLightActuationCapability(service=act_service)
        monkeypatch.setattr(registry, "_capabilities", {**registry._capabilities, "homeassistant.test_light_actuate": custom_cap})

        engine = ToolExecutionEngine(db_session, registry)
        perms = {"homeassistant:actuate"}

        item = engine.submit(
            capability_id="homeassistant.test_light_actuate",
            input_data={"target_state": "off"},
            caller_permissions=perms,
        )
        approved = engine.approve(item.id, approver="lead_engineer", approval_token="triple_digest_token")
        assert approved.status == "queued"

        # Tamper with input_json directly in DB (changing target_state to 'on')
        # while keeping stored input_digest unchanged
        approved.input_json = {"target_state": "on"}
        db_session.commit()

        # Execute must detect that recomputed digest != stored/approved digest and abort immediately
        with pytest.raises(AuthorizationError) as exc:
            await engine.execute(item.id, caller_permissions=perms)
        assert "Approval invalidated" in str(exc.value)
        # Exactly zero mutation requests dispatched!
        assert len(state_box["mutation_calls"]) == 0


def test_propose_endpoint_requires_approval_secret_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/homeassistant/actuation/propose fails closed when token is missing or secret is unset."""
    # 1. Secret unset on server
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", None)
    with TestClient(app) as client:
        resp = client.post(
            "/api/homeassistant/actuation/propose",
            json={"target_state": "on"},
            headers={"X-Pico-Approval-Token": "some_token"},
        )
        assert resp.status_code == 403

    # 2. Secret set, but wrong token sent
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "real_token_123")
    with TestClient(app) as client:
        resp2 = client.post(
            "/api/homeassistant/actuation/propose",
            json={"target_state": "on"},
            headers={"X-Pico-Approval-Token": "bad_token"},
        )
        assert resp2.status_code == 403


def test_home_assistant_disabled_refuses_actuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse actuation if HOME_ASSISTANT_ENABLED is False."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", False)
    with pytest.raises(AuthorizationError) as exc:
        validate_actuation_preconditions(operation="turn_on")
    assert "integration is disabled" in str(exc.value).lower()
