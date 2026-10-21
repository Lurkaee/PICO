"""Comprehensive adversarial and functional test suite for Milestone P8.1 OS & Desktop Automation."""

import asyncio
import os
from typing import Any
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pico.capabilities.base import RiskTier
from pico.capabilities.os.inspect_windows import (
    InspectWindowsCapability,
    InspectWindowsInput,
)
from pico.capabilities.os.launch_application import (
    LaunchApplicationCapability,
    LaunchApplicationInput,
)
from pico.capabilities.registry import CapabilityRegistry, registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.main import app
from pico.models.event_ledger import LedgerEvent
from pico.models.tool_execution import ToolExecution
from pico.os_automation.allowlist import (
    ApplicationAllowlistManager,
    compute_launch_profile_fingerprint,
    validate_launch_profile,
)
from pico.os_automation.contracts import (
    ApplicationLaunchResult,
    LaunchProfile,
    LaunchVerificationStatus,
    WindowInspectionResult,
    WindowMetadata,
)
from pico.os_automation.fake import FakeOSAutomationAdapter
from pico.os_automation.manager import (
    get_allowlist_manager,
    get_os_adapter,
    set_allowlist_manager,
    set_os_adapter,
)
from pico.os_automation.sanitization import build_sanitized_environment
from pico.os_automation.windows import WindowsAutomationAdapter
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.tool_execution_repo import ToolExecutionRepository
from pico.services.execution_engine import ToolExecutionEngine

TEST_APPROVAL_TOKEN = "test_p81_secret_approval_token"


@pytest.fixture(autouse=True)
def setup_os_automation_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate OS automation environment with synthetic fake adapter and clean allowlist."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", TEST_APPROVAL_TOKEN)
    monkeypatch.setattr(settings, "OS_AUTOMATION_ENABLED", True)
    monkeypatch.setattr(settings, "OS_APPLICATION_ALLOWLIST", "{}")

    fake_adapter = FakeOSAutomationAdapter(
        synthetic_windows=[
            WindowMetadata(
                handle=1001,
                title="PICO Terminal - Operator Console",
                process_id=4500,
                is_visible=True,
                is_minimized=False,
                is_foreground=True,
            ),
            WindowMetadata(
                handle=1002,
                title="System Monitor - Resource Telemetry",
                process_id=4510,
                is_visible=True,
                is_minimized=False,
                is_foreground=False,
            ),
            WindowMetadata(
                handle=1003,
                title="Minimized Background Service with a Very Long Descriptive Title That Exceeds Normal Limits For Testing Purpose",
                process_id=4520,
                is_visible=True,
                is_minimized=True,
                is_foreground=False,
            ),
        ],
        launch_outcome=LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED,
        simulated_pid=8888,
    )
    set_os_adapter(fake_adapter)

    # Empty allowlist manager
    allowlist_mgr = ApplicationAllowlistManager("{}")
    set_allowlist_manager(allowlist_mgr)

    yield

    # Teardown
    set_os_adapter(None)
    set_allowlist_manager(None)


# =====================================================================
# 1. Allowlist & Profile Security Verification
# =====================================================================


def test_default_application_allowlist_is_empty() -> None:
    """Requirement 1: The default application allowlist is strictly empty."""
    mgr = ApplicationAllowlistManager("{}")
    assert mgr.list_aliases() == []
    assert mgr.get_profile("notepad") is None
    assert mgr.has_alias("calc") is False


def test_unknown_alias_rejected() -> None:
    """Requirement 2: Unknown aliases are rejected."""
    mgr = ApplicationAllowlistManager("{}")
    assert mgr.get_profile("unregistered_app") is None

    # Registering one valid profile does not allow others
    mgr.register_profile({
        "alias": "approved_tool",
        "executable_path": "C:\\Windows\\System32\\approved.exe",
    })
    assert mgr.has_alias("approved_tool") is True
    assert mgr.has_alias("malicious_app") is False
    assert mgr.get_profile("malicious_app") is None


