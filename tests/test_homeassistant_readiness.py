"""Adversarial and functional tests for P7.3 Commissioning & Readiness Gate.

Verifies:
1. Empty or missing configuration produces an honest non-ready result.
2. A missing approval secret is reported as a failure for physical-action readiness.
3. Invalid, non-light or mismatched entity configuration fails closed.
4. Invalid or insecure transport configuration is rejected by policy.
5. Offline preflight performs zero network requests.
6. A requested live preflight may perform read-only requests but makes zero mutation requests.
7. Even if actuation is configured as enabled in a test fixture, preflight never invokes mutation services.
8. Preflight never invokes execution engine or consumes approval tickets.
9. Secrets and authorization tokens never appear in results, logs or audit payloads.
10. API endpoint and capability work cleanly with loopback/auth policies.
11. CLI runner operates correctly in offline and live modes.
"""

import io
import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from pico.capabilities.base import RiskTier
from pico.capabilities.homeassistant.ha_readiness import (
    HomeAssistantActuationReadinessCapability,
)
from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.homeassistant.contracts import (
    ActuationReadinessReport,
    ReadinessCheckStatus,
    ReadinessErrorCategory,
)
from pico.homeassistant.readiness import (
    HomeAssistantReadinessChecker,
    evaluate_actuation_readiness,
    main as readiness_cli_main,
)
from pico.main import app
from pico.services.execution_engine import ToolExecutionEngine


TEST_LIGHT_ID = "light.p7_test_lamp"
MOCK_BASE_URL = "http://127.0.0.1:8123"
MOCK_HA_TOKEN = "mock_super_secret_ha_token_xyz789"
MOCK_APPROVAL_SECRET = "mock_super_secret_approval_token_abc123"


