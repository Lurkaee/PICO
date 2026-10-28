"""Tests for PICO local operator pairing, cross-process SQLite challenge persistence,
HttpOnly session lifecycle, and CSRF protection.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pico.core.config import settings
from pico.core.errors import AuthorizationError
from pico.db.session import SessionLocal
from pico.main import app
from pico.models.operator_session import OperatorPairingChallenge
from pico.services.operator_session import OperatorSessionService


@pytest.fixture(autouse=True)
def reset_operator_session():
    OperatorSessionService.reset_instance()
    with SessionLocal() as s:
        s.query(OperatorPairingChallenge).delete()
        s.commit()
    yield
    OperatorSessionService.reset_instance()
    with SessionLocal() as s:
        s.query(OperatorPairingChallenge).delete()
        s.commit()


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


def test_operator_session_lifecycle_and_pairing():
    """Full lifecycle:
    1. Unauthenticated -> paired: False
    2. Trusted CLI generates pairing challenge in shared SQLite store
    3. Client pairs via /api/operator/pair -> receives HttpOnly cookie + CSRF token
    4. Authenticated -> paired: True, approver is bound to local_operator
    5. Replaying code fails (single-use consumption)
    6. Logout clears session
    """
    client = TestClient(app)

    # 1. Check unauthenticated session
    res = client.get("/api/operator/session")
    assert res.status_code == 200
    assert res.json()["paired"] is False

    # 2. Trusted CLI generates pairing challenge in shared persistent store
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(ttl_seconds=300)
    assert len(challenge.code) == 6
    assert challenge.approver == "local_operator"

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
    assert pair_data["approver"] == "local_operator"
    csrf_token = pair_data["csrf_token"]

    # Verify HttpOnly cookie was set scoped to /api
    assert "pico_operator_session" in client.cookies
    set_cookie_header = pair_res.headers.get("set-cookie", "")
    assert "pico_operator_session=" in set_cookie_header
    assert "path=/api" in set_cookie_header.lower()
    assert "httponly" in set_cookie_header.lower()
    assert "samesite=strict" in set_cookie_header.lower()

    # 4. Check authenticated session reflects trusted bound approver
    res2 = client.get("/api/operator/session")
    assert res2.status_code == 200
    assert res2.json()["paired"] is True
    assert res2.json()["approver"] == "local_operator"
    assert res2.json()["csrf_token"] == csrf_token

    # 5. One-time code cannot be reused (consumed atomically)
    reuse_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert reuse_res.status_code == 403
    assert "Invalid or expired" in reuse_res.json()["detail"]

    # 6. Logout revokes session and expires cookie at path=/api
    logout_res = client.post(
        "/api/operator/logout",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert logout_res.status_code == 200
    assert logout_res.json()["status"] == "logged_out"
    logout_cookie_header = logout_res.headers.get("set-cookie", "")
    assert "pico_operator_session=" in logout_cookie_header
    assert "path=/api" in logout_cookie_header.lower()

    # Verify session is now inactive
    res3 = client.get("/api/operator/session")
    assert res3.status_code == 200
    assert res3.json()["paired"] is False


def test_pairing_code_expiry_rejection():
    """Verify expired pairing challenges are rejected."""
    service = OperatorSessionService.get_instance()
    # Create challenge that expired 10 seconds ago
    challenge = service.generate_pairing_code(ttl_seconds=-10)

    client = TestClient(app)
    res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert res.status_code == 403
    assert "Invalid or expired" in res.json()["detail"]


def test_pairing_lockout_after_max_failed_attempts():
    """Verify rate-limiting lockout after repeated invalid pairing attempts."""
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(ttl_seconds=300)

    client = TestClient(app)

    # Submit 5 incorrect codes
    for _ in range(5):
        fail_res = client.post("/api/operator/pair", json={"code": "999999"})
        assert fail_res.status_code == 403

    # Now even the correct code should be locked out!
    locked_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert locked_res.status_code == 403
    assert "locked" in locked_res.json()["detail"].lower()


def test_concurrent_failed_pairing_attempts_atomic_lockout():
    """Verify concurrent invalid attempts atomically increment failed_attempts without lost updates."""
    import threading
    from pico.core.errors import AuthorizationError
    from pico.db.session import SessionLocal

    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(ttl_seconds=300)

    # Use separate database sessions across synchronized concurrent threads
    barrier = threading.Barrier(5)
    errors: list[str] = []

    def attempt_invalid():
        with SessionLocal() as db_session:
            barrier.wait()
            try:
                service.pair("000000", db=db_session)
            except AuthorizationError as exc:
                errors.append(str(exc))

    threads = [threading.Thread(target=attempt_invalid) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 5

    # Check the challenge in database
    with SessionLocal() as verify_session:
        ch = (
            verify_session.query(OperatorPairingChallenge)
            .filter(
                OperatorPairingChallenge.consumed_at.is_(None),
                OperatorPairingChallenge.approver == "local_operator",
            )
            .order_by(OperatorPairingChallenge.created_at.desc())
            .first()
        )
        assert ch is not None
        assert ch.failed_attempts == 5

    # A subsequent attempt with the legitimate code must fail due to lockout
    with SessionLocal() as fresh_db:
        with pytest.raises(AuthorizationError) as exc_info:
            service.pair(challenge.code, db=fresh_db)
        assert "locked" in str(exc_info.value).lower()


def test_untrusted_host_and_origin_rejected():
    """Verify requests with untrusted host or origin are rejected."""
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(ttl_seconds=300)

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

    # 1. Unauthenticated submission of consequential action must fail closed with 403
    unauth_client = TestClient(app)
    unauth_submit = unauth_client.post(
        "/api/tools/executions",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "sys_disk_0", "operation": "wipe"},
        },
        params={"execute_now": False},
    )
    assert unauth_submit.status_code == 403

    # 2. Pair an operator session via trusted CLI
    client = TestClient(app)
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code()
    pair_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert pair_res.status_code == 200
    csrf_token = pair_res.json()["csrf_token"]
    assert pair_res.json()["approver"] == "local_operator"

    # 3. Authenticated operator submits consequential execution
    submit_res = client.post(
        "/api/tools/executions",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "sys_disk_0", "operation": "wipe"},
        },
        params={"execute_now": False},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert submit_res.status_code == 201
    execution_id = submit_res.json()["id"]
    assert submit_res.json()["status"] == "awaiting_approval"

    # 4. Attempt approval without operator session or token -> must fail closed
    unauth_approve = unauth_client.post(
        f"/api/tools/executions/{execution_id}/approve",
        json={"notes": "unauthorized attempt", "approver": "impostor"},
    )
    assert unauth_approve.status_code == 403

    # 5. Approve with valid operator session + CSRF token, attempting to pass spoofed approver in JSON
    auth_approve = client.post(
        f"/api/tools/executions/{execution_id}/approve",
        json={"notes": "Approved from desktop operator UI", "approver": "impostor_name"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert auth_approve.status_code == 200
    assert auth_approve.json()["status"] == "queued"
    # Approver MUST be local_operator, ignoring the JSON parameter
    assert auth_approve.json()["approved_by"] == "local_operator"


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
    assert "loopback" in response.json()["detail"].lower()
    # Refused remote traffic must not consume the code.
    session = service.pair(code)
    assert session.is_active


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_pair_accepts_loopback(client_host):
    code = OperatorSessionService.get_instance().generate_pairing_code().code
    client = TestClient(app, client=(client_host, 50000))
    assert client.post("/api/operator/pair", json={"code": code}).status_code == 200


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_cors_allows_frontend_requests(origin, method):
    response = TestClient(app).options(
        "/api/operator/pair",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "Accept, Content-Type, X-CSRF-Token",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "*" not in response.headers.get("access-control-allow-methods", "")
    assert "*" not in response.headers.get("access-control-allow-headers", "")


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
    response = TestClient(app).options(
        "/api/operator/pair",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": header,
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("escape_path", [
    "%2e%2e/outside.txt",
    "%2e%2e/%2e%2e/outside.txt",
    "C:/outside.txt",
    "C:outside.txt",
    "C:%5coutside.txt",
    "%00",
    "%00/outside.txt",
    "assets/%00",
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


def test_spa_fallback_percent_encoded_nul_path_returns_404(tmp_path, monkeypatch):
    """Verify that requests with percent-encoded NUL bytes return 404 and do not raise 500 or escape."""
    import pico.main as pico_main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA shell", encoding="utf-8")
    monkeypatch.setattr(pico_main, "frontend_dist", dist)
    client = TestClient(app)
    response = client.get("/%00")
    assert response.status_code == 404
    assert response.status_code != 500


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


def test_increment_failed_attempts_retries_on_locked_and_raises_authorization_error_with_cause():
    """Verify busy/locked contention retries with exponential backoff and chains OperationalError as __cause__."""
    service = OperatorSessionService.get_instance()
    locked_exc = OperationalError("UPDATE ...", {}, Exception("database is locked"))

    mock_session = MagicMock()
    mock_session.execute.side_effect = locked_exc

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(AuthorizationError) as exc_info:
            service._increment_failed_attempts(now=datetime.now(timezone.utc), db=mock_session, max_retries=5)

        assert "Database busy during operator pairing verification" in str(exc_info.value)
        assert exc_info.value.__cause__ is locked_exc
        assert mock_session.rollback.call_count == 5
        assert mock_sleep.call_count == 4
        sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_times == [0.01 * (2**i) for i in range(4)]


def test_increment_failed_attempts_immediately_raises_non_busy_operational_error():
    """Verify non-busy database errors in _increment_failed_attempts propagate immediately without retrying."""
    service = OperatorSessionService.get_instance()
    non_busy_exc = OperationalError("UPDATE ...", {}, Exception("disk I/O error"))

    mock_session = MagicMock()
    mock_session.execute.side_effect = non_busy_exc

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(OperationalError) as exc_info:
            service._increment_failed_attempts(now=datetime.now(timezone.utc), db=mock_session, max_retries=5)

        assert exc_info.value is non_busy_exc
        mock_sleep.assert_not_called()
        assert mock_session.rollback.call_count == 1


def test_pair_consumption_retries_on_busy_and_chains_cause():
    """Verify busy/locked contention during challenge consumption retries within bound and chains __cause__."""
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(ttl_seconds=300)
    busy_exc = OperationalError("UPDATE ...", {}, Exception("database table is locked"))

    with patch("time.sleep") as mock_sleep:
        with patch.object(Session, "commit", side_effect=busy_exc):
            with pytest.raises(AuthorizationError) as exc_info:
                service.pair(challenge.code)

            assert "Database busy during operator pairing verification" in str(exc_info.value)
            assert exc_info.value.__cause__ is busy_exc
            assert mock_sleep.call_count == 4
            sleep_times = [call.args[0] for call in mock_sleep.call_args_list]
            assert sleep_times == [0.01 * (2**i) for i in range(4)]


def test_pair_consumption_immediately_raises_non_busy_operational_error():
    """Verify non-busy database errors during pairing consumption propagate immediately without retrying."""
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(ttl_seconds=300)
    non_busy_exc = OperationalError("UPDATE ...", {}, Exception("no such table: missing_tbl"))

    with patch("time.sleep") as mock_sleep:
        with patch.object(Session, "commit", side_effect=non_busy_exc):
            with pytest.raises(OperationalError) as exc_info:
                service.pair(challenge.code)

            assert exc_info.value is non_busy_exc
            mock_sleep.assert_not_called()
