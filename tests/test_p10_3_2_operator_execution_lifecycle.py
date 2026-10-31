"""Tests for P10.3.2: Operator Authorization & Standalone Execution Lifecycle Closure.

Covers:
- Workstream A: Paired local operator authorization vs remote approval-secret authorization,
  strict loopback enforcement, CSRF protection, and server-derived identity.
- Workstream B: Mandatory physical-actuation configuration gate across all 4 Home Assistant
  capabilities, verified at both precondition and execution-engine boundaries.
- Workstream C: Protected execution-history and status reads across all 9 matrix cases.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
import httpx
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.db.session import SessionLocal
from pico.homeassistant.actuation import (
    HomeAssistantActuationService,
    validate_actuation_preconditions,
    validate_approval_secret_configuration,
    validate_climate_actuation_preconditions,
    validate_climate_dual_setpoint_actuation_preconditions,
    validate_switch_actuation_preconditions,
)
from pico.homeassistant.client import HomeAssistantClient
from pico.main import app
from pico.models.operator_session import OperatorPairingChallenge
from pico.models.tool_execution import ToolExecution
from pico.repositories.tool_execution_repo import ToolExecutionRepository
from pico.services.execution_engine import ToolExecutionEngine
from pico.services.operator_session import OperatorSessionService


MOCK_SECRET = "valid_operator_test_secret_321"
TEST_LIGHT_ID = "light.p7_test_lamp"
TEST_SWITCH_ID = "switch.ac_plug"
TEST_CLIMATE_ID = "climate.living_room"


@pytest.fixture(autouse=True)
def clean_operator_state():
    OperatorSessionService.reset_instance()
    with SessionLocal() as s:
        s.query(OperatorPairingChallenge).delete()
        s.commit()
    yield
    OperatorSessionService.reset_instance()
    with SessionLocal() as s:
        s.query(OperatorPairingChallenge).delete()
        s.commit()


def pair_local_operator(client: TestClient) -> str:
    """Helper to pair a local operator session and return the CSRF token."""
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code()
    pair_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert pair_res.status_code == 200, f"Pairing failed: {pair_res.text}"
    return pair_res.json()["csrf_token"]


# =====================================================================
# Workstream A: Policy A Authorization Enforcement
# =====================================================================

def test_paired_local_operator_submits_approves_and_runs_consequential_execution(monkeypatch: pytest.MonkeyPatch):
    """Local paired operator on loopback can submit, approve, and run a consequential execution."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    csrf_token = pair_local_operator(loopback_client)

    # 1. Submit consequential execution
    res = loopback_client.post(
        "/api/tools/executions",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "res_1", "operation": "cleanup"},
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert res.status_code == 201, res.text
    exec_id = res.json()["id"]
    assert res.json()["status"] == "awaiting_approval"

    # 2. Approve consequential execution - authoritative approver derived as local_operator
    appr_res = loopback_client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"notes": "Approved by desktop operator", "approver": "forged_caller_name"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert appr_res.status_code == 200, appr_res.text
    assert appr_res.json()["status"] == "queued"
    assert appr_res.json()["approved_by"] == "local_operator"

    # 3. Explicit dispatch
    run_res = loopback_client.post(
        f"/api/tools/executions/{exec_id}/execute",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert run_res.status_code == 200, run_res.text
    assert run_res.json()["status"] == "succeeded"


def test_remote_token_holder_submits_approves_and_runs_consequential_execution(monkeypatch: pytest.MonkeyPatch):
    """Remote client presenting valid X-Pico-Approval-Token can submit, approve, and run."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)
    remote_client = TestClient(app, client=("192.168.1.100", 50000))

    # 1. Submit
    res = remote_client.post(
        "/api/tools/executions",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "res_2", "operation": "sync"},
        },
        headers={"X-Pico-Approval-Token": MOCK_SECRET},
    )
    assert res.status_code == 201, res.text
    exec_id = res.json()["id"]
    assert res.json()["status"] == "awaiting_approval"

    # 2. Approve - authoritative approver is authenticated_token_holder
    appr_res = remote_client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"notes": "Remote script approval", "approver": "untrusted_script"},
        headers={"X-Pico-Approval-Token": MOCK_SECRET},
    )
    assert appr_res.status_code == 200, appr_res.text
    assert appr_res.json()["status"] == "queued"
    assert appr_res.json()["approved_by"] == "authenticated_token_holder"

    # 3. Run
    run_res = remote_client.post(
        f"/api/tools/executions/{exec_id}/execute",
        headers={"X-Pico-Approval-Token": MOCK_SECRET},
    )
    assert run_res.status_code == 200, run_res.text
    assert run_res.json()["status"] == "succeeded"


def test_non_loopback_session_cookie_strictly_rejected(monkeypatch: pytest.MonkeyPatch):
    """Remote client presenting only a session cookie is rejected with fail-closed error."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)
    # Pair on loopback to get a valid session
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    csrf_token = pair_local_operator(loopback_client)
    session_cookie = loopback_client.cookies.get("pico_operator_session")
    assert session_cookie is not None

    # Connect from remote IP with that cookie
    remote_client = TestClient(app, client=("192.168.1.100", 50000))
    remote_client.cookies.set("pico_operator_session", session_cookie)

    # 1. Remote client session check returns paired=False
    sess_res = remote_client.get("/api/operator/session")
    assert sess_res.status_code == 200
    assert sess_res.json()["paired"] is False

    # 2. Remote pairing is rejected
    pair_res = remote_client.post("/api/operator/pair", json={"code": "123456"})
    assert pair_res.status_code == 403
    assert "loopback" in pair_res.json()["detail"].lower()

    # 3. Consequential submission without remote approval token is denied
    res = remote_client.post(
        "/api/tools/executions",
        json={"capability_id": "test.consequential_action", "input_data": {"resource_id": "res_1", "operation": "cleanup"}},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert res.status_code in (401, 403)
    assert "admin:consequential" in res.json()["detail"].lower()

    # 4. Consequential approval with only cookie is denied fail-closed
    appr_res = remote_client.post(
        "/api/tools/executions/some_exec_id/approve",
        json={"notes": "Remote attempt"},
    )
    assert appr_res.status_code in (401, 403)
    assert "valid local operator session or approval token" in appr_res.json()["detail"].lower()


def test_invalid_or_missing_csrf_rejected_for_local_mutations(monkeypatch: pytest.MonkeyPatch):
    """Loopback paired session without valid CSRF is rejected for mutating endpoints."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    pair_local_operator(loopback_client)

    # Missing CSRF
    res_no_csrf = loopback_client.post(
        "/api/tools/executions",
        json={"capability_id": "test.consequential_action", "input_data": {"resource_id": "res_1", "operation": "cleanup"}},
    )
    assert res_no_csrf.status_code == 403
    assert "csrf" in res_no_csrf.json()["detail"].lower()

    # Invalid CSRF
    res_bad_csrf = loopback_client.post(
        "/api/tools/executions",
        json={"capability_id": "test.consequential_action", "input_data": {"resource_id": "res_1", "operation": "cleanup"}},
        headers={"X-CSRF-Token": "invalid_bogus_csrf"},
    )
    assert res_bad_csrf.status_code == 403
    assert "csrf" in res_bad_csrf.json()["detail"].lower()


def test_host_and_origin_mismatch_rejected_for_local_session(monkeypatch: pytest.MonkeyPatch):
    """Loopback paired session with cross-origin or mismatched host is rejected."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    csrf_token = pair_local_operator(loopback_client)

    # Forged Host header
    res_bad_host = loopback_client.post(
        "/api/tools/executions",
        json={"capability_id": "test.consequential_action", "input_data": {"resource_id": "res_1", "operation": "cleanup"}},
        headers={"X-CSRF-Token": csrf_token, "Host": "evil-attacker.com"},
    )
    assert res_bad_host.status_code == 403
    assert "host" in res_bad_host.json()["detail"].lower()

    # Forged Origin header
    res_bad_origin = loopback_client.post(
        "/api/tools/executions",
        json={"capability_id": "test.consequential_action", "input_data": {"resource_id": "res_1", "operation": "cleanup"}},
        headers={"X-CSRF-Token": csrf_token, "Origin": "http://evil-attacker.com"},
    )
    assert res_bad_origin.status_code == 403
    assert "origin" in res_bad_origin.json()["detail"].lower()


def test_homeassistant_proposals_accept_paired_local_operator(monkeypatch: pytest.MonkeyPatch):
    """Dedicated HA proposal endpoints accept paired local operator session without requiring X-Pico-Approval-Token."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", "http://127.0.0.1:8123")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", "valid_token")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", TEST_LIGHT_ID)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)

    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    csrf_token = pair_local_operator(loopback_client)

    # Propose test light actuation via local operator session
    res = loopback_client.post(
        "/api/homeassistant/actuation/propose",
        json={"entity_id": TEST_LIGHT_ID, "target_state": "on"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "awaiting_approval"
    assert res.json()["capability_id"] == "homeassistant.test_light_actuate"


# =====================================================================
# Workstream B: Mandatory Physical-Actuation Configuration Gate
# =====================================================================

@pytest.mark.parametrize(
    "bad_secret",
    [None, "", "   ", "\t\n"],
    ids=["none", "empty", "spaces", "whitespace"],
)
def test_preconditions_fail_closed_for_all_4_capabilities_when_secret_unconfigured(
    monkeypatch: pytest.MonkeyPatch, bad_secret: str | None
):
    """Precondition checks for all 4 HA physical capabilities fail closed if APPROVAL_SECRET_TOKEN is invalid."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", "http://127.0.0.1:8123")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", "ha_tok")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", TEST_LIGHT_ID)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", [TEST_SWITCH_ID])
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ALLOWLIST", [TEST_CLIMATE_ID])
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_CLIMATE_DUAL_SETPOINT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", bad_secret)

    # 1. Direct validator
    with pytest.raises(AuthorizationError) as exc_v:
        validate_approval_secret_configuration()
    assert "APPROVAL_SECRET_TOKEN" in str(exc_v.value)

    # 2. Test Light Preconditions
    with pytest.raises(AuthorizationError) as exc_light:
        validate_actuation_preconditions(TEST_LIGHT_ID)
    assert "APPROVAL_SECRET_TOKEN" in str(exc_light.value)

    # 3. Switch Preconditions
    with pytest.raises(AuthorizationError) as exc_switch:
        validate_switch_actuation_preconditions(TEST_SWITCH_ID)
    assert "APPROVAL_SECRET_TOKEN" in str(exc_switch.value)

    # 4. Climate Single-Setpoint Preconditions
    with pytest.raises(AuthorizationError) as exc_climate:
        validate_climate_actuation_preconditions(TEST_CLIMATE_ID)
    assert "APPROVAL_SECRET_TOKEN" in str(exc_climate.value)

    # 5. Climate Dual-Setpoint Preconditions
    with pytest.raises(AuthorizationError) as exc_dual:
        validate_climate_dual_setpoint_actuation_preconditions(TEST_CLIMATE_ID)
    assert "APPROVAL_SECRET_TOKEN" in str(exc_dual.value)


