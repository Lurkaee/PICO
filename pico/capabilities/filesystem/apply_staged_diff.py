"""Consequential capability to apply an approved staged filesystem diff."""

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
from pico.filesystem.contracts import (
    OperationType,
    compute_content_digest,
    compute_diff_digest,
)
from pico.filesystem.roots import get_roots_manager
from pico.repositories.staged_diff_repo import StagedDiffRepository


class ApplyStagedDiffInput(BaseModel):
    proposal_id: str = Field(..., description="Unique identifier of staged diff proposal")
    root_id: str | None = Field(default=None, description="Server-bound root identifier")
    relative_path: str | None = Field(default=None, description="Server-bound relative path")
    operation: str | None = Field(default=None, description="Server-bound operation: 'create' or 'replace'")
    expected_original_state: str | None = Field(
        default=None, description="Server-bound expected original digest or 'ABSENT'"
    )
    expected_proposed_digest: str | None = Field(
        default=None, description="Server-bound proposed content digest"
    )
    expected_diff_digest: str | None = Field(
        default=None, description="Server-bound unified diff digest"
    )


class ApplyStagedDiffOutput(BaseModel):
    proposal_id: str
    root_id: str
    relative_path: str
    operation: str
    verified_digest: str
    status: str


class ApplyStagedDiffCapability(BaseCapability):
    """
    High-risk consequential capability to apply an approved staged diff.
    Binds human approval to the exact operation, path, original digest,
    proposed digest, and diff digest. Writes atomically, independently verifies
    the resulting file bytes, and never retries automatically.
    """

    metadata = CapabilityMetadata(
        capability_id="filesystem.apply_staged_diff",
        version="1.0.0",
        name="Apply Staged Filesystem Diff",
        description="Apply an approved staged text file diff, write atomically, and verify post-write file digest.",
        risk_tier=RiskTier.HIGH,
        required_permission="filesystem:write",
        required_classification="INTERNAL",
        timeout_seconds=10.0,
        max_output_bytes=65536,
        idempotent=False,
        max_retries=0,
        consequential=True,
        preconditions=[
            "Proposal must be staged and human-approved",
            "Root must be active in server allowlist",
            "Current file state on disk must match approved original state",
        ],
        expected_effects=[
            "Target file is created or replaced atomically",
            "Resulting file digest matches approved proposed digest exactly",
            "Proposal status transitions to 'applied'",
        ],
    )

    input_model = ApplyStagedDiffInput
    output_model = ApplyStagedDiffOutput

    def prepare_input(self, input_data: dict[str, Any], db: Any = None) -> dict[str, Any]:
        """
        Server-side preparation and cryptographic binding lifecycle hook.
        Resolves the staged proposal from database, checks server root allowlist,
        and injects canonical operation parameters so input_digest covers all of them.
        Rejects any caller-supplied forged or conflicting parameters.
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
            if proposal.status != "staged":
                raise ValidationError(
                    f"Staged proposal '{pid}' is in status '{proposal.status}'. Only 'staged' proposals can be submitted for apply."
                )

            # Re-verify root allowlist includes proposal root
            roots_mgr = get_roots_manager()
            roots_mgr.get_root_path(proposal.root_id)

            prepared = dict(input_data)
            server_bindings = {
                "root_id": proposal.root_id,
                "relative_path": proposal.relative_path,
                "operation": proposal.operation,
                "expected_original_state": proposal.original_state,
                "expected_proposed_digest": proposal.proposed_digest,
                "expected_diff_digest": proposal.diff_digest,
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

        # Resolve proposal and database session
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
            if proposal.status != "staged":
                raise ExecutionError(
                    f"Staged proposal '{pid}' is in status '{proposal.status}', expected 'staged'."
                )

            # Re-evaluate root allowlist and resolve safe path immediately before mutation
            roots_mgr = get_roots_manager()
            canonical_root, target_path = roots_mgr.resolve_safe_path(
                root_id=proposal.root_id,
                relative_path=proposal.relative_path,
                must_exist=(proposal.operation == OperationType.REPLACE.value),
                parent_must_exist=True,
            )

            adapter = get_filesystem_adapter()

            # Pre-mutation state inspection on disk
            if proposal.operation == OperationType.CREATE.value:
                if adapter.file_exists(target_path, root_path=canonical_root):
                    raise ExecutionError(
                        f"Apply rejected: Target file unexpectedly exists at '{proposal.relative_path}' prior to creation."
                    )
            else:
                # replace operation
                if not adapter.file_exists(target_path, root_path=canonical_root):
                    raise ExecutionError(
                        f"Apply rejected: Target file does not exist at '{proposal.relative_path}'."
                    )
                current_digest = adapter.get_content_digest(target_path, root_path=canonical_root)
                if current_digest != validated.expected_original_state:
                    raise ExecutionError(
                        f"Apply rejected: Current file digest '{current_digest}' does not match approved original digest '{validated.expected_original_state}'."
                    )

            # Recompute proposed content and diff digests
            current_prop_digest = compute_content_digest(proposal.proposed_content)
            if current_prop_digest != validated.expected_proposed_digest:
                raise ExecutionError(
                    f"Apply rejected: Staged content digest '{current_prop_digest}' does not match approved digest '{validated.expected_proposed_digest}'."
                )

            current_diff_digest = compute_diff_digest(proposal.unified_diff)
            if current_diff_digest != validated.expected_diff_digest:
                raise ExecutionError(
                    f"Apply rejected: Staged diff digest '{current_diff_digest}' does not match approved diff digest '{validated.expected_diff_digest}'."
                )

            # Perform atomic write to target path with exclusive creation or conditional replacement
            if proposal.operation == OperationType.CREATE.value:
                adapter.write_text_atomic(
                    path=target_path,
                    content=proposal.proposed_content,
                    root_path=canonical_root,
                    exclusive=True,
                    expected_proposed_digest=validated.expected_proposed_digest,
                )
            else:
                adapter.write_text_atomic(
                    path=target_path,
                    content=proposal.proposed_content,
                    root_path=canonical_root,
                    exclusive=False,
                    expected_original_digest=validated.expected_original_state,
                    expected_proposed_digest=validated.expected_proposed_digest,
                )

            # Independent post-write verification: Re-open and verify destination bytes
            actual_text = adapter.read_text(target_path, root_path=canonical_root)
            actual_digest = compute_content_digest(actual_text)

            if actual_digest != validated.expected_proposed_digest:
                raise ExecutionError(
                    f"Post-write verification failed: Resulting file digest '{actual_digest}' does not match expected '{validated.expected_proposed_digest}'."
                )

            # Mark applied and verified in database
            repo.mark_applied(pid, verified_digest=actual_digest)

            output = ApplyStagedDiffOutput(
                proposal_id=proposal.id,
                root_id=proposal.root_id,
                relative_path=proposal.relative_path,
                operation=proposal.operation,
                verified_digest=actual_digest,
                status="applied",
            )
            return ExecutionResult(
                success=True,
                output_data=output.model_dump(),
                evidence={
                    "proposal_id": proposal.id,
                    "verified_digest": actual_digest,
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
            expected_digest = input_data.get("expected_proposed_digest")
            if out.verified_digest != expected_digest:
                return VerificationResult(
                    passed=False,
                    reason=f"Verified digest '{out.verified_digest}' does not match approved proposed digest '{expected_digest}'.",
                )
            if out.status != "applied":
                return VerificationResult(
                    passed=False,
                    reason=f"Status '{out.status}' is not 'applied'.",
                )
            return VerificationResult(passed=True)
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Independent verifier failed: {str(exc)}",
                details={"error": str(exc)},
            )