def test_arbitrary_paths_traversal_and_malformed_profiles_fail_closed() -> None:
    """Requirement 3: Arbitrary paths, traversal attempts, injected arguments, and malformed profiles fail closed."""
    # 1. Relative path rejected
    with pytest.raises(ValidationError, match="Executable path must be absolute"):
        validate_launch_profile({
            "alias": "rel_app",
            "executable_path": "relative\\path\\tool.exe",
        })

    # 2. Directory traversal in executable path rejected
    with pytest.raises(ValidationError, match="Path traversal sequence"):
        validate_launch_profile({
            "alias": "traversal_app",
            "executable_path": "C:\\Windows\\System32\\..\\evil.exe",
        })

    # 3. Traversal in alias rejected
    with pytest.raises(ValidationError, match="Malformed launch profile|Invalid application alias"):
        validate_launch_profile({
            "alias": "../evil",
            "executable_path": "C:\\Tools\\app.exe",
        })

    # 4. Prohibited interpreters and shells rejected
    prohibited = ["cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "python.exe", "wscript.exe", "cscript.exe"]
    for sh in prohibited:
        with pytest.raises(ValidationError, match="Prohibited executable"):
            validate_launch_profile({
                "alias": "shell_alias",
                "executable_path": f"C:\\Windows\\System32\\{sh}",
            })

    # 5. Prohibited script extensions rejected (.bat, .cmd, .ps1, .vbs)
    disallowed_scripts = ["run.bat", "script.cmd", "setup.ps1", "macro.vbs", "app.py"]
    for scr in disallowed_scripts:
        with pytest.raises(ValidationError, match="Prohibited script extension"):
            validate_launch_profile({
                "alias": "script_alias",
                "executable_path": f"C:\\Scripts\\{scr}",
            })

    # 6. Environment variable expansion forbidden in path or working directory
    with pytest.raises(ValidationError, match="Environment variable expansion"):
        validate_launch_profile({
            "alias": "env_app",
            "executable_path": "%WINDIR%\\notepad.exe",
        })

    with pytest.raises(ValidationError, match="Environment variable expansion"):
        validate_launch_profile({
            "alias": "env_cwd_app",
            "executable_path": "C:\\Tools\\app.exe",
            "working_dir": "%TEMP%",
        })

    # 7. Non-absolute working directory rejected
    with pytest.raises(ValidationError, match="Working directory must be an absolute path"):
        validate_launch_profile({
            "alias": "cwd_app",
            "executable_path": "C:\\Tools\\app.exe",
            "working_dir": "relative\\cwd",
        })


def test_caller_controlled_arguments_and_directories_cannot_override_server_config() -> None:
    """Requirement 4: Caller-controlled arguments and working directories cannot override server configuration."""
    # LaunchApplicationInput strictly forbids extra fields (extra="forbid")
    with pytest.raises(ValueError):
        LaunchApplicationInput.model_validate({
            "alias": "valid_alias",
            "executable_path": "C:\\Evil\\override.exe",
        })

    with pytest.raises(ValueError):
        LaunchApplicationInput.model_validate({
            "alias": "valid_alias",
            "args": ["--injected-arg"],
        })

    with pytest.raises(ValueError):
        LaunchApplicationInput.model_validate({
            "alias": "valid_alias",
            "working_dir": "C:\\Evil\\Path",
        })

    with pytest.raises(ValueError):
        LaunchApplicationInput.model_validate({
            "alias": "valid_alias",
            "shell": True,
        })


def test_no_shell_execution_is_ever_used() -> None:
    """Requirement 5: Process creation strictly enforces direct argument list with shell=False."""
    fake = FakeOSAutomationAdapter()
    set_os_adapter(fake)

    profile = LaunchProfile(
        alias="test_app",
        executable_path="C:\\Windows\\app.exe",
        args=["--arg1", "val1"],
    )

    asyncio.run(fake.launch_application(profile))

    assert len(fake.launch_history) == 1
    call = fake.launch_history[0]
    assert call["shell"] is False
    assert call["executable_path"] == "C:\\Windows\\app.exe"
    assert call["args"] == ["--arg1", "val1"]


def test_child_processes_cannot_inherit_pico_secrets_or_ha_credentials() -> None:
    """Requirement 6: Child processes cannot inherit sentinel PICO approval tokens or Home Assistant credentials."""
    dirty_env = {
        "SystemRoot": "C:\\Windows",
        "PATH": "C:\\Windows\\System32;C:\\Windows",
        "TEMP": "C:\\Temp",
        "PICO_SECRET_KEY": "super_secret_pico_key",
        "APPROVAL_SECRET_TOKEN": "sentinel_approval_token_value",
        "HOME_ASSISTANT_ACCESS_TOKEN": "ha_super_secret_token",
        "HA_TOKEN": "ha_token_2",
        "CLOUD_API_KEY": "cloud_key_12345",
        "OLLAMA_API_KEY": "ollama_key",
        "DATABASE_PASSWORD": "secret_db_pass",
        "AUTH_BEARER_TOKEN": "bearer_xyz",
        "MY_CUSTOM_CREDENTIAL": "credential_abc",
    }

    clean = build_sanitized_environment(dirty_env)

    # Allowed OS variables preserved
    assert clean["SystemRoot"] == "C:\\Windows"
    assert clean["PATH"] == "C:\\Windows\\System32;C:\\Windows"
    assert clean["TEMP"] == "C:\\Temp"

    # All sensitive variables stripped
    assert "PICO_SECRET_KEY" not in clean
    assert "APPROVAL_SECRET_TOKEN" not in clean
    assert "HOME_ASSISTANT_ACCESS_TOKEN" not in clean
    assert "HA_TOKEN" not in clean
    assert "CLOUD_API_KEY" not in clean
    assert "OLLAMA_API_KEY" not in clean
    assert "DATABASE_PASSWORD" not in clean
    assert "AUTH_BEARER_TOKEN" not in clean
    assert "MY_CUSTOM_CREDENTIAL" not in clean


# =====================================================================
# 2. Window Inspection & Read-Only Bounds Verification
# =====================================================================


@pytest.mark.anyio
async def test_window_enumeration_is_read_only_and_enforces_bounds() -> None:
    """Requirement 7: Window enumeration is read-only and enforces result and title-size bounds."""
    cap = InspectWindowsCapability()
    fake_adapter = FakeOSAutomationAdapter(
        synthetic_windows=[
            WindowMetadata(
                handle=i,
                title=f"Window Title #{i} " + ("X" * 100),
                process_id=1000 + i,
                is_visible=True,
            )
            for i in range(20)
        ]
    )
    set_os_adapter(fake_adapter)

    # Request max 5 windows, max title length 20
    inp = {"max_windows": 5, "max_title_length": 20}
    context = None  # type: ignore

    res = await cap.execute(inp, context)
    assert res.success is True
    assert res.output_data is not None

    windows = res.output_data["windows"]
    assert len(windows) == 5
    assert res.output_data["total_windows_found"] == 20
    assert res.output_data["truncated"] is True

    for w in windows:
        assert len(w["title"]) <= 20

    # Verification passes
    v_res = await cap.verify(inp, res.output_data, res.evidence, context)
    assert v_res.passed is True


@pytest.mark.anyio
async def test_unsupported_platforms_reported_honestly() -> None:
    """Requirement 8: Unsupported platforms are reported honestly."""
    cap = InspectWindowsCapability()
    fake_adapter = FakeOSAutomationAdapter(
        inspection_status="unsupported",
        platform_name="linux",
    )
    set_os_adapter(fake_adapter)

    inp = {"max_windows": 10, "max_title_length": 100}
    res = await cap.execute(inp, None)  # type: ignore
    assert res.success is True
    assert res.output_data["status"] == "unsupported"
    assert res.output_data["platform"] == "linux"
    assert "unsupported" in res.output_data["message"].lower()

    # Also check WindowsAutomationAdapter on non-win32 platform if simulated
    win_adapter = WindowsAutomationAdapter()
    if os.name != "nt":
        unsupported_res = await win_adapter.inspect_windows()
        assert unsupported_res.status == "unsupported"


def test_window_titles_and_process_metadata_receive_conservative_classification() -> None:
    """Requirement 9: Window titles and process metadata receive conservative classification (INTERNAL)."""
    cap = InspectWindowsCapability()
    assert cap.metadata.required_classification == "INTERNAL"
    assert cap.metadata.risk_tier == RiskTier.READ_ONLY
    assert cap.metadata.consequential is False


@pytest.mark.anyio
async def test_window_inspection_cannot_trigger_process_creation_or_physical_actions() -> None:
    """Requirement 10: Window inspection cannot trigger process creation or physical actions."""
    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    cap = InspectWindowsCapability()
    await cap.execute({"max_windows": 10, "max_title_length": 50}, None)  # type: ignore

    # Zero process launches
    assert len(fake_adapter.launch_history) == 0


# =====================================================================
# 3. Execution Engine, Authorization & Approval Verification
# =====================================================================


def test_application_launching_requires_permission_and_valid_approval(db_session: Session) -> None:
    """Requirement 11: Application launching requires the correct permission and valid approval."""
    engine = ToolExecutionEngine(db_session, registry)

    # 1. Submission without os:launch permission fails closed
    with pytest.raises(AuthorizationError, match="lacks required permission 'os:launch'"):
        engine.submit(
            capability_id="os.launch_application",
            input_data={"alias": "calc_tool"},
            caller_permissions={"tools:calculate"},  # Lacks os:launch
        )

    # 2. Submission with os:launch succeeds but transitions to awaiting_approval (consequential=True)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "calc_tool"},
        caller_permissions={"os:launch"},
    )
    assert execution.status == "awaiting_approval"

    # Cannot execute directly without approval
    with pytest.raises(ValidationError, match="expected 'awaiting_approval'|cannot execute"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))


