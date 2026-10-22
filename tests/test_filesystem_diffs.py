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
        with pytest.raises(ValidationError, match="Symlink or reparse point detected|Path escapes"):
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


# ==============================================================================
# 6. P8.2 Security Follow-up: Fail-Closed Permissions & Path Redirection Protection
# ==============================================================================


def test_adversarial_unset_secret_fails_closed_zero_writes(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path):
    """When APPROVAL_SECRET_TOKEN is unset or empty, apply and rollback endpoints fail closed with zero mutations."""
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", None)
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    res_stage = client.post(
        "/api/filesystem/proposals",
        json={
            "root_id": "ws",
            "relative_path": "unauthed.txt",
            "operation": "create",
            "proposed_content": "danger data\n",
        },
    )
    assert res_stage.status_code == 201
    proposal_id = res_stage.json()["id"]

    # 1. Apply without secret configured fails closed (403 Forbidden)
    res_apply = client.post(
        f"/api/filesystem/proposals/{proposal_id}/apply",
        headers={"X-Pico-Approval-Token": "some_token"},
    )
    assert res_apply.status_code == 403
    assert "filesystem:write" in res_apply.json().get("detail", "")
    assert not (ws / "unauthed.txt").exists()

    # 2. Even with empty header
    res_apply_no_header = client.post(f"/api/filesystem/proposals/{proposal_id}/apply")
    assert res_apply_no_header.status_code == 403
    assert not (ws / "unauthed.txt").exists()


def test_adversarial_missing_invalid_mismatched_approval_tokens(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Missing, invalid, or mismatched approval tokens produce zero writes and zero deletes."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    res_stage = client.post(
        "/api/filesystem/proposals",
        json={
            "root_id": "ws",
            "relative_path": "token_check.txt",
            "operation": "create",
            "proposed_content": "token check content\n",
        },
    )
    assert res_stage.status_code == 201
    proposal_id = res_stage.json()["id"]

    # Missing token header
    res_no_tok = client.post(f"/api/filesystem/proposals/{proposal_id}/apply")
    assert res_no_tok.status_code == 403
    assert not (ws / "token_check.txt").exists()

    # Empty token header
    res_empty_tok = client.post(
        f"/api/filesystem/proposals/{proposal_id}/apply",
        headers={"X-Pico-Approval-Token": ""},
    )
    assert res_empty_tok.status_code == 403
    assert not (ws / "token_check.txt").exists()

    # Mismatched token header
    res_wrong_tok = client.post(
        f"/api/filesystem/proposals/{proposal_id}/apply",
        headers={"X-Pico-Approval-Token": "invalid_wrong_token_999"},
    )
    assert res_wrong_tok.status_code == 403
    assert not (ws / "token_check.txt").exists()


