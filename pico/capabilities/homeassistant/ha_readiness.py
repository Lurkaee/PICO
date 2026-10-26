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
from pico.homeassistant.contracts import (
    ActuationReadinessReport,
    ClimateReadinessReport,
    SwitchActuationReadinessReport,
)
from pico.homeassistant.readiness import (
    evaluate_actuation_readiness,
    evaluate_climate_readiness,
    evaluate_switch_actuation_readiness,
)


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


class HomeAssistantSwitchActuationReadinessInput(BaseModel):
    check_live: bool = Field(
        default=False,
        description="Explicit operator opt-in to perform live read-only connectivity check. Default False performs offline preflight.",
    )
    target_entity_id: str | None = Field(
        default=None,
        description="Optional specific switch entity ID to check against Stage C allowlist.",
    )


class HomeAssistantSwitchActuationReadinessCapability(BaseCapability):
    """Read-only capability evaluating commissioning readiness for Stage C allowlisted switches."""

    metadata = CapabilityMetadata(
        capability_id="homeassistant.switch_readiness",
        version="1.0.0",
        name="Home Assistant Switch Readiness",
        description="Inspect commissioning, Stage C allowlist, and physical action readiness for switches without executing mutations.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="homeassistant:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=8192,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = HomeAssistantSwitchActuationReadinessInput
    output_model = SwitchActuationReadinessReport

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        validated = self.input_model.model_validate(input_data)
        report = await evaluate_switch_actuation_readiness(
            check_live=validated.check_live,
            target_entity_id=validated.target_entity_id,
        )
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
            reason="Read-only switch actuation readiness check evaluated successfully with zero mutations.",
        )


class HomeAssistantClimateReadinessInput(BaseModel):
    check_live: bool = Field(
        default=False,
        description="Explicit operator opt-in to perform live read-only connectivity check. Default False performs offline preflight.",
    )
    target_entity_id: str | None = Field(
        default=None,
        description="Optional specific climate entity ID to check against Stage D allowlist.",
    )


class HomeAssistantClimateReadinessCapability(BaseCapability):
    """Read-only capability evaluating commissioning readiness for Stage D climate awareness."""

    metadata = CapabilityMetadata(
        capability_id="homeassistant.climate_readiness",
        version="1.0.0",
        name="Home Assistant Climate Readiness",
        description="Inspect configuration, Stage D allowlist, and read-only readiness for climate entities without executing mutations.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="homeassistant:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=8192,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = HomeAssistantClimateReadinessInput
    output_model = ClimateReadinessReport

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        validated = self.input_model.model_validate(input_data)
        report = await evaluate_climate_readiness(
            check_live=validated.check_live,
            target_entity_id=validated.target_entity_id,
        )
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
            reason="Read-only climate readiness check evaluated successfully with zero mutations.",
        )
