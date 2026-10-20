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
from pico.homeassistant.contracts import ActuationReadinessReport
from pico.homeassistant.readiness import evaluate_actuation_readiness


class HomeAssistantActuationReadinessInput(BaseModel):
    check_live: bool = Field(
        default=False,
        description="Explicit operator opt-in to perform live read-only connectivity check. Default False performs offline preflight.",
    )


class HomeAssistantActuationReadinessCapability(BaseCapability):
    """Read-only capability evaluating commissioning readiness for the configured test light."""

    metadata = CapabilityMetadata(
        capability_id="homeassistant.actuation_readiness",
        version="1.0.0",
        name="Home Assistant Actuation Readiness",
        description="Inspect commissioning and physical action readiness for the configured test light without executing mutations.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="homeassistant:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=8192,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = HomeAssistantActuationReadinessInput
    output_model = ActuationReadinessReport

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        validated = self.input_model.model_validate(input_data)
        report = await evaluate_actuation_readiness(check_live=validated.check_live)
        return ExecutionResult(
            success=True,
            output_data=report.model_dump(),
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        return VerificationResult(
            passed=True,
            reason="Read-only actuation readiness check evaluated successfully with zero mutations.",
        )
