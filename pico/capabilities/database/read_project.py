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
from pico.repositories.project_repo import ProjectRepository


class ReadProjectInput(BaseModel):
    project_id: str = Field(min_length=1, max_length=64, description="Project ID to look up")


class ReadProjectOutput(BaseModel):
    found: bool
    project_id: str
    name: str | None = None
    status: str | None = None
    created_at_iso: str | None = None


class ReadProjectCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="db.read_project",
        version="1.0.0",
        name="Read Project by ID",
        description="Reads project record from the local SQLite database without mutating state.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="projects:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=8192,
        idempotent=True,
        max_retries=2,
        consequential=False,
    )
    input_model = ReadProjectInput
    output_model = ReadProjectOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        if not context.db:
            return ExecutionResult(success=False, error_message="Database session not available in context")

        repo = ProjectRepository(context.db)
        pid = input_data["project_id"]
        proj = repo.get_by_id(pid)
        if not proj:
            return ExecutionResult(
                success=True,
                output_data={"found": False, "project_id": pid},
                evidence={"db_queried": True, "row_exists": False},
            )
        return ExecutionResult(
            success=True,
            output_data={
                "found": True,
                "project_id": proj.id,
                "name": proj.name,
                "status": proj.status,
                "created_at_iso": proj.created_at.isoformat() if proj.created_at else None,
            },
            evidence={"db_queried": True, "row_exists": True, "verified_name": proj.name},
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        base_ver = await super().verify(input_data, output_data, evidence, context)
        if not base_ver.passed:
            return base_ver

        if not evidence.get("db_queried"):
            return VerificationResult(passed=False, reason="Verification failed: DB query evidence missing.")

        found = output_data.get("found", False)
        if found and not output_data.get("name"):
            return VerificationResult(passed=False, reason="Found project returned without a name.")
        return VerificationResult(passed=True)