def test_missing_or_invalid_credentials_and_forged_approver_rejected(db_session: Session) -> None:
    """Requirement 12: Missing or invalid credentials and forged approval identities do not authorize launch."""
    engine = ToolExecutionEngine(db_session, registry)

    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "calc_tool"},
        caller_permissions={"os:launch"},
    )

    # 1. Approval with missing token fails closed
    with pytest.raises(AuthorizationError, match="approval fails closed without valid server-side APPROVAL_SECRET_TOKEN"):
        engine.approve(
            execution_id=execution.id,
            approver="admin",
            approval_token=None,
        )

    # 2. Approval with incorrect token fails closed
    with pytest.raises(AuthorizationError, match="approval fails closed without valid server-side APPROVAL_SECRET_TOKEN"):
        engine.approve(
            execution_id=execution.id,
            approver="admin",
            approval_token="wrong_token_value",
        )

    # 3. Forged approver without valid token fails closed
    with pytest.raises(AuthorizationError):
        engine.approve(
            execution_id=execution.id,
            approver="attacker_spoofed_admin",
            approval_token="invalid",
        )


def test_modified_or_stale_approval_bound_launch_input_is_rejected(db_session: Session) -> None:
    """Requirement 13: Modified or stale approval-bound launch input is rejected (triple-digest validation)."""
    engine = ToolExecutionEngine(db_session, registry)

    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "calc_tool"},
        caller_permissions={"os:launch"},
    )

    # Valid approval
    engine.approve(
        execution_id=execution.id,
        approver="admin",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # Tamper with input_json in DB directly
    exec_repo = ToolExecutionRepository(db_session)
    tampered_execution = exec_repo.get_by_id(execution.id)
    assert tampered_execution is not None
    tampered_execution.input_json = {"alias": "malicious_tool"}
    db_session.commit()

    # Execution boundary detects digest mismatch and rejects
    with pytest.raises(AuthorizationError, match="Approval invalidated|Digest mismatch"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))


