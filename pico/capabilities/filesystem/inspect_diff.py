"""Capability to inspect a staged filesystem diff proposal."""

from typing import Any
from pydantic import BaseModel, Field

from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
)
from pico.core.errors import EntityNotFoundError
from pico.db.session import SessionLocal
from pico.filesystem.staging import get_staged_diff_proposal


class InspectDiffInput(BaseModel):
    proposal_id: str = Field(..., description="Unique identifier of staged diff proposal")


class InspectDiffOutput(BaseModel):
    proposal_id: str
    root_id: str
    relative_path: str
    operation: str
    original_state: str
    proposed_digest: str
    diff_digest: str
    unified_diff: str
    status: str
    execution_id: str | None = None
    applied_at: str | None = None
    verified_at: str | None = None
    verified_digest: str | None = None
    rolled_back_at: str | None = None


class InspectDiffCapability(BaseCapability):
    """
    Read-only capability to inspect the exact diff and verification metadata
    of a staged filesystem proposal.
    """

    metadata = CapabilityMetadata(
        capability_id="filesystem.inspect_diff",
        version="1.0.0",
        name="Inspect Filesystem Diff",
        description="Inspect an existing staged proposal, unified diff, digests, and verification status.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="filesystem:read",
        required_classification="INTERNAL",
        timeout_seconds=10.0,
        max_output_bytes=262144,
        idempotent=True,
        max_retries=0,
        consequential=False,
        preconditions=["Proposal must exist in database"],
        expected_effects=["Returns proposal diff and verification metadata; workspace untouched"],
    )

    input_model = InspectDiffInput
    output_model = InspectDiffOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        validated = self.input_model.model_validate(input_data)
        db = context.db
        if db is not None:
            proposal = get_staged_diff_proposal(db, validated.proposal_id)
        else:
            with SessionLocal() as session:
                proposal = get_staged_diff_proposal(session, validated.proposal_id)

        output = InspectDiffOutput(
            proposal_id=proposal.id,
            root_id=proposal.root_id,
            relative_path=proposal.relative_path,
            operation=proposal.operation,
            original_state=proposal.original_state,
            proposed_digest=proposal.proposed_digest,
            diff_digest=proposal.diff_digest,
            unified_diff=proposal.unified_diff,
            status=proposal.status,
            execution_id=proposal.execution_id,
            applied_at=proposal.applied_at.isoformat() if proposal.applied_at else None,
            verified_at=proposal.verified_at.isoformat() if proposal.verified_at else None,
            verified_digest=proposal.verified_digest,
            rolled_back_at=proposal.rolled_back_at.isoformat() if proposal.rolled_back_at else None,
        )
        return ExecutionResult(
            success=True,
            output_data=output.model_dump(),
            evidence={"proposal_id": proposal.id, "status": proposal.status},
        )
