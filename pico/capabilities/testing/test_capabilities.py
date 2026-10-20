import asyncio
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


# --- 1. Idempotent Counter ---
class IdempotentCounterInput(BaseModel):
    increment: int = Field(default=1, ge=1, le=100)


class IdempotentCounterOutput(BaseModel):
    incremented_by: int
    acknowledged: bool


class IdempotentCounterCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="test.idempotent_counter",
        version="1.0.0",
        name="Test Idempotent Counter",
        description="Test capability for verifying idempotency key deduplication and state preservation.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="PUBLIC",
        timeout_seconds=2.0,
        idempotent=True,
        max_retries=2,
        consequential=False,
    )
    input_model = IdempotentCounterInput
    output_model = IdempotentCounterOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        inc = input_data.get("increment", 1)
        return ExecutionResult(
            success=True,
            output_data={"incremented_by": inc, "acknowledged": True},
            evidence={"inc_recorded": inc},
        )


# --- 2. Controlled Failure ---
class ControlledFailureInput(BaseModel):
    should_fail: bool = True
    error_message: str = "Simulated deterministic execution failure"


class ControlledFailureOutput(BaseModel):
    result: str


class ControlledFailureCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="test.controlled_failure",
        version="1.0.0",
        name="Test Controlled Failure",
        description="Test capability that deterministically fails execution when requested.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="PUBLIC",
        timeout_seconds=2.0,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = ControlledFailureInput
    output_model = ControlledFailureOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        if input_data.get("should_fail", True):
            return ExecutionResult(
                success=False,
                error_message=input_data.get("error_message", "Controlled failure triggered."),
            )
        return ExecutionResult(
            success=True,
            output_data={"result": "Execution succeeded as requested."},
        )


# --- 3. Slow Timeout ---
class SlowTimeoutInput(BaseModel):
    sleep_seconds: float = Field(default=5.0, ge=0.0, le=30.0)


class SlowTimeoutOutput(BaseModel):
    slept_seconds: float


class SlowTimeoutCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="test.slow_timeout",
        version="1.0.0",
        name="Test Slow Timeout",
        description="Test capability that sleeps to verify execution timeout enforcement.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="PUBLIC",
        timeout_seconds=0.5,  # Short timeout for testing deadline
        idempotent=False,
        max_retries=0,
        consequential=False,
    )
    input_model = SlowTimeoutInput
    output_model = SlowTimeoutOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        duration = input_data.get("sleep_seconds", 5.0)
        await asyncio.sleep(duration)
        return ExecutionResult(
            success=True,
            output_data={"slept_seconds": duration},
        )


# --- 4. Verifier Failure ---
class VerifierFailureInput(BaseModel):
    trigger_verifier_failure: bool = True


class VerifierFailureOutput(BaseModel):
    raw_status: str


class VerifierFailureCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="test.verifier_failure",
        version="1.0.0",
        name="Test Verifier Failure",
        description="Test capability where execution succeeds, but independent verifier detects a discrepancy.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="PUBLIC",
        timeout_seconds=2.0,
        idempotent=True,
        max_retries=0,
        consequential=False,
    )
    input_model = VerifierFailureInput
    output_model = VerifierFailureOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        # Execution handler claims success!
        return ExecutionResult(
            success=True,
            output_data={"raw_status": "handler_reported_ok"},
            evidence={"tampered": input_data.get("trigger_verifier_failure", True)},
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        # Independent verification catches the failure
        if input_data.get("trigger_verifier_failure", True):
            return VerificationResult(
                passed=False,
                reason="Independent verifier detected invalid postcondition proof.",
                details={"evidence": evidence},
            )
        return VerificationResult(passed=True)


# --- 5. Consequential Action Requiring Human Approval ---
class ConsequentialActionInput(BaseModel):
    resource_id: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=64)


class ConsequentialActionOutput(BaseModel):
    performed: bool
    resource_id: str
    operation: str


class ConsequentialActionCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="test.consequential_action",
        version="1.0.0",
        name="Test Consequential Action",
        description="Test capability simulating an action that requires prior human approval.",
        risk_tier=RiskTier.HIGH,
        required_permission="admin:consequential",
        required_classification="INTERNAL",
        timeout_seconds=3.0,
        idempotent=False,
        max_retries=0,
        consequential=True,  # Mandatory human approval!
    )
    input_model = ConsequentialActionInput
    output_model = ConsequentialActionOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            output_data={
                "performed": True,
                "resource_id": input_data["resource_id"],
                "operation": input_data["operation"],
            },
            evidence={"approver": context.approver},
        )
