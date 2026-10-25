"""Tests for PICO local operator pairing, cross-process SQLite challenge persistence,
HttpOnly session lifecycle, and CSRF protection.
"""

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from pico.core.config import settings
from pico.db.session import SessionLocal
from pico.main import app
from pico.models.operator_session import OperatorPairingChallenge
from pico.services.operator_session import OperatorSessionService


@pytest.fixture(autouse=True)
def reset_operator_session():
    OperatorSessionService.reset_instance()
    yield
    OperatorSessionService.reset_instance()


def test_pairing_code_cannot_be_minted_via_browser_api():
    """Verify that browser-side self-service code generation endpoint is removed."""
    client = TestClient(app)
    response = client.post("/api/operator/pairing-code")
    assert response.status_code in (404, 405)


def test_pair_with_invalid_code():
    client = TestClient(app)
    response = client.post("/api/operator/pair", json={"code": "000000"})
    assert response.status_code == 403
    assert "Invalid or expired" in response.json()["detail"]


def test_cross_process_cli_pairing_and_session_lifecycle():
    """
    Simulates CLI running in one process (persisting challenge to SQLite)
    and FastAPI TestClient in another process consuming it and managing session lifecycle.
    """
    client = TestClient(app)

    # 1. Check unauthenticated session
    res = client.get("/api/operator/session")
    assert res.status_code == 200
    assert res.json()["paired"] is False

    # 2. Trusted CLI generates pairing challenge in shared persistent store
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(approver="trusted_operator", ttl_seconds=300)
    assert len(challenge.code) == 6
    assert challenge.approver == "trusted_operator"

    # 3. Client attempts to pair and claim a forged approver identity -> must be ignored!
    pair_res = client.post(
        "/api/operator/pair",
        json={"code": challenge.code, "approver": "forged_malicious_actor"},
    )
    assert pair_res.status_code == 200
    pair_data = pair_res.json()
    assert pair_data["status"] == "paired"
    assert "csrf_token" in pair_data
    # Approver MUST be bound to the trusted challenge issuance, NOT the caller's request body
    assert pair_data["approver"] == "trusted_operator"
    csrf_token = pair_data["csrf_token"]

    # Verify HttpOnly cookie was set
    assert "pico_operator_session" in client.cookies

    # 4. Check authenticated session reflects trusted bound approver
    res2 = client.get("/api/operator/session")
    assert res2.status_code == 200
    assert res2.json()["paired"] is True
    assert res2.json()["approver"] == "trusted_operator"
    assert res2.json()["csrf_token"] == csrf_token

    # 5. One-time code cannot be reused (consumed atomically)
    reuse_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert reuse_res.status_code == 403
    assert "Invalid or expired" in reuse_res.json()["detail"]

    # 6. Logout revokes session
    logout_res = client.post(
        "/api/operator/logout",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert logout_res.status_code == 200
    assert logout_res.json()["status"] == "logged_out"

    # Verify session is now inactive
    res3 = client.get("/api/operator/session")
    assert res3.status_code == 200
    assert res3.json()["paired"] is False


def test_pairing_code_expiry_rejection():
    """Verify expired pairing challenges are rejected."""
    service = OperatorSessionService.get_instance()
    # Create challenge that expired 10 seconds ago
    challenge = service.generate_pairing_code(approver="human_operator", ttl_seconds=-10)

    client = TestClient(app)
    res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert res.status_code == 403
    assert "Invalid or expired" in res.json()["detail"]


def test_pairing_lockout_after_max_failed_attempts():
    """Verify rate-limiting lockout after repeated invalid pairing attempts."""
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(approver="human_operator", ttl_seconds=300)

    client = TestClient(app)

    # Submit 5 incorrect codes
    for _ in range(5):
        fail_res = client.post("/api/operator/pair", json={"code": "999999"})
        assert fail_res.status_code == 403

    # Now even the correct code should be locked out!
    locked_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert locked_res.status_code == 403
    assert "locked" in locked_res.json()["detail"].lower()


def test_untrusted_host_and_origin_rejected():
    """Verify requests with untrusted host or origin are rejected."""
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(approver="human_operator", ttl_seconds=300)

    client = TestClient(app)
    untrusted_res = client.post(
        "/api/operator/pair",
        json={"code": challenge.code},
        headers={"Host": "evil-external-site.com", "Origin": "http://evil-external-site.com"},
    )
    assert untrusted_res.status_code == 403
    assert "Untrusted origin or host" in untrusted_res.json()["detail"]


def test_csrf_protection_on_mutating_requests():
    client = TestClient(app)

    # Generate code and pair
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code()
    pair_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert pair_res.status_code == 200
    csrf_token = pair_res.json()["csrf_token"]

    # Attempt mutating call without CSRF token
    fail_res = client.post("/api/operator/logout")
    assert fail_res.status_code == 403
    assert "CSRF" in fail_res.json()["detail"]

    # Attempt mutating call with invalid CSRF token
    fail_res2 = client.post("/api/operator/logout", headers={"X-CSRF-Token": "invalid_csrf_token"})
    assert fail_res2.status_code == 403

    # Mutating call with valid CSRF token succeeds
    ok_res = client.post("/api/operator/logout", headers={"X-CSRF-Token": csrf_token})
    assert ok_res.status_code == 200


def test_operator_session_can_approve_consequential_execution(monkeypatch):
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "super_secret_server_token_12345")

    client = TestClient(app)

    # Submit a consequential execution using test.consequential_action
    submit_res = client.post(
        "/api/tools/executions",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "sys_disk_0", "operation": "wipe"},
        },
        params={"execute_now": False},
    )
    assert submit_res.status_code == 201
    execution_id = submit_res.json()["id"]
    assert submit_res.json()["status"] == "awaiting_approval"

    # Attempt approval without operator session or token -> must fail closed
    unauth_approve = client.post(
        f"/api/tools/executions/{execution_id}/approve",
        json={"notes": "unauthorized attempt", "approver": "human_operator"},
    )
    assert unauth_approve.status_code == 403

    # Now pair an operator session via trusted CLI
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(approver="verified_operator")
    pair_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert pair_res.status_code == 200
    csrf_token = pair_res.json()["csrf_token"]

    # Approve with valid operator session + CSRF token
    auth_approve = client.post(
        f"/api/tools/executions/{execution_id}/approve",
        json={"notes": "Approved from desktop operator UI"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert auth_approve.status_code == 200
    assert auth_approve.json()["status"] == "queued"
    assert auth_approve.json()["approved_by"] == "verified_operator"


def test_spa_fallback_serving_and_api_isolation(tmp_path, monkeypatch):
    import pico.main as pico_main

    # Create hermetic test dist directory
    test_dist = tmp_path / "frontend" / "dist"
    test_dist.mkdir(parents=True)
    index_file = test_dist / "index.html"
    index_file.write_text('<!DOCTYPE html><html><body><div id="root">PICO Desktop</div></body></html>', encoding="utf-8")

    monkeypatch.setattr(pico_main, "frontend_dist", test_dist)

    client = TestClient(app)

    # Non-API route returns index.html SPA fallback
    res = client.get("/")
    assert res.status_code == 200
    assert "PICO Desktop" in res.text

    res_route = client.get("/workflows/wf_123")
    assert res_route.status_code == 200
    assert "PICO Desktop" in res_route.text

    # API routes are NOT shadowed
    api_404 = client.get("/api/unknown_route")
    assert api_404.status_code == 404

    api_post_404 = client.post("/api/unknown_post")
    assert api_post_404.status_code == 404
