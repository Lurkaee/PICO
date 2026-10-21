"""Capability to stage a filesystem diff proposal."""

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
from pico.filesystem.staging import stage_diff_proposal


class StageDiffInput(BaseModel):
    root_id: str = Field(..., description="Allowlisted filesystem root identifier")
    relative_path: str = Field(..., description="Workspace-relative path to target text file")
    operation: str = Field(..., description="Operation type: 'create' or 'replace'")
    proposed_content: str = Field(..., description="Proposed UTF-8 text file content")


class StageDiffOutput(BaseModel):
    proposal_id: str
    root_id: str
    relative_path: str
    operation: str
    original_state: str
    proposed_digest: str
    diff_digest: str
    unified_diff: str
    status: str


class StageDiffCapability(BaseCapability):
    """
    Read-only capability to inspect, validate, and stage a proposed text file change.
    Does not mutate the workspace target.
    """

    metadata = CapabilityMetadata(
        capability_id="filesystem.stage_diff",
        version="1.0.0",
        name="Stage Filesystem Diff",
        description="Inspect and stage a proposed text file creation or replacement with a unified diff.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="filesystem:read",
        required_classification="INTERNAL",
        timeout_seconds=10.0,
        max_output_bytes=262144,
        idempotent=False,
        max_retries=0,
        consequential=False,
        preconditions=["Root must be configured in server allowlist"],
        expected_effects=["Staged proposal persisted in local database; workspace untouched"],
    )

    input_model = StageDiffInput
    output_model = StageDiffOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        validated = self.input_model.model_validate(input_data)
        db = context.db
        if db is not None:
            proposal = stage_diff_proposal(
                db=db,
                root_id=validated.root_id,
                relative_path=validated.relative_path,
                operation=validated.operation,
                proposed_content=validated.proposed_content,
            )
        else:
            with SessionLocal() as session:
                proposal = stage_diff_proposal(
                    db=session,
                    root_id=validated.root_id,
                    relative_path=validated.relative_path,
                    operation=validated.operation,
                    proposed_content=validated.proposed_content,
                )

        output = StageDiffOutput(
            proposal_id=proposal.id,
            root_id=proposal.root_id,
            relative_path=proposal.relative_path,
            operation=proposal.operation,
            original_state=proposal.original_state,
            proposed_digest=proposal.proposed_digest,
            diff_digest=proposal.diff_digest,
            unified_diff=proposal.unified_diff,
            status=proposal.status,
        )
        return ExecutionResult(
            success=True,
            output_data=output.model_dump(),
            evidence={"proposal_id": proposal.id, "diff_digest": proposal.diff_digest},
        )
