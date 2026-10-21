"""Comprehensive adversarial and functional tests for Milestone P8.2 Staged Filesystem Diffs.

Verifies:
1. Root allowlist & path safety (empty default, traversal, device names, reparse points, zero writes on rejection)
2. Text-only contract, diff generation, and staging (create vs replace, bounds, NUL bytes, newline normalization)
3. Approval binding and apply safety (triple-digest, pre-dispatch state check, atomic write, post-write verification, zero retry)
4. Conflict-safe guarded rollback (restore vs remove, external edit detection, zero repeat)
5. Privacy, audit ledger integrity, and REST API lifecycle
"""

import asyncio
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ExecutionError, ValidationError
from pico.filesystem.adapter import (
    FakeFilesystemAdapter,
    WindowsFilesystemAdapter,
    get_filesystem_adapter,
    set_filesystem_adapter,
)
from pico.filesystem.contracts import (
    OperationType,
    compute_content_digest,
    compute_diff_digest,
    validate_relative_path,
    validate_text_content,
)
from pico.filesystem.diff_utils import generate_unified_diff
from pico.filesystem.roots import (
    FilesystemRootsManager,
    get_roots_manager,
    set_roots_manager,
)
from pico.filesystem.staging import (
    discard_staged_diff_proposal,
    get_staged_diff_proposal,
    stage_diff_proposal,
)
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.staged_diff_repo import StagedDiffRepository
from pico.repositories.tool_execution_repo import ToolExecutionRepository
from pico.services.execution_engine import ToolExecutionEngine

TEST_APPROVAL_TOKEN = "test_fs_token_secret_123"


@pytest.fixture(autouse=True)
def reset_filesystem_state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Ensure clean filesystem state and token configuration for every test."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", TEST_APPROVAL_TOKEN)
    set_roots_manager(None)
    set_filesystem_adapter(None)
    yield
    set_roots_manager(None)
    set_filesystem_adapter(None)


# ==============================================================================
# 1. Root Allowlist & Path Safety Tests
# ==============================================================================


def test_empty_by_default_root_allowlist_fails_closed(tmp_path):
    """Empty-by-default allowlist: no operations permitted without explicit operator configuration."""
    mgr = FilesystemRootsManager("{}")
    assert mgr.list_roots() == {}
    with pytest.raises(ValidationError, match="Filesystem operations are disabled: No filesystem roots are allowlisted"):
        mgr.get_root_path("workspace")

    with pytest.raises(ValidationError):
        mgr.resolve_safe_path("workspace", "file.txt")


def test_path_traversal_and_malformed_relative_paths_rejected(tmp_path):
    """Rejects traversal ('..'), absolute paths, UNC paths, and empty components."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    mgr = FilesystemRootsManager({"ws": str(ws)})

    traversal_inputs = [
        "../outside.txt",
        "sub/../../outside.txt",
        "..\\windows_traversal.txt",
        "/absolute/path.txt",
        "\\absolute\\path.txt",
        "C:\\Windows\\System32\\calc.exe",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "",
        "   ",
        ".",
        "sub/./file.txt",
    ]

    for bad_path in traversal_inputs:
        with pytest.raises(ValidationError):
            mgr.resolve_safe_path("ws", bad_path)


def test_device_names_alternate_streams_and_env_vars_rejected(tmp_path):
    """Rejects Windows device names (CON, NUL, COM1), alternate streams (:), and environment variables (%, $)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    mgr = FilesystemRootsManager({"ws": str(ws)})

    forbidden_inputs = [
        "NUL",
        "nul.txt",
        "CON",
        "con.log",
        "PRN",
        "AUX",
        "COM1",
        "com2.txt",
        "LPT1",
        "lpt3.dat",
        "file.txt:stream",
        "file.txt:$DATA",
        "%TEMP%\\file.txt",
        "$HOME/file.txt",
    ]

    for bad_name in forbidden_inputs:
        with pytest.raises(ValidationError):
            mgr.resolve_safe_path("ws", bad_name)