def test_concurrent_or_repeated_launch_cannot_bypass_atomic_claim(db_session: Session) -> None:
    """Requirement 14: Concurrent or repeated launch attempts cannot bypass atomic claim or single-use approval."""
    # Register an approved profile
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "approved_calc",
        "executable_path": "C:\\Tools\\calc.exe",
    })
    set_allowlist_manager(mgr)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "approved_calc"},
        caller_permissions={"os:launch"},
    )

    engine.approve(
        execution_id=execution.id,
        approver="admin",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # First execution succeeds
    completed = asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))
    assert completed.status == "succeeded"

    # Replay attempt fails closed: status is no longer queued
    with pytest.raises(ValidationError, match="cannot execute"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))


def test_no_automatic_relaunch_occurs_after_timeout_or_uncertain_outcome(db_session: Session) -> None:
    """Requirement 15: No automatic relaunch occurs after timeout or uncertain outcome (max_retries=0)."""
    cap = LaunchApplicationCapability()
    assert cap.metadata.max_retries == 0
    assert cap.metadata.idempotent is False


def test_event_ledger_records_launch_outcomes_without_leaking_credentials(db_session: Session) -> None:
    """Requirement 16: The event ledger records launch outcomes without leaking credentials."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "ledger_app",
        "executable_path": "C:\\Tools\\app.exe",
    })
    set_allowlist_manager(mgr)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "ledger_app"},
        caller_permissions={"os:launch"},
    )

    engine.approve(
        execution_id=execution.id,
        approver="lead_engineer",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    ledger_repo = EventLedgerRepository(db_session)
    events = ledger_repo.list_events(execution_id=execution.id)

    assert len(events) >= 3  # task_submitted, approval_recorded, execution_started, execution_succeeded
    event_types = [e.event_type for e in events]
    assert "approval_recorded" in event_types
    assert "execution_succeeded" in event_types

    # Ensure secret token does NOT appear in any ledger payload
    for e in events:
        payload_str = str(e.payload_json)
        assert TEST_APPROVAL_TOKEN not in payload_str


# =====================================================================
# 4. REST API Endpoint End-to-End Verification
# =====================================================================


def test_api_os_capabilities_discovery(client: TestClient) -> None:
    """Verify capabilities are discoverable via GET /api/tools/capabilities."""
    resp = client.get("/api/tools/capabilities")
    assert resp.status_code == 200
    caps = resp.json()
    cap_ids = [c["capability_id"] for c in caps]

    assert "os.inspect_windows" in cap_ids
    assert "os.launch_application" in cap_ids

    # Inspect windows details
    inspect_cap = next(c for c in caps if c["capability_id"] == "os.inspect_windows")
    assert inspect_cap["risk_tier"] == "READ_ONLY"
    assert inspect_cap["required_permission"] == "os:inspect"
    assert inspect_cap["consequential"] is False

    # Launch app details
    launch_cap = next(c for c in caps if c["capability_id"] == "os.launch_application")
    assert launch_cap["risk_tier"] == "HIGH"
    assert launch_cap["required_permission"] == "os:launch"
    assert launch_cap["consequential"] is True


def test_api_inspect_windows_authorization(client: TestClient) -> None:
    """Verify POST /api/tools/executions for os.inspect_windows requires os:inspect authorization."""
    # 1. Without approval token, default permissions do NOT include os:inspect -> 403
    resp = client.post(
        "/api/tools/executions",
        json={
            "capability_id": "os.inspect_windows",
            "input_data": {"max_windows": 10, "max_title_length": 50},
        },
    )
    assert resp.status_code == 403
    assert "os:inspect" in resp.json()["detail"]

    # 2. With valid X-Pico-Approval-Token, os:inspect is authorized
    resp = client.post(
        "/api/tools/executions",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
        json={
            "capability_id": "os.inspect_windows",
            "input_data": {"max_windows": 10, "max_title_length": 50},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "succeeded"
    assert data["output_data"]["status"] == "success"
    assert len(data["output_data"]["windows"]) > 0


def test_api_end_to_end_application_launch(client: TestClient) -> None:
    """End-to-end REST test of proposing, approving, and executing an allowlisted application."""
    # Setup allowlist
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "api_test_tool",
        "executable_path": "C:\\Tools\\test_tool.exe",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter(
        launch_outcome=LaunchVerificationStatus.INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED,
        simulated_pid=7777,
    )
    set_os_adapter(fake_adapter)

    # 1. Submit without token -> 403
    resp_unauth = client.post(
        "/api/tools/executions",
        json={
            "capability_id": "os.launch_application",
            "input_data": {"alias": "api_test_tool"},
        },
    )
    assert resp_unauth.status_code == 403

    # 2. Submit with token -> 201 awaiting_approval
    resp_submit = client.post(
        "/api/tools/executions",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
        json={
            "capability_id": "os.launch_application",
            "input_data": {"alias": "api_test_tool"},
        },
    )
    assert resp_submit.status_code == 201
    exec_data = resp_submit.json()
    exec_id = exec_data["id"]
    assert exec_data["status"] == "awaiting_approval"

    # 3. Approve without token -> 403
    resp_appr_fail = client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"approver": "admin", "notes": "Unauthenticated test"},
    )
    assert resp_appr_fail.status_code == 403

    # 4. Approve with valid token -> 200 queued
    resp_appr = client.post(
        f"/api/tools/executions/{exec_id}/approve",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
        json={"approver": "lead_engineer", "notes": "Approved for testing"},
    )
    assert resp_appr.status_code == 200
    assert resp_appr.json()["status"] == "queued"

    # 5. Execute -> 200 succeeded
    resp_exec = client.post(
        f"/api/tools/executions/{exec_id}/execute",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
    )
    assert resp_exec.status_code == 200
    res_final = resp_exec.json()
    assert res_final["status"] == "succeeded"
    assert res_final["output_data"]["status"] == LaunchVerificationStatus.INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED.value
    assert res_final["output_data"]["process_id"] == 7777

    # Assert zero real processes launched
    assert len(fake_adapter.launch_history) == 1
    assert fake_adapter.launch_history[0]["shell"] is False


@pytest.mark.anyio
async def test_launch_capability_fails_closed_when_alias_unregistered_at_execution_time() -> None:
    """Proves that even if an execution reaches execute, unallowlisted alias is rejected."""
    cap = LaunchApplicationCapability()
    mgr = ApplicationAllowlistManager("{}")
    set_allowlist_manager(mgr)

    res = await cap.execute({"alias": "ghost_alias"}, None)  # type: ignore
    assert res.success is False
    assert res.output_data["status"] == LaunchVerificationStatus.REQUEST_REJECTED.value
    assert "not allowlisted" in res.error_message


@pytest.mark.anyio
async def test_launch_capability_handles_failed_and_uncertain_statuses() -> None:
    """Proves distinct handling of LAUNCH_FAILED and LAUNCH_UNCERTAIN."""
    cap = LaunchApplicationCapability()
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "failing_app",
        "executable_path": "C:\\Tools\\fail.exe",
    })
    set_allowlist_manager(mgr)

    # 1. Failed launch
    fake_fail = FakeOSAutomationAdapter(
        launch_outcome=LaunchVerificationStatus.LAUNCH_FAILED,
        launch_message="Simulated process creation error",
    )
    set_os_adapter(fake_fail)

    prepared_input = cap.prepare_input({"alias": "failing_app"})
    res_fail = await cap.execute(prepared_input, None)  # type: ignore
    assert res_fail.success is False
    assert res_fail.output_data["status"] == LaunchVerificationStatus.LAUNCH_FAILED.value
    assert "Simulated process creation error" in res_fail.error_message

    # 2. Uncertain launch
    fake_uncertain = FakeOSAutomationAdapter(
        launch_outcome=LaunchVerificationStatus.LAUNCH_UNCERTAIN,
        launch_message="Uncertain timeout",
    )
    set_os_adapter(fake_uncertain)

    res_uncertain = await cap.execute(prepared_input, None)  # type: ignore
    assert res_uncertain.success is False
    assert res_uncertain.output_data["status"] == LaunchVerificationStatus.LAUNCH_UNCERTAIN.value


@pytest.mark.anyio
async def test_launch_capability_verifier_rejection() -> None:
    """Proves independent verifier rejects corrupt or mismatched outputs."""
    cap = LaunchApplicationCapability()

    # Mismatched alias
    v_res1 = await cap.verify(
        {"alias": "expected_alias"},
        {
            "alias": "different_alias",
            "status": LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED.value,
            "executable_path": "C:\\Tools\\app.exe",
            "verification_status": "ok",
        },
        {},
        None,  # type: ignore
    )
    assert v_res1.passed is False
    assert "does not match input alias" in v_res1.reason

    # Invalid status
    v_res2 = await cap.verify(
        {"alias": "my_app"},
        {
            "alias": "my_app",
            "status": "fabricated_unknown_status",
            "executable_path": "C:\\Tools\\app.exe",
            "verification_status": "ok",
        },
        {},
        None,  # type: ignore
    )
    assert v_res2.passed is False
    assert "Unrecognized launch verification status" in v_res2.reason


@pytest.mark.anyio
async def test_windows_adapter_executable_not_found_fails_closed() -> None:
    """Proves that WindowsAutomationAdapter returns LAUNCH_FAILED if executable is missing."""
    win_adapter = WindowsAutomationAdapter()
    profile = LaunchProfile(
        alias="nonexistent",
        executable_path="C:\\Nonexistent\\Directory\\does_not_exist.exe",
    )

    res = await win_adapter.launch_application(profile)
    assert res.status == LaunchVerificationStatus.LAUNCH_FAILED
    assert "not found" in res.message.lower()


# =====================================================================
# 5. Launch Profile Approval Binding & Fingerprint Hardening (P8.1 Follow-up)
# =====================================================================


def test_approved_profile_launches_when_fingerprint_matches(db_session: Session) -> None:
    """P8.1 Follow-up Req 1: A valid approved profile launches through fake adapter when fingerprint matches."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "trusted_notepad",
        "executable_path": "C:\\Windows\\notepad.exe",
        "args": ["--mode", "safe"],
        "working_dir": "C:\\Tools",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter(
        launch_outcome=LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED,
        simulated_pid=1234,
    )
    set_os_adapter(fake_adapter)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "trusted_notepad"},
        caller_permissions={"os:launch"},
    )

    # Server enriched input with launch_fingerprint
    assert execution.input_json.get("launch_fingerprint") is not None
    expected_fp = compute_launch_profile_fingerprint(mgr.get_profile("trusted_notepad"))
    assert execution.input_json.get("launch_fingerprint") == expected_fp

    # Approve
    engine.approve(
        execution_id=execution.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # Execute
    completed = asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))
    assert completed.status == "succeeded"
    assert completed.output_json["status"] == LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED.value
    assert completed.output_json["process_id"] == 1234

    # Process creation occurred exactly once
    assert len(fake_adapter.launch_history) == 1
    assert fake_adapter.launch_history[0]["alias"] == "trusted_notepad"
    assert fake_adapter.launch_history[0]["executable_path"] == "C:\\Windows\\notepad.exe"


