"""Consequential capability for conflict-safe guarded rollback of applied diffs."""

from typing import Any
from pydantic import BaseModel, Field

from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)
from pico.core.errors import ExecutionError, ValidationError
from pico.db.session import SessionLocal
from pico.filesystem.adapter import get_filesystem_adapter
from pico.filesystem.contracts import OperationType, compute_content_digest
from pico.filesystem.roots import get_roots_manager
from pico.repositories.staged_diff_repo import StagedDiffRepository


class RollbackStagedDiffInput(BaseModel):
    proposal_id: str = Field(..., description="Unique identifier of applied staged diff proposal")
    expected_applied_digest: str | None = Field(
        default=None, description="Server-bound digest of applied content to verify absence of external edits"
    )
    operation: str | None = Field(default=None, description="Server-bound original operation")
    target_restore_state: str | None = Field(
        default=None, description="Server-bound expected state after rollback ('ABSENT' or original sha256)"
    )


class RollbackStagedDiffOutput(BaseModel):
    proposal_id: str
    root_id: str
    relative_path: str
    operation: str
    restored_state: str
    status: str


class RollbackStagedDiffCapability(BaseCapability):
    """
    High-risk consequential capability for conflict-safe, guarded rollback of an applied diff.
    Verifies that the target file has not undergone external modifications since application.
    For file creations, safely removes the file. For replacements, atomically restores original bytes.
    Independently verifies the restored state and updates proposal status to 'rolled_back'.
    """

    metadata = CapabilityMetadata(
        capability_id="filesystem.rollback_staged_diff",
        version="1.0.0",
        name="Rollback Staged Filesystem Diff",
        description="Safely roll back an applied filesystem diff if no external edits have occurred.",
        risk_tier=RiskTier.HIGH,
        required_permission="filesystem:write",
        required_classification="INTERNAL",
        timeout_seconds=10.0,
        max_output_bytes=65536,
        idempotent=False,
        max_retries=0,
        consequential=True,
        preconditions=[
            "Proposal must be in 'applied' status",
            "Root must be active in server allowlist",
            "Target file must exactly match the applied digest (zero external edits)",
        ],
        expected_effects=[
            "Target file restored to original state or removed if created",
            "Restored state independently verified",
            "Proposal status transitions to 'rolled_back'",
        ],
    )

    input_model = RollbackStagedDiffInput
    output_model = RollbackStagedDiffOutput

    def prepare_input(self, input_data: dict[str, Any], db: Any = None) -> dict[str, Any]:
        """
        Server-side preparation and cryptographic binding for rollback.
        Ensures approval covers exact proposal ID, root, path, applied digest, and restore state.
        """
        proposal_id = input_data.get("proposal_id")
        if not proposal_id or not str(proposal_id).strip():
            raise ValidationError("Field 'proposal_id' is required.")

        pid = str(proposal_id).strip()
        session_to_close = None
        session = db
        if session is None:
            session_to_close = SessionLocal()
            session = session_to_close

        try:
            repo = StagedDiffRepository(session)
            proposal = repo.get_by_id(pid)
            if not proposal:
                raise ValidationError(f"Staged proposal '{pid}' not found.")
            if proposal.status != "applied":
                raise ValidationError(
                    f"Staged proposal '{pid}' is in status '{proposal.status}'. Only 'applied' proposals can be rolled back."
                )

            # Re-verify root allowlist includes proposal root
            roots_mgr = get_roots_manager()
            roots_mgr.get_root_path(proposal.root_id)

            prepared = dict(input_data)
            server_bindings = {
                "expected_applied_digest": proposal.proposed_digest,
                "operation": proposal.operation,
                "target_restore_state": proposal.original_state,
            }

            for key, expected_val in server_bindings.items():
                caller_val = prepared.get(key)
                if caller_val is not None and caller_val != expected_val:
                    raise ValidationError(
                        f"Caller-supplied parameter '{key}' ({caller_val}) does not match server-persisted proposal ({expected_val})."
                    )
                prepared[key] = expected_val

            return prepared
        finally:
            if session_to_close is not None:
                session_to_close.close()

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        validated = self.input_model.model_validate(input_data)
        pid = validated.proposal_id

        db = context.db
        session_to_close = None
        if db is None:
            session_to_close = SessionLocal()
            db = session_to_close

        try:
            repo = StagedDiffRepository(db)
            proposal = repo.get_by_id(pid)
            if not proposal:
                raise ExecutionError(f"Staged proposal '{pid}' not found.")
            if proposal.status != "applied":
                raise ExecutionError(
                    f"Proposal '{pid}' is in status '{proposal.status}', expected 'applied'."
                )

            # Re-evaluate root allowlist and resolve safe path
            roots_mgr = get_roots_manager()
            canonical_root, target_path = roots_mgr.resolve_safe_path(
                root_id=proposal.root_id,
                relative_path=proposal.relative_path,
                must_exist=True,
                parent_must_exist=True,
            )

            adapter = get_filesystem_adapter()

            # Target must exist and match applied digest exactly (guard against external edits)
            if not adapter.file_exists(target_path, root_path=canonical_root):
                raise ExecutionError(
                    f"Rollback rejected: Target file does not exist at '{proposal.relative_path}'."
                )

            current_digest = adapter.get_content_digest(target_path, root_path=canonical_root)
            if current_digest != validated.expected_applied_digest:
                raise ExecutionError(
                    f"Rollback rejected: File content has been modified externally since application. Current digest: '{current_digest}', expected: '{validated.expected_applied_digest}'."
                )

            if proposal.operation == OperationType.CREATE.value:
                # Safely delete the created file
                adapter.delete_file(
                    target_path,
                    root_path=canonical_root,
                    expected_digest=validated.expected_applied_digest,
                )
                if adapter.file_exists(target_path, root_path=canonical_root):
                    raise ExecutionError(
                        f"Rollback verification failed: File still exists at '{proposal.relative_path}' after deletion."
                    )
                restored_state = "ABSENT"
            else:
                # replace operation: restore original content
                if proposal.original_content is None:
                    raise ExecutionError("Rollback failed: Original content snapshot is missing.")
                adapter.write_text_atomic(
                    target_path,
                    proposal.original_content,
                    root_path=canonical_root,
                    exclusive=False,
                    expected_original_digest=validated.expected_applied_digest,
                )

                # Independent verification: re-read file and verify original digest
                restored_text = adapter.read_text(target_path, root_path=canonical_root)
                restored_digest = compute_content_digest(restored_text)
                if restored_digest != proposal.original_state:
                    raise ExecutionError(
                        f"Rollback verification failed: Restored file digest '{restored_digest}' does not match original digest '{proposal.original_state}'."
                    )
                restored_state = restored_digest

            repo.mark_rolled_back(pid)

            output = RollbackStagedDiffOutput(
                proposal_id=proposal.id,
                root_id=proposal.root_id,
                relative_path=proposal.relative_path,
                operation=proposal.operation,
                restored_state=restored_state,
                status="rolled_back",
            )
            return ExecutionResult(
                success=True,
                output_data=output.model_dump(),
                evidence={
                    "proposal_id": proposal.id,
                    "restored_state": restored_state,
                    "target_path": proposal.relative_path,
                },
            )
        finally:
            if session_to_close is not None:
                session_to_close.close()

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        try:
            out = self.output_model.model_validate(output_data)
            expected_restore = input_data.get("target_restore_state")
            if out.restored_state != expected_restore:
                return VerificationResult(
                    passed=False,
                    reason=f"Restored state '{out.restored_state}' does not match expected restore state '{expected_restore}'.",
                )
            if out.status != "rolled_back":
                return VerificationResult(
                    passed=False,
                    reason=f"Status '{out.status}' is not 'rolled_back'.",
                )
            return VerificationResult(passed=True)
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Independent verifier failed: {str(exc)}",
                details={"error": str(exc)},
            )