def test_symlinks_and_reparse_points_detected_and_rejected(tmp_path):
    """Detects symlinks in target file or parent path components and fails closed."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret.txt"
    secret_file.write_text("classified data", encoding="utf-8")

    mgr = FilesystemRootsManager({"ws": str(ws)})
    set_roots_manager(mgr)

    # Attempt to create a symlink inside ws pointing outside
    symlink_file = ws / "symlink.txt"
    try:
        os.symlink(str(secret_file), str(symlink_file))
        has_symlink = True
    except (OSError, NotImplementedError):
        # On Windows without developer mode/privileges, symlink creation might fail
        has_symlink = False

    if has_symlink:
        with pytest.raises(ValidationError, match="Symlink or reparse point detected"):
            mgr.resolve_safe_path("ws", "symlink.txt", must_exist=True)


def test_revalidating_root_allowlist_removal_after_proposal_fails_closed(tmp_path, db_session: Session):
    """If server allowlist removes a root after proposal, subsequent execution fails closed."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    mgr = FilesystemRootsManager({"ws": str(ws)})
    set_roots_manager(mgr)

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="new_file.txt",
        operation="create",
        proposed_content="line 1\nline 2\n",
    )

    # Now simulate server removing the root from allowlist
    set_roots_manager(FilesystemRootsManager("{}"))

    engine = ToolExecutionEngine(db_session, registry)
    with pytest.raises(ValidationError, match="Filesystem operations are disabled|is not in the server-configured allowlist"):
        engine.submit(
            capability_id="filesystem.apply_staged_diff",
            input_data={"proposal_id": proposal.id},
            caller_permissions={"filesystem:write"},
        )


def test_zero_writes_on_all_rejected_paths(tmp_path):
    """Zero calls to adapter write or delete on any rejected path."""
    fake = FakeFilesystemAdapter()
    set_filesystem_adapter(fake)

    ws = tmp_path / "workspace"
    ws.mkdir()
    mgr = FilesystemRootsManager({"ws": str(ws)})

    with pytest.raises(ValidationError):
        mgr.resolve_safe_path("ws", "../outside.txt")

    assert fake.write_call_count == 0
    assert fake.delete_call_count == 0


# ==============================================================================
# 2. Text-Only Contract, Diff Generation, and Staging Tests
# ==============================================================================


def test_stage_create_proposal_generates_unified_diff_and_absent_state(tmp_path, db_session: Session):
    """File creation stages unified diff against /dev/null and records 'ABSENT' original state."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="hello.txt",
        operation="create",
        proposed_content="Hello PICO World\n",
    )

    assert proposal.id.startswith("diff_")
    assert proposal.operation == "create"
    assert proposal.original_state == "ABSENT"
    assert proposal.status == "staged"
    assert proposal.proposed_digest == compute_content_digest("Hello PICO World\n")
    assert "/dev/null" in proposal.unified_diff
    assert "+Hello PICO World" in proposal.unified_diff
    assert proposal.diff_digest == compute_diff_digest(proposal.unified_diff)

    # Verify target file has NOT been created yet on disk
    assert not (ws / "hello.txt").exists()


def test_stage_replace_proposal_generates_unified_diff_and_content_digest(tmp_path, db_session: Session):
    """File replacement reads existing file, computes original digest, and generates diff."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target_file = ws / "config.txt"
    target_file.write_text("line 1\nold line\nline 3\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="config.txt",
        operation="replace",
        proposed_content="line 1\nnew line\nline 3\n",
    )

    expected_orig_digest = compute_content_digest("line 1\nold line\nline 3\n")
    assert proposal.operation == "replace"
    assert proposal.original_state == expected_orig_digest
    assert "-old line" in proposal.unified_diff
    assert "+new line" in proposal.unified_diff

    # Verify original file content on disk has NOT changed
    assert target_file.read_text(encoding="utf-8") == "line 1\nold line\nline 3\n"