def test_changing_executable_path_after_approval_invalidates_execution(db_session: Session) -> None:
    """P8.1 Follow-up Req 2: Changing the executable path after approval invalidates the execution."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "mutating_app",
        "executable_path": "C:\\Tools\\v1\\app.exe",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "mutating_app"},
        caller_permissions={"os:launch"},
    )
    engine.approve(
        execution_id=execution.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # Mutate executable path in server allowlist after approval
    mgr.register_profile({
        "alias": "mutating_app",
        "executable_path": "C:\\Tools\\v2_altered\\app.exe",
    })

    # Execution must fail closed with AuthorizationError
    with pytest.raises(AuthorizationError, match="Approval invalidated.*modified after approval"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    # Zero calls to process creation
    assert len(fake_adapter.launch_history) == 0


def test_changing_fixed_arguments_after_approval_invalidates_execution(db_session: Session) -> None:
    """P8.1 Follow-up Req 3: Changing fixed arguments or argument order after approval invalidates execution."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "arg_app",
        "executable_path": "C:\\Tools\\app.exe",
        "args": ["--mode", "restricted", "--sandbox"],
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "arg_app"},
        caller_permissions={"os:launch"},
    )
    engine.approve(
        execution_id=execution.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # 1. Mutate args: inject a new argument
    mgr.register_profile({
        "alias": "arg_app",
        "executable_path": "C:\\Tools\\app.exe",
        "args": ["--mode", "restricted", "--sandbox", "--injected-flag"],
    })

    with pytest.raises(AuthorizationError, match="Approval invalidated.*modified after approval"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    # Assert zero process creations
    assert len(fake_adapter.launch_history) == 0

    # 2. Mutate args order: same args but swapped order
    mgr.register_profile({
        "alias": "order_app",
        "executable_path": "C:\\Tools\\app.exe",
        "args": ["--first", "--second"],
    })
    execution_order = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "order_app"},
        caller_permissions={"os:launch"},
    )
    engine.approve(
        execution_id=execution_order.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # Swap order in allowlist
    mgr.register_profile({
        "alias": "order_app",
        "executable_path": "C:\\Tools\\app.exe",
        "args": ["--second", "--first"],
    })

    with pytest.raises(AuthorizationError, match="Approval invalidated.*modified after approval"):
        asyncio.run(engine.execute(execution_order.id, caller_permissions={"os:launch"}))

    assert len(fake_adapter.launch_history) == 0


def test_changing_working_directory_after_approval_invalidates_execution(db_session: Session) -> None:
    """P8.1 Follow-up Req 4: Changing the working directory after approval invalidates execution."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "cwd_app",
        "executable_path": "C:\\Tools\\app.exe",
        "working_dir": "C:\\SafeWorkingDirectory",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "cwd_app"},
        caller_permissions={"os:launch"},
    )
    engine.approve(
        execution_id=execution.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # Change working directory after approval
    mgr.register_profile({
        "alias": "cwd_app",
        "executable_path": "C:\\Tools\\app.exe",
        "working_dir": "C:\\AlteredWorkingDirectory",
    })

    with pytest.raises(AuthorizationError, match="Approval invalidated.*modified after approval"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    assert len(fake_adapter.launch_history) == 0


def test_replacing_allowlist_manager_after_approval_cannot_change_target(db_session: Session) -> None:
    """P8.1 Follow-up Req 5: Replacing allowlist manager or config reload cannot change authorized target."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "reload_app",
        "executable_path": "C:\\Tools\\app.exe",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "reload_app"},
        caller_permissions={"os:launch"},
    )
    engine.approve(
        execution_id=execution.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # Case A: Simulate configuration reload wiping or omitting the alias
    empty_mgr = ApplicationAllowlistManager("{}")
    set_allowlist_manager(empty_mgr)

    with pytest.raises(AuthorizationError, match="Approval invalidated.*missing or not allowlisted"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    assert len(fake_adapter.launch_history) == 0

    # Case B: Simulate reload replacing alias with completely different executable
    replaced_mgr = ApplicationAllowlistManager("{}")
    replaced_mgr.register_profile({
        "alias": "reload_app",
        "executable_path": "C:\\Different\\other.exe",
    })
    set_allowlist_manager(replaced_mgr)

    with pytest.raises(AuthorizationError, match="Approval invalidated.*modified after approval"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    assert len(fake_adapter.launch_history) == 0


def test_caller_supplied_missing_or_tampered_fingerprint_rejected(db_session: Session) -> None:
    """P8.1 Follow-up Req 6: Caller-supplied, missing or tampered fingerprint values cannot authorize arbitrary profile."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "auth_target",
        "executable_path": "C:\\Tools\\target.exe",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    engine = ToolExecutionEngine(db_session, registry)

    # 1. Caller supplies forged fingerprint at submission -> fails validation immediately
    with pytest.raises(ValidationError, match="does not match trusted server-computed profile fingerprint"):
        engine.submit(
            capability_id="os.launch_application",
            input_data={
                "alias": "auth_target",
                "launch_fingerprint": "0123456789abcdef" * 4,
            },
            caller_permissions={"os:launch"},
        )

    # 2. Caller supplies fingerprint for unallowlisted alias -> rejected
    with pytest.raises(ValidationError, match="Cannot supply launch_fingerprint for unallowlisted"):
        engine.submit(
            capability_id="os.launch_application",
            input_data={
                "alias": "unallowlisted_ghost",
                "launch_fingerprint": "0123456789abcdef" * 4,
            },
            caller_permissions={"os:launch"},
        )

    # 3. Direct DB tampering of bound launch_fingerprint after approval -> triple digest detects and fails closed
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "auth_target"},
        caller_permissions={"os:launch"},
    )
    engine.approve(
        execution_id=execution.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    exec_repo = ToolExecutionRepository(db_session)
    db_item = exec_repo.get_by_id(execution.id)
    assert db_item is not None
    tampered_input = dict(db_item.input_json)
    tampered_input["launch_fingerprint"] = "9999999999999999" * 4
    db_item.input_json = tampered_input
    db_session.commit()

    with pytest.raises(AuthorizationError, match="Approval invalidated: Persisted input digest does not match"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    assert len(fake_adapter.launch_history) == 0


@pytest.mark.anyio
async def test_capability_execute_direct_missing_or_mismatched_fingerprint_produces_zero_launches() -> None:
    """P8.1 Follow-up Req 7: Direct capability execute with missing/mismatched fingerprint produces zero process calls."""
    cap = LaunchApplicationCapability()
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "direct_app",
        "executable_path": "C:\\Tools\\app.exe",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    # 1. Missing fingerprint
    res1 = await cap.execute({"alias": "direct_app", "launch_fingerprint": None}, None)  # type: ignore
    assert res1.success is False
    assert res1.output_data["status"] == LaunchVerificationStatus.REQUEST_REJECTED.value
    assert "missing" in res1.error_message.lower()
    assert len(fake_adapter.launch_history) == 0

    # 2. Mismatched fingerprint
    res2 = await cap.execute({"alias": "direct_app", "launch_fingerprint": "bad_fp" * 8}, None)  # type: ignore
    assert res2.success is False
    assert res2.output_data["status"] == LaunchVerificationStatus.REQUEST_REJECTED.value
    assert "modified after approval" in res2.error_message.lower()
    assert len(fake_adapter.launch_history) == 0


def test_event_ledger_records_fingerprint_invalidation_without_leaking_secrets(db_session: Session) -> None:
    """P8.1 Follow-up Req 10: Event ledger accurately records fingerprint rejection without leaking secrets."""
    mgr = ApplicationAllowlistManager("{}")
    mgr.register_profile({
        "alias": "ledger_audit_app",
        "executable_path": "C:\\Tools\\app_original.exe",
    })
    set_allowlist_manager(mgr)

    fake_adapter = FakeOSAutomationAdapter()
    set_os_adapter(fake_adapter)

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="os.launch_application",
        input_data={"alias": "ledger_audit_app"},
        caller_permissions={"os:launch"},
    )
    engine.approve(
        execution_id=execution.id,
        approver="lead_operator",
        approval_token=TEST_APPROVAL_TOKEN,
    )

    # Change profile
    mgr.register_profile({
        "alias": "ledger_audit_app",
        "executable_path": "C:\\Tools\\app_tampered.exe",
    })

    with pytest.raises(AuthorizationError):
        asyncio.run(engine.execute(execution.id, caller_permissions={"os:launch"}))

    # Assert zero process calls
    assert len(fake_adapter.launch_history) == 0

    # Inspect event ledger
    ledger_repo = EventLedgerRepository(db_session)
    events = ledger_repo.list_events(execution_id=execution.id)
    event_types = [e.event_type for e in events]

    assert "authorization_denied" in event_types

    denied_event = next(e for e in events if e.event_type == "authorization_denied")
    assert "modified after approval" in denied_event.payload_json["reason"].lower()
    assert denied_event.payload_json["alias"] == "ledger_audit_app"

    # Assert no secrets in any event
    for e in events:
        payload_str = str(e.payload_json)
        assert TEST_APPROVAL_TOKEN not in payload_str
        assert "password" not in payload_str.lower()