@pytest.fixture(autouse=True)
def configure_safe_readiness_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default baseline: HA enabled, test light configured, actuation disabled by default."""
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", MOCK_BASE_URL)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", MOCK_HA_TOKEN)
    # Physical actuation must remain disabled by default during commissioning readiness
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", False)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", TEST_LIGHT_ID)
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", MOCK_APPROVAL_SECRET)


def make_spy_ha_transport(
    initial_state: str = "off",
    light_exists: bool = True,
) -> tuple[httpx.MockTransport, dict[str, list[dict[str, Any]]]]:
    """Mock httpx transport tracking calls and explicitly recording any mutation attempt."""
    calls: dict[str, list[dict[str, Any]]] = {
        "read_calls": [],
        "mutation_calls": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path

        # Read-only state inspection
        if request.method == "GET" and url_path.startswith("/api/states/"):
            calls["read_calls"].append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
            })
            entity_id = url_path.split("/")[-1]
            if light_exists and entity_id == TEST_LIGHT_ID:
                return httpx.Response(
                    200,
                    json={
                        "entity_id": TEST_LIGHT_ID,
                        "state": initial_state,
                        "attributes": {"friendly_name": "P7 Commissioning Test Light"},
                    },
                )
            return httpx.Response(404, json={"message": "Entity not found"})

        # Service mutations: MUST NOT BE CALLED by readiness
        if request.method == "POST" or url_path.startswith("/api/services/"):
            calls["mutation_calls"].append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "content": request.content.decode("utf-8") if request.content else "",
            })
            return httpx.Response(200, json=[])

        return httpx.Response(404, json={"message": "Not handled"})

    return httpx.MockTransport(handler), calls


# =====================================================================
# 1. Empty or missing configuration
# =====================================================================

@pytest.mark.anyio
async def test_readiness_empty_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ENABLED", False)
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", "")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACCESS_TOKEN", "")
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", "")
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "")

    report = await evaluate_actuation_readiness(check_live=False)

    assert report.ready_for_commissioning is False
    assert report.live_check_performed is False
    assert report.live_passed is None

    checks = {c.name: c for c in report.checks}
    assert checks["integration_configuration"].status == ReadinessCheckStatus.NOT_CONFIGURED
    assert checks["target_test_light"].status == ReadinessCheckStatus.NOT_CONFIGURED

    assert checks["approval_credential"].status == ReadinessCheckStatus.NOT_CONFIGURED
    assert checks["authorization_path"].status == ReadinessCheckStatus.FAIL
    assert checks["transport_security"].status == ReadinessCheckStatus.NOT_CONFIGURED

    assert checks["actuation_disabled_by_default"].status == ReadinessCheckStatus.PASS
    assert checks["live_test_light_readability"].status == ReadinessCheckStatus.NOT_CHECKED


# =====================================================================
# 2. Missing approval secret fails physical action readiness
# =====================================================================

@pytest.mark.anyio
async def test_readiness_missing_approval_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "")

    report = await evaluate_actuation_readiness(check_live=False)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    assert checks["approval_credential"].status == ReadinessCheckStatus.NOT_CONFIGURED
    assert "APPROVAL_SECRET_TOKEN" in checks["approval_credential"].remediation


# =====================================================================
# 3. Invalid, non-light or mismatched entity configuration
# =====================================================================

@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_entity_id",
    [
        "switch.living_room_switch",
        "sensor.temperature",
        "light.",
        "light.has spaces",
        "light.injection;drop",
        "invalid_entity",
    ],
)
async def test_readiness_invalid_entity_fails(
    invalid_entity_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID", invalid_entity_id)

    report = await evaluate_actuation_readiness(check_live=False)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    assert checks["target_test_light"].status == ReadinessCheckStatus.FAIL
    assert "light" in checks["target_test_light"].remediation.lower() or "HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID" in checks["target_test_light"].remediation


# =====================================================================
# 4. Insecure transport configuration is rejected
# =====================================================================

@pytest.mark.anyio
@pytest.mark.parametrize(
    "insecure_url",
    [
        "http://evil.com:8123",
        "http://192.168.1.50:8123",
        "ftp://127.0.0.1:8123",
        "not_a_valid_url",
    ],
)
async def test_readiness_insecure_transport_rejected(
    insecure_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", insecure_url)

    report = await evaluate_actuation_readiness(check_live=False)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    assert checks["transport_security"].status == ReadinessCheckStatus.FAIL


# =====================================================================
# 5. Offline preflight performs ZERO network requests
# =====================================================================

@pytest.mark.anyio
async def test_offline_preflight_performs_zero_network_requests() -> None:
    transport, calls = make_spy_ha_transport()

    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        report = await evaluate_actuation_readiness(check_live=False)

    # Prove zero network calls made
    assert len(calls["read_calls"]) == 0
    assert len(calls["mutation_calls"]) == 0
    assert report.live_check_performed is False
    assert report.live_passed is None

    checks = {c.name: c for c in report.checks}
    assert checks["live_test_light_readability"].status == ReadinessCheckStatus.NOT_CHECKED
    assert report.ready_for_commissioning is False
    assert "NOT been performed" in report.summary or "has not been performed" in report.summary.lower()


# =====================================================================
# 6. Live preflight makes read-only requests and ZERO mutation requests
# =====================================================================

@pytest.mark.anyio
async def test_live_preflight_makes_read_only_and_zero_mutation_calls() -> None:
    transport, calls = make_spy_ha_transport(initial_state="off")

    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    # Prove strictly zero mutations
    assert len(calls["mutation_calls"]) == 0
    # Prove read call occurred
    assert len(calls["read_calls"]) == 1
    assert calls["read_calls"][0]["method"] == "GET"
    assert "/api/states/light.p7_test_lamp" in calls["read_calls"][0]["url"]

    assert report.live_check_performed is True
    assert report.live_passed is True
    assert report.ready_for_commissioning is True

    checks = {c.name: c for c in report.checks}
    assert checks["live_test_light_readability"].status == ReadinessCheckStatus.PASS


@pytest.mark.anyio
async def test_live_preflight_missing_entity_reported_honestly() -> None:
    transport, calls = make_spy_ha_transport(light_exists=False)

    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    assert len(calls["mutation_calls"]) == 0
    assert report.live_check_performed is True
    assert report.live_passed is False
    assert report.ready_for_commissioning is False

    checks = {c.name: c for c in report.checks}
    assert checks["live_test_light_readability"].status == ReadinessCheckStatus.FAIL


# =====================================================================
# 7. Even if actuation is configured enabled, preflight never mutates & fails safety check
# =====================================================================

@pytest.mark.anyio
async def test_preflight_with_actuation_enabled_never_mutates_and_fails_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicitly enable actuation in fixture
    monkeypatch.setattr(settings, "HOME_ASSISTANT_ACTUATION_ENABLED", True)

    transport, calls = make_spy_ha_transport()

    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    # Prove zero mutations even when actuation is enabled
    assert len(calls["mutation_calls"]) == 0

    # Actuation disabled by default check must FAIL because actuation is enabled before commissioning
    checks = {c.name: c for c in report.checks}
    assert checks["actuation_disabled_by_default"].status == ReadinessCheckStatus.FAIL
    assert report.ready_for_commissioning is False
    assert "HOME_ASSISTANT_ACTUATION_ENABLED=false" in checks["actuation_disabled_by_default"].remediation


# =====================================================================
# 8. Preflight never invokes execution engine or consumes approval tickets
# =====================================================================

@pytest.mark.anyio
async def test_preflight_never_invokes_execution_engine_or_claims() -> None:
    with (
        patch.object(ToolExecutionEngine, "submit") as mock_submit,
        patch.object(ToolExecutionEngine, "execute") as mock_execute,
    ):
        report_offline = await evaluate_actuation_readiness(check_live=False)
        assert mock_submit.call_count == 0
        assert mock_execute.call_count == 0

        transport, calls = make_spy_ha_transport()
        with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            report_live = await evaluate_actuation_readiness(check_live=True)

        assert mock_submit.call_count == 0
        assert mock_execute.call_count == 0
        assert len(calls["mutation_calls"]) == 0


# =====================================================================
# 9. Secrets and authorization tokens never appear in results or logs
# =====================================================================

@pytest.mark.anyio
async def test_secrets_never_leak_in_report_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    transport, calls = make_spy_ha_transport()
    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    serialized_report = json.dumps(report.model_dump())
    serialized_repr = repr(report)
    log_text = caplog.text

    for secret in [MOCK_HA_TOKEN, MOCK_APPROVAL_SECRET]:
        assert secret not in serialized_report, f"Secret leaked in serialized report: {secret}"
        assert secret not in serialized_repr, f"Secret leaked in repr: {secret}"
        assert secret not in log_text, f"Secret leaked in log messages: {secret}"


# =====================================================================
# 10. API endpoint tests (loopback and auth enforcement)
# =====================================================================

def test_api_readiness_endpoint_offline() -> None:
    client = TestClient(app)
    response = client.get("/api/homeassistant/actuation/readiness")
    assert response.status_code == 200

    data = response.json()
    assert data["target_entity_id"] == TEST_LIGHT_ID
    assert data["live_check_performed"] is False
    assert data["live_passed"] is None
    assert data["ready_for_commissioning"] is False


def test_api_readiness_endpoint_live() -> None:
    transport, calls = make_spy_ha_transport()
    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        client = TestClient(app)
        response = client.get("/api/homeassistant/actuation/readiness?check_live=true")

    assert response.status_code == 200
    data = response.json()
    assert data["live_check_performed"] is True
    assert data["live_passed"] is True
    assert data["ready_for_commissioning"] is True
    assert len(calls["mutation_calls"]) == 0


def test_api_readiness_remote_fails_closed_without_token() -> None:
    client = TestClient(app, base_url="http://remote.host")
    with patch("pico.api.homeassistant.is_loopback_host", return_value=False):
        res_no_token = client.get("/api/homeassistant/actuation/readiness")
        assert res_no_token.status_code == 403

        res_bad_token = client.get(
            "/api/homeassistant/actuation/readiness",
            headers={"X-Pico-Approval-Token": "wrong_token"},
        )
        assert res_bad_token.status_code == 403

        res_good_token = client.get(
            "/api/homeassistant/actuation/readiness",
            headers={"X-Pico-Approval-Token": MOCK_APPROVAL_SECRET},
        )
        assert res_good_token.status_code == 200


# =====================================================================
# 11. Capability registration and execution
# =====================================================================

@pytest.mark.anyio
async def test_capability_actuation_readiness_metadata_and_execution() -> None:
    cap = registry.get("homeassistant.actuation_readiness")
    assert cap is not None
    assert cap.metadata.risk_tier == RiskTier.READ_ONLY
    assert cap.metadata.consequential is False

    from pico.capabilities.base import ExecutionContext
    context = ExecutionContext(
        execution_id="exec_test_readiness_001",
        task_id=None,
        session_id=None,
        caller_permissions={"homeassistant:read"},
        context_classification="INTERNAL",
    )

    result_offline = await cap.execute({"check_live": False}, context)
    assert result_offline.success is True
    assert result_offline.output_data["live_check_performed"] is False

    transport, calls = make_spy_ha_transport()
    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result_live = await cap.execute({"check_live": True}, context)

    assert result_live.success is True
    assert result_live.output_data["live_check_performed"] is True
    assert result_live.output_data["ready_for_commissioning"] is True
    assert len(calls["mutation_calls"]) == 0

    verif = await cap.verify({"check_live": True}, result_live.output_data, context)
    assert verif.passed is True



# =====================================================================
# 12. CLI runner tests
# =====================================================================

def test_cli_runner_offline_and_live() -> None:
    # Test offline JSON output
    out_buf = io.StringIO()
    with patch("sys.stdout", out_buf):
        rc = readiness_cli_main(["--json"])
    assert rc == 1  # Not ready because live check was not performed
    parsed = json.loads(out_buf.getvalue())
    assert parsed["live_check_performed"] is False

    # Test live JSON output
    transport, calls = make_spy_ha_transport()
    out_buf_live = io.StringIO()
    with (
        patch("sys.stdout", out_buf_live),
        patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        rc_live = readiness_cli_main(["--live", "--json"])
    assert rc_live == 0  # Ready for commissioning!
    assert len(calls["mutation_calls"]) == 0


# =====================================================================
# 13. Secret-redaction hardening and structured error category tests
# =====================================================================

@pytest.mark.anyio
async def test_readiness_url_query_sentinel_secret_never_leaked(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel_query = "sentinel_query_secret_token_xyz999"
    sentinel_val = "sentinel_val_12345"
    tainted_url = f"http://127.0.0.1:8123/?token={sentinel_query}&key={sentinel_val}"
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", tainted_url)

    caplog.set_level(logging.DEBUG)

    report = await evaluate_actuation_readiness(check_live=False)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    assert checks["transport_security"].status == ReadinessCheckStatus.FAIL
    assert checks["transport_security"].error_category == ReadinessErrorCategory.INVALID_URL

    # Test report dump, repr, summary, messages, remediation
    report_json = json.dumps(report.model_dump())
    report_repr = repr(report)
    log_text = caplog.text

    assert sentinel_query not in report_json
    assert sentinel_val not in report_json
    assert sentinel_query not in report_repr
    assert sentinel_val not in report_repr
    assert sentinel_query not in report.summary
    assert sentinel_val not in report.summary
    for check in report.checks:
        assert sentinel_query not in check.message
        assert sentinel_val not in check.message
        if check.remediation:
            assert sentinel_query not in check.remediation
            assert sentinel_val not in check.remediation
    assert sentinel_query not in log_text
    assert sentinel_val not in log_text


def test_readiness_cli_redacts_sentinel_query_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_query = "sentinel_query_secret_token_xyz999"
    sentinel_val = "sentinel_val_12345"
    tainted_url = f"http://127.0.0.1:8123/?token={sentinel_query}&key={sentinel_val}"
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", tainted_url)

    # Verify JSON CLI output redacts secrets
    out_buf = io.StringIO()
    with patch("sys.stdout", out_buf):
        rc = readiness_cli_main(["--json"])
    assert rc == 1
    cli_out = out_buf.getvalue()
    assert sentinel_query not in cli_out
    assert sentinel_val not in cli_out

    # Also test text CLI output redacts secrets
    out_text_buf = io.StringIO()
    with patch("sys.stdout", out_text_buf):
        rc_text = readiness_cli_main([])
    assert rc_text == 1
    text_out = out_text_buf.getvalue()
    assert sentinel_query not in text_out
    assert sentinel_val not in text_out
    assert "[INVALID_URL]" in text_out


@pytest.mark.anyio
async def test_readiness_url_fragment_and_userinfo_never_leaked(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel_frag = "sentinel_fragment_secret_token_777"
    tainted_frag_url = f"http://127.0.0.1:8123/#{sentinel_frag}"
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", tainted_frag_url)

    caplog.set_level(logging.DEBUG)
    report = await evaluate_actuation_readiness(check_live=False)
    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    assert checks["transport_security"].status == ReadinessCheckStatus.FAIL
    assert checks["transport_security"].error_category == ReadinessErrorCategory.INVALID_URL
    assert sentinel_frag not in json.dumps(report.model_dump())
    assert sentinel_frag not in caplog.text

    sentinel_user = "sentinel_admin_user"
    sentinel_pass = "sentinel_admin_password_888"
    tainted_userinfo_url = f"http://{sentinel_user}:{sentinel_pass}@127.0.0.1:8123"
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", tainted_userinfo_url)

    report_userinfo = await evaluate_actuation_readiness(check_live=False)
    assert report_userinfo.ready_for_commissioning is False
    checks_u = {c.name: c for c in report_userinfo.checks}
    assert checks_u["transport_security"].status == ReadinessCheckStatus.FAIL
    assert checks_u["transport_security"].error_category == ReadinessErrorCategory.INVALID_URL
    report_u_json = json.dumps(report_userinfo.model_dump())
    assert sentinel_user not in report_u_json
    assert sentinel_pass not in report_u_json
    assert sentinel_pass not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "malformed_url",
    [
        "http://127.0.0.1:999999999",
        "http://127.0.0.1:invalid_port",
        "http://",
        "http://[::1",
        "http://127.0.0.1:8123:extra_colon",
        "http://   ",
    ],
)
async def test_readiness_malformed_urls_and_ports_produce_structured_fail(
    malformed_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", malformed_url)

    # Must never crash or raise unhandled ValueError
    report = await evaluate_actuation_readiness(check_live=False)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    assert checks["transport_security"].status == ReadinessCheckStatus.FAIL
    assert checks["transport_security"].error_category == ReadinessErrorCategory.INVALID_URL
    # Ensure raw malformed input or unhandled exception details do not leak into messages
    assert "Traceback" not in checks["transport_security"].message
    assert "ValueError" not in checks["transport_security"].message


@pytest.mark.anyio
async def test_live_preflight_invalid_url_performs_zero_network_and_zero_mutation_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_leak = "leak_sentinel_xyz"
    monkeypatch.setattr(
        settings,
        "HOME_ASSISTANT_BASE_URL",
        f"http://127.0.0.1:invalid_port/?leak={sentinel_leak}",
    )

    transport, calls = make_spy_ha_transport()
    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    # Must perform strictly zero network and mutation requests
    assert len(calls["read_calls"]) == 0
    assert len(calls["mutation_calls"]) == 0
    assert report.live_check_performed is True
    assert report.live_passed is False
    assert report.ready_for_commissioning is False

    checks = {c.name: c for c in report.checks}
    assert checks["transport_security"].status == ReadinessCheckStatus.FAIL
    assert checks["transport_security"].error_category == ReadinessErrorCategory.INVALID_URL
    assert checks["live_test_light_readability"].status == ReadinessCheckStatus.FAIL
    assert checks["live_test_light_readability"].error_category == ReadinessErrorCategory.INVALID_URL

    report_json = json.dumps(report.model_dump())
    assert sentinel_leak not in report_json


@pytest.mark.anyio
async def test_readiness_valid_loopback_and_secure_remote_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Valid loopback HTTP
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", "http://127.0.0.1:8123")
    report1 = await evaluate_actuation_readiness(check_live=False)
    checks1 = {c.name: c for c in report1.checks}
    assert checks1["transport_security"].status == ReadinessCheckStatus.PASS
    assert checks1["transport_security"].error_category is None

    # Valid secure remote HTTPS
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", "https://ha.internal.example:8123")
    report2 = await evaluate_actuation_readiness(check_live=False)
    checks2 = {c.name: c for c in report2.checks}
    assert checks2["transport_security"].status == ReadinessCheckStatus.PASS
    assert checks2["transport_security"].error_category is None

    # Insecure remote HTTP
    monkeypatch.setattr(settings, "HOME_ASSISTANT_BASE_URL", "http://ha.internal.example:8123")
    report3 = await evaluate_actuation_readiness(check_live=False)
    checks3 = {c.name: c for c in report3.checks}
    assert checks3["transport_security"].status == ReadinessCheckStatus.FAIL
    assert checks3["transport_security"].error_category == ReadinessErrorCategory.INSECURE_TRANSPORT


@pytest.mark.anyio
async def test_readiness_live_network_error_sanitization() -> None:
    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by peer at internal_sensitive_host:8123")

    error_transport = httpx.MockTransport(error_handler)
    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=error_transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    live_check = checks["live_test_light_readability"]
    assert live_check.status == ReadinessCheckStatus.FAIL
    assert live_check.error_category == ReadinessErrorCategory.CONNECTION_FAILED
    # Sensitive connection exception text must NOT leak
    assert "internal_sensitive_host" not in json.dumps(report.model_dump())
    assert "internal_sensitive_host" not in live_check.message
    assert "internal_sensitive_host" not in live_check.remediation


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_readiness_live_auth_rejected_error_category(status_code: int) -> None:
    def auth_fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "Unauthorized"})

    auth_transport = httpx.MockTransport(auth_fail_handler)
    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=auth_transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    live_check = checks["live_test_light_readability"]
    assert live_check.status == ReadinessCheckStatus.FAIL
    assert live_check.error_category == ReadinessErrorCategory.AUTHENTICATION_REJECTED


@pytest.mark.anyio
async def test_readiness_live_entity_not_found_error_category() -> None:
    transport, _ = make_spy_ha_transport(light_exists=False)
    with patch("pico.homeassistant.client.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        report = await evaluate_actuation_readiness(check_live=True)

    assert report.ready_for_commissioning is False
    checks = {c.name: c for c in report.checks}
    live_check = checks["live_test_light_readability"]
    assert live_check.status == ReadinessCheckStatus.FAIL
    assert live_check.error_category == ReadinessErrorCategory.ENTITY_NOT_FOUND