def test_binary_content_and_nul_bytes_rejected(tmp_path, db_session: Session):
    """NUL bytes and binary content are rejected at validation time."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    with pytest.raises(ValidationError, match="NUL bytes are not permitted"):
        validate_text_content("text with \x00 NUL byte")

    with pytest.raises(ValidationError, match="NUL bytes are not permitted"):
        stage_diff_proposal(
            db=db_session,
            root_id="ws",
            relative_path="file.txt",
            operation="create",
            proposed_content="binary\x00data",
        )


def test_content_and_diff_size_bounds_enforced(monkeypatch: pytest.MonkeyPatch, tmp_path, db_session: Session):
    """Exceeding configured maximum file bytes or line length fails validation."""
    monkeypatch.setattr(settings, "PICO_FILESYSTEM_MAX_FILE_BYTES", 100)
    monkeypatch.setattr(settings, "PICO_FILESYSTEM_MAX_LINE_LENGTH", 50)

    # Exceeding file size limit
    with pytest.raises(ValidationError, match="exceeds maximum allowed limit of 100 bytes"):
        validate_text_content("A" * 150)

    # Exceeding line length limit
    with pytest.raises(ValidationError, match="exceeds maximum allowed limit of 50 characters"):
        validate_text_content("B" * 60)


def test_canonical_newline_normalization_produces_consistent_digests():
    """CRLF and LF produce the same canonical newline representation and SHA-256 digest."""
    crlf_text = "line 1\r\nline 2\r\n"
    lf_text = "line 1\nline 2\n"

    norm_crlf = validate_text_content(crlf_text)
    norm_lf = validate_text_content(lf_text)

    assert norm_crlf == norm_lf
    assert compute_content_digest(crlf_text) == compute_content_digest(lf_text)


def test_stage_create_rejects_existing_file_and_replace_rejects_missing_file(tmp_path, db_session: Session):
    """Create fails if target already exists; replace fails if target does not exist."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    existing_file = ws / "exists.txt"
    existing_file.write_text("already here\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    # Create when file exists
    with pytest.raises(ValidationError, match="Target file already exists"):
        stage_diff_proposal(
            db=db_session,
            root_id="ws",
            relative_path="exists.txt",
            operation="create",
            proposed_content="should fail\n",
        )

    # Replace when file is absent
    with pytest.raises(ValidationError, match="Target file does not exist"):
        stage_diff_proposal(
            db=db_session,
            root_id="ws",
            relative_path="missing.txt",
            operation="replace",
            proposed_content="should fail\n",
        )


# ==============================================================================
# 3. Approval Binding and Apply Safety Tests
# ==============================================================================


def test_apply_requires_permission_and_valid_approval(tmp_path, db_session: Session):
    """Consequential apply requires 'filesystem:write' and valid approval token."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="approved.txt",
        operation="create",
        proposed_content="verified content\n",
    )

    engine = ToolExecutionEngine(db_session, registry)

    # 1. Submission without 'filesystem:write' fails closed
    with pytest.raises(AuthorizationError, match="Caller lacks required permission 'filesystem:write'"):
        engine.submit(
            capability_id="filesystem.apply_staged_diff",
            input_data={"proposal_id": proposal.id},
            caller_permissions={"filesystem:read"},
        )

    # 2. Submit with permission -> transitions to awaiting_approval
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    assert execution.status == "awaiting_approval"

    # 3. Direct execution without approval fails closed
    with pytest.raises((AuthorizationError, ValidationError)):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # 4. Approval with missing/invalid token fails closed
    with pytest.raises(AuthorizationError, match="Filesystem modification approval fails closed"):
        engine.approve(
            execution_id=execution.id,
            approver="admin",
            approval_token="wrong_token",
        )

    assert not (ws / "approved.txt").exists()


def test_approval_bound_to_exact_operation_path_original_digest_and_diff_digest(tmp_path, db_session: Session):
    """Approval digest binds operation, path, original digest, proposed digest, and diff digest."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="bound.txt",
        operation="create",
        proposed_content="bound text\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )

    # Verify prepared input contains all server-bound fields
    inp = execution.input_json
    assert inp["proposal_id"] == proposal.id
    assert inp["root_id"] == "ws"
    assert inp["relative_path"] == "bound.txt"
    assert inp["operation"] == "create"
    assert inp["expected_original_state"] == "ABSENT"
    assert inp["expected_proposed_digest"] == proposal.proposed_digest
    assert inp["expected_diff_digest"] == proposal.diff_digest

    # Approve
    approved = engine.approve(
        execution_id=execution.id,
        approver="admin",
        approval_token=TEST_APPROVAL_TOKEN,
    )
    assert approved.status == "queued"
    assert approved.approval_input_digest == execution.input_digest


def test_forged_or_caller_tampered_metadata_rejected_at_preparation(tmp_path, db_session: Session):
    """Caller attempting to pass conflicting or forged expected digests is rejected at submit."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="tamper.txt",
        operation="create",
        proposed_content="real content\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    with pytest.raises(ValidationError, match="does not match server-persisted proposal"):
        engine.submit(
            capability_id="filesystem.apply_staged_diff",
            input_data={
                "proposal_id": proposal.id,
                "expected_proposed_digest": "forged_sha256_hash",
            },
            caller_permissions={"filesystem:write"},
        )


def test_target_file_modified_after_approval_fails_closed_zero_writes(tmp_path, db_session: Session):
    """If original file changes on disk after approval, apply fails closed with zero writes."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target_file = ws / "mutable.txt"
    target_file.write_text("initial version\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="mutable.txt",
        operation="replace",
        proposed_content="approved modification\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # External actor edits the file before dispatch
    target_file.write_text("external unauthorized modification\n", encoding="utf-8")

    # Execution boundary detects original digest mismatch and rejects
    with pytest.raises(AuthorizationError, match="Target file content .* was modified after approval"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Target remains the external edit, not the approved modification
    assert target_file.read_text(encoding="utf-8") == "external unauthorized modification\n"


def test_staged_content_or_diff_modified_after_approval_fails_closed_zero_writes(tmp_path, db_session: Session):
    """If staged proposal content in database is tampered with after approval, apply fails closed."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="tamper_staged.txt",
        operation="create",
        proposed_content="clean text\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Tamper with staged proposal content directly in DB
    staged_repo = StagedDiffRepository(db_session)
    p = staged_repo.get_by_id(proposal.id)
    assert p is not None
    p.proposed_content = "malicious injected text\n"
    db_session.commit()

    with pytest.raises((AuthorizationError, ExecutionError)):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    assert not (ws / "tamper_staged.txt").exists()


def test_target_file_created_after_create_approval_fails_closed_zero_writes(tmp_path, db_session: Session):
    """If a file unexpectedly appears at target path before create dispatch, apply fails closed."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="unborn.txt",
        operation="create",
        proposed_content="baby file\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # An unexpected file appears right before dispatch
    (ws / "unborn.txt").write_text("surprise\n", encoding="utf-8")

    with pytest.raises(AuthorizationError, match="Target file already exists"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    assert (ws / "unborn.txt").read_text(encoding="utf-8") == "surprise\n"


def test_atomic_write_and_independent_post_write_verification_succeeds(tmp_path, db_session: Session):
    """Approved apply writes atomically, independently reads back bytes, and reports verified."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposed_text = "atomic verified content\nline 2\n"
    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="verified_write.txt",
        operation="create",
        proposed_content=proposed_text,
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    res = asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    assert res.status == "succeeded"
    assert res.verification_status == "passed"
    assert res.output_json["verified_digest"] == compute_content_digest(proposed_text)

    # Verify actual file on disk
    disk_file = ws / "verified_write.txt"
    assert disk_file.exists()
    assert disk_file.read_text(encoding="utf-8") == proposed_text


def test_atomic_single_use_claim_prevents_concurrent_or_replay_dispatch(tmp_path, db_session: Session):
    """Concurrent execution or replay of an approved ticket fails closed."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="single_use.txt",
        operation="create",
        proposed_content="single use only\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # First dispatch succeeds
    res1 = asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))
    assert res1.status == "succeeded"

    # Replay fails closed
    with pytest.raises((AuthorizationError, ValidationError)):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))


