"""Tests for PICO local operator pairing, HttpOnly session lifecycle, and CSRF protection."""

from concurrent.futures import ThreadPoolExecutor
from itertools import count
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from pico.core.errors import AuthorizationError
from pico.core.config import settings
from pico.main import app
from pico.services.operator_session import OperatorSessionService


@pytest.fixture(autouse=True)
def reset_operator_session():
    OperatorSessionService.reset_instance()
    yield
    OperatorSessionService.reset_instance()


def test_pairing_code_generation_is_out_of_band_only():
    client = TestClient(app)
    assert client.post("/api/operator/pairing-code").status_code == 404
    pairing = OperatorSessionService.get_instance().generate_pairing_code()
    assert len(pairing.code) == 6
    assert pairing.code.isdigit()
    assert pairing.approver == "human_operator"


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
    code = OperatorSessionService.get_instance().generate_pairing_code().code

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
    code = OperatorSessionService.get_instance().generate_pairing_code().code
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


def test_operator_session_can_approve_consequential_execution(monkeypatch, client):
    # Set APPROVAL_SECRET_TOKEN on backend to ensure strict mode is active
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "super_secret_server_token_12345")

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
    code = OperatorSessionService.get_instance().generate_pairing_code().code
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


@pytest.mark.parametrize("client_host", ["192.0.2.1", "203.0.113.2"])
def test_pair_rejects_non_loopback_before_header_validation(client_host, monkeypatch):
    service = OperatorSessionService.get_instance()
    code = service.generate_pairing_code().code

    def unexpected_header_validation(*args):
        pytest.fail("Non-loopback request reached header validation")

    monkeypatch.setattr(service, "validate_origin_and_host", unexpected_header_validation)
    client = TestClient(app, client=(client_host, 50000))
    response = client.post("/api/operator/pair", json={"code": code})
    assert response.status_code == 403
    assert "loopback" in response.json()["detail"]
    # Refused remote traffic must not consume the code.
    assert service.pair(code).is_active


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_pair_accepts_loopback(client_host):
    code = OperatorSessionService.get_instance().generate_pairing_code().code
    client = TestClient(app, client=(client_host, 50000))
    assert client.post("/api/operator/pair", json={"code": code}).status_code == 200


def test_pairing_attempt_limit_retires_outstanding_codes():
    service = OperatorSessionService()
    pairing = service.generate_pairing_code()
    for _ in range(service.MAX_PAIRING_ATTEMPTS):
        with pytest.raises(AuthorizationError, match="Invalid or expired"):
            service.pair("000000")
    with pytest.raises(AuthorizationError, match="Invalid or expired"):
        service.pair(pairing.code)
    assert service.get_active_pairing_code() is None
    replacement = service.generate_pairing_code(approver="replacement")
    assert service.pair(replacement.code).approver == "replacement"


def test_successful_pairing_below_attempt_limit_and_expiry():
    service = OperatorSessionService()
    pairing = service.generate_pairing_code(approver="local_operator")
    for _ in range(service.MAX_PAIRING_ATTEMPTS - 1):
        with pytest.raises(AuthorizationError):
            service.pair("000000")
    assert service.pair(" " + pairing.code + " ").approver == "local_operator"
    expired = service.generate_pairing_code(ttl_seconds=-1)
    with pytest.raises(AuthorizationError, match="Invalid or expired"):
        service.pair(expired.code)


def test_concurrent_pairing_consumes_code_once():
    service = OperatorSessionService()
    code = service.generate_pairing_code().code
    barrier = Barrier(8)

    def pair_once(_):
        barrier.wait(timeout=5)
        try:
            return service.pair(code)
        except AuthorizationError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(pair_once, range(8)))
    assert sum(result is not None for result in results) == 1


def test_concurrent_generation_and_pairing_preserve_codes(monkeypatch):
    sequence = count()
    monkeypatch.setattr("pico.services.operator_session.secrets.randbelow", lambda _: next(sequence))
    service = OperatorSessionService()
    initial = service.generate_pairing_code(ttl_seconds=600)
    expired = service.generate_pairing_code(ttl_seconds=-1)

    def generate(_):
        return service.generate_pairing_code()

    with ThreadPoolExecutor(max_workers=8) as pool:
        pairing_result = pool.submit(service.pair, initial.code)
        generated = list(pool.map(generate, range(64)))
        assert pairing_result.result().is_active
    assert service.get_active_pairing_code() is not None
    assert expired.code not in service._active_codes
    for pairing in generated:
        assert service.pair(pairing.code).is_active


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_cors_allows_frontend_requests(origin, method):
    response = TestClient(app).options("/api/operator/pair", headers={
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "Accept, Content-Type, X-CSRF-Token",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "*" not in response.headers["access-control-allow-methods"]
    assert "*" not in response.headers["access-control-allow-headers"]


@pytest.mark.parametrize("origin,method,header", [
    ("http://localhost:3000", "POST", "Content-Type"),
    ("http://127.0.0.1:3000", "POST", "Content-Type"),
    ("http://localhost:8000", "POST", "Content-Type"),
    ("http://127.0.0.1:8000", "POST", "Content-Type"),
    ("https://example.com", "POST", "Content-Type"),
    ("http://localhost:5173", "TRACE", "Content-Type"),
    ("http://localhost:5173", "POST", "X-Untrusted"),
])
def test_cors_rejects_unneeded_origins_methods_and_headers(origin, method, header):
    response = TestClient(app).options("/api/operator/pair", headers={
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": header,
    })
    assert response.status_code == 400


@pytest.mark.parametrize("escape_path", [
    "%2e%2e/outside.txt",
    "%2e%2e/%2e%2e/outside.txt",
    "C:/outside.txt",
    "C:outside.txt",
    "C:%5coutside.txt",
])
def test_spa_fallback_rejects_escape_paths(tmp_path, monkeypatch, escape_path):
    import pico.main as pico_main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA shell", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private outside file", encoding="utf-8")
    monkeypatch.setattr(pico_main, "frontend_dist", dist)
    response = TestClient(app).get("/" + escape_path)
    assert response.status_code == 404
    assert outside.read_text(encoding="utf-8") not in response.text


def test_spa_fallback_rejects_symlink_escape(tmp_path, monkeypatch):
    import pico.main as pico_main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA shell", encoding="utf-8")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("private outside file", encoding="utf-8")
    link = dist / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if __import__("os").name != "nt":
            raise
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    monkeypatch.setattr(pico_main, "frontend_dist", dist)
    response = TestClient(app).get("/linked/secret.txt")
    assert response.status_code == 404
    assert "private outside file" not in response.text
