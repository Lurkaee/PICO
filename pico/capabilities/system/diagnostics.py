from typing import Any
from pydantic import BaseModel
from sqlalchemy import text

from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)
from pico.core.config import settings


class DiagnosticsInput(BaseModel):
    include_db_check: bool = True


class DiagnosticsOutput(BaseModel):
    system_name: str
    version: str
    environment: str
    db_responsive: bool
    status: str


class DiagnosticsCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="system.diagnostics",
        version="1.0.0",
        name="System Runtime Diagnostics",
        description="Checks system readiness, database connectivity, and environment parameters without executing shell commands.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="system:diagnostics",
        required_classification="INTERNAL",
        timeout_seconds=3.0,
        max_output_bytes=4096,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = DiagnosticsInput
    output_model = DiagnosticsOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        db_responsive = False
        if input_data.get("include_db_check", True) and context.db:
            try:
                res = context.db.execute(text("SELECT 1")).scalar()
                db_responsive = (res == 1)
            except Exception:
                db_responsive = False

        return ExecutionResult(
            success=True,
            output_data={
                "system_name": settings.PROJECT_NAME,
                "version": settings.VERSION,
                "environment": settings.ENVIRONMENT,
                "db_responsive": db_responsive,
                "status": "healthy" if db_responsive else "degraded",
            },
            evidence={"tested_db": input_data.get("include_db_check", True)},
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

        if output_data.get("system_name") != settings.PROJECT_NAME:
            return VerificationResult(passed=False, reason="Reported system name does not match configuration.")
        return VerificationResult(passed=True)
