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
from pico.repositories.task_repo import TaskRepository


class ReadTaskInput(BaseModel):
    task_id: str = Field(min_length=1, max_length=64, description="Task ID to look up")


class ReadTaskOutput(BaseModel):
    found: bool
    task_id: str
    title: str | None = None
    status: str | None = None
    project_id: str | None = None
    created_at_iso: str | None = None


class ReadTaskCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="db.read_task",
        version="1.0.0",
        name="Read Task by ID",
        description="Reads task record from the local SQLite database without mutating state.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="tasks:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=8192,
        idempotent=True,
        max_retries=2,
        consequential=False,
    )
    input_model = ReadTaskInput
    output_model = ReadTaskOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        if not context.db:
            return ExecutionResult(success=False, error_message="Database session not available in context")

        repo = TaskRepository(context.db)
        tid = input_data["task_id"]
        task = repo.get_by_id(tid)
        if not task:
            return ExecutionResult(
                success=True,
                output_data={"found": False, "task_id": tid},
                evidence={"db_queried": True, "row_exists": False},
            )
        return ExecutionResult(
            success=True,
            output_data={
                "found": True,
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "project_id": task.project_id,
                "created_at_iso": task.created_at.isoformat() if task.created_at else None,
            },
            evidence={"db_queried": True, "row_exists": True, "verified_status": task.status},
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
        if found and not output_data.get("title"):
            return VerificationResult(passed=False, reason="Found task returned without a title.")
        return VerificationResult(passed=True)