def test_adversarial_target_swapped_with_symlink_after_validation(tmp_path, db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """If target path is swapped with a symlink after proposal/validation, mutation fails closed and outside file is untouched."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_secret = outside / "secret.txt"
    outside_secret.write_text("pristine external content\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    target = ws / "target.txt"
    target.write_text("initial content\n", encoding="utf-8")

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="target.txt",
        operation="replace",
        proposed_content="overwritten content\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Now replace target.txt with a symlink pointing to outside_secret
    target.unlink()
    try:
        os.symlink(str(outside_secret), str(target))
        symlink_created = True
    except (OSError, NotImplementedError):
        symlink_created = False

    if not symlink_created:
        # On Windows without symlink privileges, test via controlled adapter mock of is_symlink_or_reparse_point
        from pico.filesystem import adapter as adapter_mod
        from pico.filesystem import roots as roots_mod
        orig_check = adapter_mod.is_symlink_or_reparse_point
        target.write_text("initial content\n", encoding="utf-8")
        monkeypatch.setattr(
            adapter_mod,
            "is_symlink_or_reparse_point",
            lambda p: True if os.path.normpath(p) == os.path.normpath(str(target)) else orig_check(p),
        )
        monkeypatch.setattr(
            roots_mod,
            "is_symlink_or_reparse_point",
            lambda p: True if os.path.normpath(p) == os.path.normpath(str(target)) else orig_check(p),
        )

    with pytest.raises((ValidationError, ExecutionError, AuthorizationError)):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Crucial assertion: outside file MUST remain unchanged
    assert outside_secret.read_text(encoding="utf-8") == "pristine external content\n"


def test_adversarial_parent_directory_swapped_with_junction_fails_closed(tmp_path, db_session: Session):
    """Parent directory swapped with a junction/reparse point after validation fails closed with zero mutations."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    real_sub = ws / "subdir"
    real_sub.mkdir()
    target = real_sub / "target.txt"
    target.write_text("initial text\n", encoding="utf-8")

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "target.txt"
    outside_file.write_text("outside protected\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="subdir/target.txt",
        operation="replace",
        proposed_content="malicious overwrite\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Swap real_sub with a directory junction pointing to outside_dir
    import shutil
    import sys
    shutil.rmtree(real_sub)

    junction_created = False
    if sys.platform == "win32":
        try:
            import _winapi
            _winapi.CreateJunction(str(outside_dir), str(real_sub))
            junction_created = True
        except Exception:
            junction_created = False

    if junction_created:
        with pytest.raises((ValidationError, ExecutionError, AuthorizationError)):
            asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

        # Outside directory must not have been modified
        assert outside_file.read_text(encoding="utf-8") == "outside protected\n"


def test_adversarial_concurrent_target_creation_blocks_exclusive_apply(tmp_path, db_session: Session):
    """Target created concurrently immediately before or during mutation is blocked by exclusive creation."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="concurrent.txt",
        operation="create",
        proposed_content="pico proposal\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Concurrently create the target file before dispatch
    concurrent_file = ws / "concurrent.txt"
    concurrent_file.write_text("competitor content\n", encoding="utf-8")

    with pytest.raises((AuthorizationError, ExecutionError)):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Ensure concurrent content was NOT overwritten
    assert concurrent_file.read_text(encoding="utf-8") == "competitor content\n"


def test_adversarial_creation_rollback_fails_closed_when_content_or_path_modified(tmp_path, db_session: Session):
    """Rollback of create operation fails closed if file was modified or redirected, refusing to delete."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="rollback_guard.txt",
        operation="create",
        proposed_content="applied content\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    target = ws / "rollback_guard.txt"
    assert target.exists()

    # Modify content externally
    target.write_text("user added important work here\n", encoding="utf-8")

    # Attempt rollback
    rb_exec = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(rb_exec.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    with pytest.raises((AuthorizationError, ExecutionError)):
        asyncio.run(engine.execute(rb_exec.id, caller_permissions={"filesystem:write"}))

    # File must NOT have been deleted
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "user added important work here\n"


def test_adversarial_denied_or_uncertain_operation_never_retried(tmp_path, db_session: Session):
    """A failed or denied filesystem execution leaves retry_count at 0 and never schedules automated retries."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="no_retry.txt",
        operation="create",
        proposed_content="content\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    # Execute directly without approval -> fails
    with pytest.raises((AuthorizationError, ValidationError)):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    repo = ToolExecutionRepository(db_session)
    reloaded = repo.get_by_id(execution.id)
    assert reloaded.retry_count == 0
    assert reloaded.status in ("failed", "awaiting_approval")


def test_adversarial_repeated_and_concurrent_execution_claims_atomic(tmp_path, db_session: Session):
    """Repeated or concurrent execution requests on the same execution respect atomic single-use claims."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="concurrent_claim.txt",
        operation="create",
        proposed_content="concurrent content\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # First execution succeeds
    asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Immediate second execution claim fails because status is 'succeeded', not 'queued'
    with pytest.raises(ValidationError, match="is in status 'succeeded', cannot execute"):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))


def test_adversarial_no_changes_outside_authorized_root(client: TestClient, tmp_path):
    """Verify that any attempt to stage or apply outside the root is rejected with zero mutations outside."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside_protected"
    outside.mkdir()
    outside_file = outside / "safe.txt"
    outside_file.write_text("protected content\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    # Attempt to stage outside relative path
    res = client.post(
        "/api/filesystem/proposals",
        json={
            "root_id": "ws",
            "relative_path": "../outside_protected/safe.txt",
            "operation": "replace",
            "proposed_content": "hacked\n",
        },
    )
    assert res.status_code in (400, 422)
    assert outside_file.read_text(encoding="utf-8") == "protected content\n"


def test_adversarial_parent_directory_junction_swap_before_creation(tmp_path, db_session: Session):
    """A parent directory replaced by a junction immediately before creation fails closed with zero outside writes."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    real_sub = ws / "subdir"
    real_sub.mkdir()

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_target = outside_dir / "created.txt"

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="subdir/created.txt",
        operation="create",
        proposed_content="malicious creation payload\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Swap real_sub with a directory junction pointing to outside_dir
    import shutil
    import sys
    shutil.rmtree(real_sub)

    junction_created = False
    if sys.platform == "win32":
        try:
            import _winapi
            _winapi.CreateJunction(str(outside_dir), str(real_sub))
            junction_created = True
        except Exception:
            junction_created = False

    if junction_created:
        with pytest.raises((ValidationError, ExecutionError, AuthorizationError)):
            asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

        # Outside directory must have NO new file created
        assert not outside_target.exists()


def test_adversarial_parent_directory_junction_swap_before_replacement(tmp_path, db_session: Session):
    """A parent directory replaced immediately before replacement fails closed with zero outside mutations."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    real_sub = ws / "subdir"
    real_sub.mkdir()
    target = real_sub / "target.txt"
    target.write_text("initial text\n", encoding="utf-8")

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "target.txt"
    outside_file.write_text("outside protected content\n", encoding="utf-8")

    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="subdir/target.txt",
        operation="replace",
        proposed_content="malicious overwrite\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Swap real_sub with a directory junction pointing to outside_dir
    import shutil
    import sys
    shutil.rmtree(real_sub)

    junction_created = False
    if sys.platform == "win32":
        try:
            import _winapi
            _winapi.CreateJunction(str(outside_dir), str(real_sub))
            junction_created = True
        except Exception:
            junction_created = False

    if junction_created:
        with pytest.raises((ValidationError, ExecutionError, AuthorizationError)):
            asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

        # Outside file must remain byte-for-byte untouched
        assert outside_file.read_text(encoding="utf-8") == "outside protected content\n"


def test_adversarial_target_substituted_between_digest_validation_and_mutation(tmp_path, db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """A target substituted or modified between expected-digest validation and mutation fails closed."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    target = ws / "substitute.txt"
    target.write_text("legitimate content\n", encoding="utf-8")

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="substitute.txt",
        operation="replace",
        proposed_content="approved replacement\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Tamper with the target immediately before execution
    target.write_text("concurrent attacker tamper\n", encoding="utf-8")

    with pytest.raises((ExecutionError, AuthorizationError)):
        asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # The file should still contain the untampered state or failed state, NOT approved replacement
    assert target.read_text(encoding="utf-8") == "concurrent attacker tamper\n"


def test_adversarial_parent_substituted_before_rollback_deletion(tmp_path, db_session: Session):
    """A parent directory substituted before rollback deletion refuses to delete outside target."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    real_sub = ws / "sub_rollback"
    real_sub.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="sub_rollback/rollback_target.txt",
        operation="create",
        proposed_content="to be rolled back\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    created_file = real_sub / "rollback_target.txt"
    assert created_file.exists()

    # Outside victim directory with same relative filename
    outside_dir = tmp_path / "outside_rollback"
    outside_dir.mkdir()
    outside_victim = outside_dir / "rollback_target.txt"
    outside_victim.write_text("critical outside victim\n", encoding="utf-8")

    # Submit and approve rollback
    rb_exec = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(rb_exec.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    # Now substitute real_sub with junction to outside_dir before rollback executes
    import shutil
    import sys
    shutil.rmtree(real_sub)

    junction_created = False
    if sys.platform == "win32":
        try:
            import _winapi
            _winapi.CreateJunction(str(outside_dir), str(real_sub))
            junction_created = True
        except Exception:
            junction_created = False

    if junction_created:
        with pytest.raises((ValidationError, ExecutionError, AuthorizationError)):
            asyncio.run(engine.execute(rb_exec.id, caller_permissions={"filesystem:write"}))

        # Outside victim must remain intact and NOT deleted!
        assert outside_victim.exists()
        assert outside_victim.read_text(encoding="utf-8") == "critical outside victim\n"


def test_adversarial_inspection_error_fails_closed(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """An inspection error (PermissionError / unknown OSError) fails closed with ValidationError."""
    from pico.filesystem.contracts import is_symlink_or_reparse_point
    from pico.filesystem.adapter import verify_boundary_and_reparse

    ws = tmp_path / "workspace"
    ws.mkdir()
    target = ws / "test.txt"

    # Non-existent file genuinely returns False
    assert is_symlink_or_reparse_point(str(target)) is False

    # Simulate PermissionError during inspection
    def mock_lstat_permission_denied(path):
        raise PermissionError(13, "Permission denied during adversarial inspection test")

    monkeypatch.setattr(os, "lstat", mock_lstat_permission_denied)

    # is_symlink_or_reparse_point must fail closed by raising ValidationError, NEVER returning False
    with pytest.raises(ValidationError, match="Filesystem inspection failed"):
        is_symlink_or_reparse_point(str(target))

    # verify_boundary_and_reparse must also fail closed
    with pytest.raises(ValidationError, match="Filesystem inspection failed"):
        verify_boundary_and_reparse(str(target), root_path=str(ws))


def test_adversarial_cleanup_when_path_redirected(tmp_path):
    """Cleanup after a failed operation when the original path has been redirected refuses to delete outside target."""
    from pico.filesystem.adapter import safe_unlink

    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside_clean"
    outside.mkdir()
    outside_victim = outside / "victim.txt"
    outside_victim.write_text("protected outside victim\n", encoding="utf-8")

    # 1. Attempt to safe_unlink a file outside root: ignored and untouched
    safe_unlink(str(outside_victim), root_path=str(ws))
    assert outside_victim.exists()
    assert outside_victim.read_text(encoding="utf-8") == "protected outside victim\n"

    # 2. If path is a junction inside workspace pointing outside, safe_unlink refuses to follow or delete outside
    import sys
    if sys.platform == "win32":
        import _winapi
        junc = ws / "junc_link"
        try:
            _winapi.CreateJunction(str(outside), str(junc))
            junc_created = True
        except Exception:
            junc_created = False

        if junc_created:
            safe_unlink(str(junc / "victim.txt"), root_path=str(ws))
            assert outside_victim.exists()
            assert outside_victim.read_text(encoding="utf-8") == "protected outside victim\n"


def test_adversarial_pinned_directory_blocks_external_rename_or_deletion(tmp_path):
    """Windows kernel blocks external process attempts to delete or rename pinned directories with ERROR_SHARING_VIOLATION."""
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows-specific NT kernel sharing violation test")

    from pico.filesystem.adapter import pin_directory_hierarchy

    ws = tmp_path / "workspace"
    ws.mkdir()
    sub1 = ws / "level1"
    sub1.mkdir()
    sub2 = sub1 / "level2"
    sub2.mkdir()

    with pin_directory_hierarchy(str(sub2), str(ws)) as handles:
        assert len(handles) == 3

        # Attempt to delete sub2 externally while pinned -> WinError 32
        with pytest.raises(OSError) as exc_info:
            os.rmdir(str(sub2))
        assert exc_info.value.winerror == 32 or "used by another process" in str(exc_info.value)

        # Attempt to rename sub1 externally while pinned -> WinError 32
        with pytest.raises(OSError) as exc_info2:
            os.rename(str(sub1), str(ws / "level1_renamed"))
        assert exc_info2.value.winerror == 32 or "used by another process" in str(exc_info2.value)


def test_adversarial_competing_target_substitution_before_replacement_prevents_overwrite(tmp_path, db_session: Session):
    """
    Adversarial test reproducing target substitution race:
    Injects a competing target change after expected-original-digest validation and handle relocation,
    immediately prior to MoveFileW.
    Asserts:
    1. Zero overwrite: PICO rejects replacing the substituted competitor file.
    2. Competitor file content is 100% preserved.
    3. Displaced original file is preserved in .pico_backup_* with its original content.
    4. Execution fails closed with ExecutionError.
    5. Proposal remains in 'staged' status (not 'applied').
    6. Audit ledger truthfully records execution_failed with accurate details.
    """
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows-specific NT kernel handle relocation and MoveFileW test")

    from pathlib import Path

    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    target_file = ws / "critical_config.ini"
    target_file.write_text("setting=original_approved_value\n", encoding="utf-8")

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="critical_config.ini",
        operation="replace",
        proposed_content="setting=pico_replacement_value\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    adapter = get_filesystem_adapter()
    assert isinstance(adapter, WindowsFilesystemAdapter)

    # Injected attack hook: immediately after target handle is relocated to backup,
    # an adversarial competing process substitutes different content at the original target path.
    competitor_injected_content = "COMPETITOR_SUBSTITUTED_CONTENT\n"
    def attack_hook(norm_path: str):
        Path(norm_path).write_text(competitor_injected_content, encoding="utf-8")

    adapter._on_pre_replace_move = attack_hook

    result_exec = asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Assert execution failed closed
    assert result_exec.status == "failed"
    assert "Target file was concurrently created or substituted prior to replacement" in (result_exec.error_message or "")
    assert "Displaced original preserved in" in (result_exec.error_message or "")

    # Assert competitor substituted file was NOT overwritten
    assert target_file.read_text(encoding="utf-8") == competitor_injected_content

    # Assert displaced original file is preserved and accounted for
    backup_files = list(ws.glob(".pico_backup_*"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text(encoding="utf-8") == "setting=original_approved_value\n"

    # Assert proposal was NOT marked applied
    staged_repo = StagedDiffRepository(db_session)
    refreshed_proposal = staged_repo.get_by_id(proposal.id)
    assert refreshed_proposal is not None
    assert refreshed_proposal.status == "staged"

    # Assert audit ledger truthfully records execution failure
    ledger_repo = EventLedgerRepository(db_session)
    events = ledger_repo.list_events(execution_id=execution.id)
    fail_events = [e for e in events if e.event_type == "execution_failed"]
    assert len(fail_events) == 1
    assert "Target file was concurrently created or substituted prior to replacement" in fail_events[0].payload_json.get("error", "")


def test_adversarial_replacement_rollback_competing_substitution_fails_closed(tmp_path, db_session: Session):
    """
    Adversarial test for rollback of replacement:
    If a competing target change occurs after expected-applied-digest validation during rollback,
    MoveFileW fails closed, competitor content is preserved, displaced applied file is preserved in backup,
    and proposal status remains 'applied'.
    """
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows-specific NT kernel handle relocation and MoveFileW test")

    from pathlib import Path

    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    target_file = ws / "restore_target.txt"
    target_file.write_text("original_alpha\n", encoding="utf-8")

    # 1. Stage and cleanly apply initial replacement
    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="restore_target.txt",
        operation="replace",
        proposed_content="applied_beta\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    apply_exec = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(apply_exec.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    apply_res = asyncio.run(engine.execute(apply_exec.id, caller_permissions={"filesystem:write"}))
    assert apply_res.status == "succeeded"
    assert target_file.read_text(encoding="utf-8") == "applied_beta\n"

    # 2. Stage and approve rollback
    rollback_exec = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(rollback_exec.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    adapter = get_filesystem_adapter()
    assert isinstance(adapter, WindowsFilesystemAdapter)

    # Injected attack hook during rollback replacement
    competitor_rollback_content = "COMPETITOR_INJECTED_DURING_ROLLBACK\n"
    def rollback_attack(norm_path: str):
        Path(norm_path).write_text(competitor_rollback_content, encoding="utf-8")

    adapter._on_pre_replace_move = rollback_attack

    rollback_res = asyncio.run(engine.execute(rollback_exec.id, caller_permissions={"filesystem:write"}))

    assert rollback_res.status == "failed"
    assert "Target file was concurrently created or substituted prior to replacement" in (rollback_res.error_message or "")

    # Zero overwrite of competitor content
    assert target_file.read_text(encoding="utf-8") == competitor_rollback_content

    # Displaced applied file is preserved in backup
    backup_files = list(ws.glob(".pico_backup_*"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text(encoding="utf-8") == "applied_beta\n"

    # Proposal remains 'applied' (not 'rolled_back')
    staged_repo = StagedDiffRepository(db_session)
    refreshed_p = staged_repo.get_by_id(proposal.id)
    assert refreshed_p is not None
    assert refreshed_p.status == "applied"


def test_adversarial_non_windows_mutations_fail_closed(tmp_path, monkeypatch):
    """
    On non-Windows platforms, directory hierarchy pinning and handle sharing semantics
    cannot be established by the kernel. WindowsFilesystemAdapter explicitly fails closed
    for exclusive creation, replacement, and deletion mutations rather than silently degrading.
    """
    import sys
    monkeypatch.setattr(sys, "platform", "linux")

    ws = tmp_path / "workspace"
    ws.mkdir()
    target = ws / "test.txt"
    target.write_text("initial\n", encoding="utf-8")

    adapter = WindowsFilesystemAdapter()

    # 1. Replacement fails closed
    with pytest.raises(ExecutionError, match="Atomic target-bound file replacement requires Windows NT kernel"):
        adapter.write_text_atomic(
            str(target),
            "new_content\n",
            root_path=str(ws),
            exclusive=False,
            expected_original_digest=compute_content_digest("initial\n"),
        )

    # 2. Exclusive creation fails closed
    with pytest.raises(ExecutionError, match="Handle-bound atomic filesystem mutations require Windows NT kernel"):
        adapter.write_text_atomic(
            str(ws / "new_file.txt"),
            "created\n",
            root_path=str(ws),
            exclusive=True,
        )

    # 3. Deletion fails closed
    with pytest.raises(ExecutionError, match="Handle-bound atomic file deletion requires Windows NT kernel"):
        adapter.delete_file(str(target), root_path=str(ws))


def test_adversarial_temp_file_substitution_attempt_blocked_by_handle_identity(tmp_path, db_session: Session):
    """
    Finding 1: An external process attempts to tamper with or replace the .pico_tmp_* file
    between content staging and the final move.
    Because h_temp is held open with exclusive sharing (no FILE_SHARE_WRITE, no FILE_SHARE_DELETE),
    external tampering/deletion fails with PermissionError (sharing violation), and the move
    remains strictly bound to the approved temporary file object written by PICO.
    """
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows NT kernel handle sharing test")

    from pathlib import Path
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    target_file = ws / "config.txt"
    target_file.write_text("setting=alpha\n", encoding="utf-8")

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="config.txt",
        operation="replace",
        proposed_content="setting=beta_approved\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    adapter = get_filesystem_adapter()
    assert isinstance(adapter, WindowsFilesystemAdapter)

    tamper_attempt_recorded = []

    def attack_temp_hook(temp_path: str, norm_path: str):
        # Adversary attempts to delete or overwrite the temporary file while PICO holds h_temp
        try:
            os.remove(temp_path)
            tamper_attempt_recorded.append("unlocked_delete_succeeded")
        except PermissionError:
            tamper_attempt_recorded.append("delete_blocked_by_kernel")

        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write("MALICIOUS_SUBSTITUTE\n")
            tamper_attempt_recorded.append("unlocked_write_succeeded")
        except PermissionError:
            tamper_attempt_recorded.append("write_blocked_by_kernel")

    adapter._on_pre_temp_rename = attack_temp_hook

    result_exec = asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Assert sharing protections prevented any external tampering with temp file
    assert "delete_blocked_by_kernel" in tamper_attempt_recorded
    assert "write_blocked_by_kernel" in tamper_attempt_recorded
    assert "unlocked_delete_succeeded" not in tamper_attempt_recorded
    assert "unlocked_write_succeeded" not in tamper_attempt_recorded

    # Assert execution succeeded with approved content
    assert result_exec.status == "succeeded"
    assert target_file.read_text(encoding="utf-8") == "setting=beta_approved\n"

    # Assert clean state: temp file and backup file cleaned up
    assert len(list(ws.glob(".pico_tmp_*"))) == 0
    assert len(list(ws.glob(".pico_backup_*"))) == 0


def test_adversarial_post_move_verification_failure_preserves_backup_and_remains_staged(tmp_path, db_session: Session):
    """
    Finding 2: If destination verification fails after the handle rename (e.g. content mismatch),
    PICO fails closed, PRESERVES the displaced original backup file, records the backup location in the error,
    and leaves the proposal in 'staged' status with zero automatic retries.
    """
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows NT kernel handle test")

    from pathlib import Path
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    target_file = ws / "important.txt"
    target_file.write_text("critical_original_data\n", encoding="utf-8")

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="important.txt",
        operation="replace",
        proposed_content="new_data_value\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(execution.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    adapter = get_filesystem_adapter()
    assert isinstance(adapter, WindowsFilesystemAdapter)

    # Injected adversarial hook: after move succeeds but BEFORE post-write verification,
    # corrupt the destination file content so verification digest check fails.
    def corrupt_post_move(norm_path: str):
        Path(norm_path).write_text("CORRUPTED_AFTER_MOVE\n", encoding="utf-8")

    adapter._on_post_replace_move = corrupt_post_move

    result_exec = asyncio.run(engine.execute(execution.id, caller_permissions={"filesystem:write"}))

    # Assert execution failed closed
    assert result_exec.status == "failed"
    assert "Post-write verification failed" in (result_exec.error_message or "")
    assert "Displaced original preserved in" in (result_exec.error_message or "")

    # Crucial: verify backup was NOT destroyed and contains original data
    backup_files = list(ws.glob(".pico_backup_*"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text(encoding="utf-8") == "critical_original_data\n"

    # Crucial: proposal was NOT marked applied
    staged_repo = StagedDiffRepository(db_session)
    refreshed = staged_repo.get_by_id(proposal.id)
    assert refreshed is not None
    assert refreshed.status == "staged"


def test_adversarial_rollback_post_move_verification_failure_preserves_backup_and_remains_applied(tmp_path, db_session: Session):
    """
    Finding 2 (Rollback): If post-move verification fails during a rollback operation,
    the displaced file is preserved in backup, and the proposal status remains 'applied'.
    """
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows NT kernel handle test")

    from pathlib import Path
    ws = tmp_path / "workspace"
    ws.mkdir()
    set_roots_manager(FilesystemRootsManager({"ws": str(ws)}))

    target_file = ws / "rollback_target.txt"
    target_file.write_text("initial_original\n", encoding="utf-8")

    proposal = stage_diff_proposal(
        db=db_session,
        root_id="ws",
        relative_path="rollback_target.txt",
        operation="replace",
        proposed_content="applied_state\n",
    )

    engine = ToolExecutionEngine(db_session, registry)
    apply_exec = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(apply_exec.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)
    res_apply = asyncio.run(engine.execute(apply_exec.id, caller_permissions={"filesystem:write"}))
    assert res_apply.status == "succeeded"

    # Stage and approve rollback
    rollback_exec = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal.id},
        caller_permissions={"filesystem:write"},
    )
    engine.approve(rollback_exec.id, approver="admin", approval_token=TEST_APPROVAL_TOKEN)

    adapter = get_filesystem_adapter()
    assert isinstance(adapter, WindowsFilesystemAdapter)

    # Injected corrupt hook during rollback post-move verification
    def corrupt_rollback_post_move(norm_path: str):
        Path(norm_path).write_text("CORRUPTED_DURING_ROLLBACK\n", encoding="utf-8")

    adapter._on_post_replace_move = corrupt_rollback_post_move

    res_rollback = asyncio.run(engine.execute(rollback_exec.id, caller_permissions={"filesystem:write"}))

    assert res_rollback.status == "failed"
    assert "Post-write verification failed" in (res_rollback.error_message or "")
    assert "Displaced original preserved in" in (res_rollback.error_message or "")

    # Assert backup file is preserved
    backup_files = list(ws.glob(".pico_backup_*"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text(encoding="utf-8") == "applied_state\n"

    # Assert proposal was NOT marked rolled_back, remains 'applied'
    staged_repo = StagedDiffRepository(db_session)
    refreshed = staged_repo.get_by_id(proposal.id)
    assert refreshed is not None
    assert refreshed.status == "applied"


def test_adversarial_cleanup_does_not_delete_unrelated_files(tmp_path):
    """
    Ensure safe_unlink and adapter cleanup mechanisms strictly avoid deleting
    unrelated files or directories, and refuse to delete reparse points.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    unrelated_file = ws / "unrelated_user_file.txt"
    unrelated_file.write_text("DO_NOT_DELETE\n", encoding="utf-8")

    from pico.filesystem.adapter import safe_unlink
    # safe_unlink outside root does nothing
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("OUTSIDE\n", encoding="utf-8")
    safe_unlink(str(outside_file), str(ws))
    assert outside_file.exists()

    # safe_unlink on root itself does nothing
    safe_unlink(str(ws), str(ws))
    assert ws.exists()

    # Unrelated user file is untouched
    assert unrelated_file.exists()
    assert unrelated_file.read_text(encoding="utf-8") == "DO_NOT_DELETE\n"
