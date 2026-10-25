"""Tests for PICO local operator pairing, HttpOnly session lifecycle, and CSRF protection."""

import pytest
from fastapi.testclient import TestClient

from pico.core.config import settings
from pico.main import app
from pico.services.operator_session import OperatorSessionService


@pytest.fixture(autouse=True)
def reset_operator_session():
    OperatorSessionService.reset_instance()
    yield
    OperatorSessionService.reset_instance()


def test_pairing_code_generation_loopback():
    client = TestClient(app)
    response = client.post("/api/operator/pairing-code")
    assert response.status_code == 200
    data = response.json()
    assert "code" in data
    assert len(data["code"]) == 6
    assert data["code"].isdigit()
    assert data["approver"] == "human_operator"


def test_pair_with_invalid_code():
    client = TestClient(app)
    response = client.post("/api/operator/pair", json={"code": "000000"})
    assert response.status_code == 403
    assert "Invalid or expired" in response.json()["detail"]


def test_pair_and_session_lifecycle():
    client = TestClient(app)

    # 1. Check unauthenticated session
    res = client.get("/api/operator/session")
    assert res.status_code == 200
    assert res.json()["paired"] is False

    # 2. Generate code
    code_res = client.post("/api/operator/pairing-code")
    code = code_res.json()["code"]

    # 3. Pair
    pair_res = client.post("/api/operator/pair", json={"code": code})
    assert pair_res.status_code == 200
    pair_data = pair_res.json()
    assert pair_data["status"] == "paired"
    assert "csrf_token" in pair_data
    csrf_token = pair_data["csrf_token"]

    # Verify cookie was set
    assert "pico_operator_session" in client.cookies

    # 4. Check authenticated session
    res2 = client.get("/api/operator/session")
    assert res2.status_code == 200
    assert res2.json()["paired"] is True
    assert res2.json()["approver"] == "human_operator"
    assert res2.json()["csrf_token"] == csrf_token

    # 5. One-time code cannot be reused
    reuse_res = client.post("/api/operator/pair", json={"code": code})
    assert reuse_res.status_code == 403

    # 6. Logout
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


def test_csrf_protection_on_mutating_requests():
    client = TestClient(app)

    # Generate code and pair
    code_res = client.post("/api/operator/pairing-code")
    code = code_res.json()["code"]
    pair_res = client.post("/api/operator/pair", json={"code": code})
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
    # Set APPROVAL_SECRET_TOKEN on backend to ensure strict mode is active
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "super_secret_server_token_12345")

    client = TestClient(app)

    # Submit a consequential execution (using a mock or registered capability)
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
        json={"notes": "unauthorized attempt"},
    )
    assert unauth_approve.status_code == 403

    # Now pair an operator session
    code_res = client.post("/api/operator/pairing-code")
    code = code_res.json()["code"]
    pair_res = client.post("/api/operator/pair", json={"code": code})
    assert pair_res.status_code == 200
    csrf_token = pair_res.json()["csrf_token"]

    # Approve with valid operator session + CSRF token (no backend secret exposed!)
    auth_approve = client.post(
        f"/api/tools/executions/{execution_id}/approve",
        json={"notes": "Approved from desktop operator UI"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert auth_approve.status_code == 200
    assert auth_approve.json()["status"] == "queued"
    assert auth_approve.json()["approved_by"] == "local_operator"


def test_spa_fallback_serving_and_api_isolation(tmp_path, monkeypatch):
    import pico.main as pico_main

    # Create hermetic test dist directory
    test_dist = tmp_path / "frontend" / "dist"
    test_dist.mkdir(parents=True)
    index_file = test_dist / "index.html"
    index_file.write_text('<!DOCTYPE html><html><body><div id="root">PICO Desktop</div></body></html>', encoding="utf-8")

    sample_file = test_dist / "sample.txt"
    sample_file.write_text("sample static content", encoding="utf-8")

    monkeypatch.setattr(pico_main, "frontend_dist", test_dist)

    client = TestClient(app)

    # 1. Root serves index.html
    root_res = client.get("/")
    assert root_res.status_code == 200
    assert '<div id="root">' in root_res.text

    # 2. Client route serves index.html
    client_route_res = client.get("/workflows")
    assert client_route_res.status_code == 200
    assert '<div id="root">' in client_route_res.text

    # 3. Direct static file in dist is served
    file_res = client.get("/sample.txt")
    assert file_res.status_code == 200
    assert file_res.text == "sample static content"

    # 4. Unhandled GET on /api returns 404, not SPA index.html
    api_get = client.get("/api/nonexistent_route")
    assert api_get.status_code == 404
    assert '<div id="root">' not in api_get.text

    # 5. Unhandled POST on /api returns 404, never 405 or SPA
    api_post = client.post("/api/nonexistent_route", json={"foo": "bar"})
    assert api_post.status_code == 404
    assert '<div id="root">' not in api_post.text
