from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from pydantic import BaseModel


class RiskTier(str, Enum):
    READ_ONLY = "READ_ONLY"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class CapabilityMetadata:
    capability_id: str
    version: str = "1.0.0"
    name: str = ""
    description: str = ""
    risk_tier: RiskTier = RiskTier.LOW
    required_permission: str | None = None
    required_classification: str = "INTERNAL"
    timeout_seconds: float = 10.0
    max_output_bytes: int = 65536
    idempotent: bool = False
    max_retries: int = 0
    consequential: bool = False
    preconditions: list[str] = field(default_factory=list)
    expected_effects: list[str] = field(default_factory=list)


@dataclass
class ExecutionContext:
    execution_id: str
    task_id: str | None = None
    session_id: str | None = None
    correlation_id: str | None = None
    caller_permissions: set[str] = field(default_factory=set)
    context_classification: str = "INTERNAL"
    approver: str | None = None
    db: Any = None


@dataclass
class ExecutionResult:
    success: bool
    output_data: dict[str, Any] | None = None
    error_message: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    passed: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class BaseCapability(ABC):
    metadata: CapabilityMetadata
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    def prepare_input(self, input_data: dict[str, Any], db: Any = None) -> dict[str, Any]:
        """
        Optionally prepare, enrich, or canonicalize input before validation and digest calculation.
        Default implementation returns input_data unchanged.
        """
        return input_data

    @abstractmethod
    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        """Execute the capability logic under sandbox/bounds constraints."""
        pass

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        """
        Deterministic independent verifier.
        Validates output matches output_model schema and checks postconditions.
        """
        try:
            self.output_model.model_validate(output_data)
            return VerificationResult(passed=True)
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Output schema validation failed: {str(exc)}",
                details={"error": str(exc)},
            )