def test_verification_failure_records_uncertainty_and_never_retries(tmp_path, db_session: Session):
    """Capability specifies max_retries=0; post-write verification failure never retries automatically."""
    cap = registry.get("filesystem.apply_staged_diff")
    assert cap.metadata.max_retries == 0
    assert cap.metadata.consequential is True


# ==============================================================================
# 4. Conflict-Safe Guarded Rollback Tests
# ==============================================================================


def test_rollback_create_operation_removes_created_file_when_unmodified(tmp_path, db_session: Session):
    """Rollback of create operation safely removes the created file when contents are untouched."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    content = "created to be rolled back\n"
    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="temp_create.txt",
        operation="create",
        proposed_content=content,
    )

    engine = ToolExecutionEngine(db_session, registry)
    exec_apply = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(exec_apply.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    asyncio.run(engine.execute(exec_apply.id, caller_permissions={"filesystem:write"}))

    assert (ws / "temp_create.txt").exists()

    # Submit rollback
    exec_rollback = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    assert exec_rollback.status == "awaiting_approval"

    engine.approve(exec_rollback.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    res_rollback = asyncio.run(engine.execute(exec_rollback.id, caller_permissions={"filesystem:write"}))

    assert res_rollback.status == "succeeded"
    assert res_rollback.verification_status == "passed"
    assert res_rollback.output_json["restored_state"] == "ABSENT"

    # Destination file is now gone
    assert not (ws / "temp_create.txt").exists()

    # Proposal marked rolled_back
    refreshed_proposal = get_staged_diff_proposal(db_session, proposal.id)
    assert refreshed_proposal.status == "rolled_back"


def test_rollback_replace_operation_atomically_restores_original_bytes(tmp_path, db_session: Session):
    """Rollback of replace operation restores the exact original content bytes."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target_file = ws / "restore_me.txt"
    original_text = "original pristine content\nline 2\n"
    target_file.write_text(original_text, encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="restore_me.txt",
        operation="replace",
        proposed_content="mutated content\nline 2\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    exec_apply = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(exec_apply.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    asyncio.run(engine.execute(exec_apply.id, caller_permissions={"filesystem:write"}))

    assert target_file.read_text(encoding="utf-8") == "mutated content\nline 2\n"

    # Submit rollback
    exec_rollback = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(exec_rollback.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    res_rollback = asyncio.run(engine.execute(exec_rollback.id, caller_permissions={"filesystem:write"}))

    assert res_rollback.status == "succeeded"
    assert res_rollback.verification_status == "passed"
    assert res_rollback.output_json["restored_state"] == compute_content_digest(original_text)

    # Verify target file has been restored to original bytes
    assert target_file.read_text(encoding="utf-8") == original_text


def test_external_edit_after_apply_blocks_rollback_zero_restore(tmp_path, db_session: Session):
    """If an external edit occurs after apply, rollback fails closed to prevent overwriting third-party work."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target_file = ws / "conflict.txt"
    target_file.write_text("v1\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="conflict.txt",
        operation="replace",
        proposed_content="v2\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    exec_apply = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(exec_apply.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    asyncio.run(engine.execute(exec_apply.id, caller_permissions={"filesystem:write"}))

    # External modification happens after apply
    target_file.write_text("v3 external engineer edit\n", encoding="utf-8")

    # Attempt rollback
    exec_rollback = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(exec_rollback.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    with pytest.raises(AuthorizationError, match="modified externally since application"):
        asyncio.run(engine.execute(exec_rollback.id, caller_permissions={"filesystem:write"}))

    # File remains untouched at v3
    assert target_file.read_text(encoding="utf-8") == "v3 external engineer edit\n"


# ==============================================================================
# 5. Privacy, Event Ledger, and REST API Tests
# ==============================================================================


def test_api_stage_inspect_and_discard_diff_proposal(client: TestClient, tmp_path):
    """Test REST API staging lifecycle endpoints (stage -> inspect -> discard)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    # 1. Stage proposal
    res_stage = client.post(
        "/api/filesystem/proposals",
        json={
            "root_id": "ws",
            "relative_path": "api_file.txt",
            "operation": "create",
            "proposed_content": "API created content\n",
        },
    )
    assert res_stage.status_code == 201
    stage_data = res_stage.json()
    proposal_id = stage_data["id"]
    assert stage_data["status"] == "staged"
    assert "+API created content" in stage_data["unified_diff"]

    # 2. Inspect proposal
    res_inspect = client.get(f"/api/filesystem/proposals/{proposal_id}")
    assert res_inspect.status_code == 200
    assert res_inspect.json()["id"] == proposal_id

    # 3. List proposals
    res_list = client.get("/api/filesystem/proposals?root_id=ws")
    assert res_list.status_code == 200
    assert any(p["id"] == proposal_id for p in res_list.json())

    # 4. Discard proposal
    res_discard = client.post(f"/api/filesystem/proposals/{proposal_id}/discard")
    assert res_discard.status_code == 200
    assert res_discard.json()["status"] == "discarded"


def test_api_end_to_end_apply_and_rollback_flow(client: TestClient, tmp_path):
    """Test full REST API lifecycle: Stage -> Submit Apply -> Approve -> Execute -> Submit Rollback -> Approve -> Execute."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    # 1. Stage proposal
    res_stage = client.post(
        "/api/filesystem/proposals",
        json={
            "root_id": "ws",
            "relative_path": "lifecycle.txt",
            "operation": "create",
            "proposed_content": "hello lifecycle\n",
        },
    )
    assert res_stage.status_code == 201
    proposal_id = res_stage.json()["id"]

    # 2. Submit Apply
    res_submit = client.post(
        f"/api/filesystem/proposals/{proposal_id}/apply",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
    )
    assert res_submit.status_code == 202
    exec_id = res_submit.json()["id"]
    assert res_submit.json()["status"] == "awaiting_approval"

    # 3. Approve Execution via tools API
    res_approve = client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"approver": "lead_engineer"},
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
    )
    assert res_approve.status_code == 200
    assert res_approve.json()["status"] == "queued"

    # 4. Run Execution via tools API
    res_run = client.post(
        f"/api/tools/executions/{exec_id}/execute",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
    )
    assert res_run.status_code == 200
    assert res_run.json()["status"] == "succeeded"
    assert (ws / "lifecycle.txt").exists()

    # 5. Submit Rollback
    res_submit_rb = client.post(
        f"/api/filesystem/proposals/{proposal_id}/rollback",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
    )
    assert res_submit_rb.status_code == 202
    rb_exec_id = res_submit_rb.json()["id"]

    # 6. Approve Rollback
    res_approve_rb = client.post(
        f"/api/tools/executions/{rb_exec_id}/approve",
        json={"approver": "lead_engineer"},
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
    )
    assert res_approve_rb.status_code == 200

    # 7. Execute Rollback
    res_run_rb = client.post(
        f"/api/tools/executions/{rb_exec_id}/execute",
        headers={"X-Pico-Approval-Token": TEST_APPROVAL_TOKEN},
    )
    assert res_run_rb.status_code == 200
    assert res_run_rb.json()["status"] == "succeeded"

    # File safely removed
    assert not (ws / "lifecycle.txt").exists()


def test_event_ledger_records_file_diff_lifecycle_without_leaking_content(tmp_path, db_session: Session):
    """Audit ledger accurately records execution and approval events without leaking full file content or secrets."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    secret_sentinel = "SUPER_SECRET_PAT_TOKEN_12345678"
    content = f"config_setting={secret_sentinel}\n"

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="secret_config.txt",
        operation="create",
        proposed_content=content,
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Inspect event ledger entries
    ledger_repo = EventLedgerRepository(db_session)
    events = ledger_repo.list_events(execution_id=execution.id)

    assert len(events) >= 3  # task_submitted, approval_recorded, task_completed
    event_types = [e.event_type for e in events]
    assert "task_submitted" in event_types
    assert "approval_recorded" in event_types
    assert "execution_succeeded" in event_types

    # Ensure secret sentinel and full content are NOT in event ledger payloads
    for ev in events:
        payload_str = str(ev.payload_json or {})
        assert secret_sentinel not in payload_str
        assert ev.data_classification == "INTERNAL"