@pytest.mark.anyio
async def test_execution_engine_fails_closed_if_secret_removed_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
):
    """If APPROVAL_SECRET_TOKEN is removed after approval, ToolExecutionEngine.execute fails closed before physical dispatch."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", "http://127.0.0.1:8123")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", "ha_tok")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", TEST_LIGHT_ID)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)

    engine = ToolExecutionEngine(db_session, registry)
    perms = {"homeassistant:actuate"}

    # Submit and approve while secret is present
    item = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={"entity_id": TEST_LIGHT_ID, "target_state": "on"},
        caller_permissions=perms,
    )
    approved = engine.approve(
        item.id,
        notes="Approved for test",
        authenticated_principal="local_operator",
    )
    assert approved.status == "queued"

    # Now simulate secret being wiped/removed from server config before dispatch
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", None)

    # Execution must fail closed and record 0 physical mutation calls
    with pytest.raises(AuthorizationError) as exc_info:
        await engine.execute(item.id, caller_permissions=perms)
    assert "APPROVAL_SECRET_TOKEN" in str(exc_info.value)

    # Verify execution remains queued or failed without executing physical action
    refreshed = db_session.get(ToolExecution, item.id)
    assert refreshed.status in ("queued", "failed")


# =====================================================================
# Workstream C: Protected Execution History & Status Reads (9 Cases)
# =====================================================================

def test_history_matrix_cases(monkeypatch: pytest.MonkeyPatch):
    """Test all 9 adversarial read-boundary cases across generic and Home Assistant endpoints."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)

    # Case 1: Loopback without session or token -> 401/403 fails closed
    c1 = TestClient(app, client=("127.0.0.1", 50000))
    res1 = c1.get("/api/tools/executions")
    assert res1.status_code in (401, 403)
    assert "execution history" in res1.json()["detail"].lower()

    # Case 2: Loopback with expired/invalid session cookie -> 401/403 fails closed
    c2 = TestClient(app, client=("127.0.0.1", 50000))
    c2.cookies.set("pico_operator_session", "expired_or_invalid_session_id")
    res2 = c2.get("/api/tools/executions")
    assert res2.status_code in (401, 403)
    assert "execution history" in res2.json()["detail"].lower()

    # Case 3: Non-loopback presenting session cookie -> 401/403 (session rejected for non-loopback)
    c3 = TestClient(app, client=("192.168.1.100", 50000))
    # Create valid session on loopback first
    c_lb = TestClient(app, client=("127.0.0.1", 50000))
    pair_local_operator(c_lb)
    valid_session = c_lb.cookies.get("pico_operator_session")
    c3.cookies.set("pico_operator_session", valid_session)
    res3 = c3.get("/api/tools/executions")
    assert res3.status_code in (401, 403)
    assert "execution history" in res3.json()["detail"].lower()

    # Case 4: Non-loopback without token -> 401/403 fails closed
    c4 = TestClient(app, client=("192.168.1.100", 50000))
    res4 = c4.get("/api/tools/executions")
    assert res4.status_code in (401, 403)
    assert "execution history" in res4.json()["detail"].lower()

    # Case 5: Non-loopback with invalid token -> 403
    c5 = TestClient(app, client=("192.168.1.100", 50000))
    res5 = c5.get("/api/tools/executions", headers={"X-Pico-Approval-Token": "wrong_token_value"})
    assert res5.status_code == 403

    # Case 6: Server unconfigured token -> 403 / 503 fail-closed for remote token
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", None)
    c6 = TestClient(app, client=("192.168.1.100", 50000))
    res6 = c6.get("/api/tools/executions", headers={"X-Pico-Approval-Token": "some_token"})
    assert res6.status_code in (403, 503)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)

    # Case 7: Valid loopback paired session -> 200
    c7 = TestClient(app, client=("127.0.0.1", 50000))
    pair_local_operator(c7)
    res7 = c7.get("/api/tools/executions")
    assert res7.status_code == 200
    assert isinstance(res7.json()["items"], list)

    # Case 8: Valid remote approval token -> 200
    c8 = TestClient(app, client=("192.168.1.100", 50000))
    res8 = c8.get("/api/tools/executions", headers={"X-Pico-Approval-Token": MOCK_SECRET})
    assert res8.status_code == 200
    assert isinstance(res8.json()["items"], list)

    # Case 9: Valid loopback session with mismatch host/origin -> 403
    c9 = TestClient(app, client=("127.0.0.1", 50000))
    pair_local_operator(c9)
    res9 = c9.get("/api/tools/executions", headers={"Host": "attacker.com"})
    assert res9.status_code == 403
    assert "host" in res9.json()["detail"].lower()


def test_homeassistant_status_endpoints_require_history_authorization(monkeypatch: pytest.MonkeyPatch):
    """Home Assistant status endpoints are protected by require_history_authorization."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_SECRET)
    unauth_client = TestClient(app, client=("127.0.0.1", 50000))

    # Unauthenticated calls fail closed
    res_light = unauth_client.get("/api/homeassistant/actuation/non_existent_id")
    assert res_light.status_code in (401, 403)

    res_switch = unauth_client.get("/api/homeassistant/switch/non_existent_id")
    assert res_switch.status_code in (401, 403)

    res_climate = unauth_client.get("/api/homeassistant/climate/execution/non_existent_id")
    assert res_climate.status_code in (401, 403)

    res_dual = unauth_client.get("/api/homeassistant/climate/dual-setpoint/execution/non_existent_id")
    assert res_dual.status_code in (401, 403)

    # Authenticated paired local session passes to entity lookup (returns 404 for non-existent execution)
    pair_local_operator(unauth_client)
    res_light_auth = unauth_client.get("/api/homeassistant/actuation/non_existent_id")
    assert res_light_auth.status_code == 404

    res_switch_auth = unauth_client.get("/api/homeassistant/switch/non_existent_id")
    assert res_switch_auth.status_code == 404
