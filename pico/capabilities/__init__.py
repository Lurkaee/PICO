from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)
from pico.capabilities.core.calculator import CalculatorCapability
from pico.capabilities.core.validate_json import ValidateJsonCapability
from pico.capabilities.database.read_project import ReadProjectCapability
from pico.capabilities.database.read_task import ReadTaskCapability
from pico.capabilities.perception.perception_capabilities import (
    PerceptionAnalyzeMotionCapability,
    PerceptionGetRecentEventsCapability,
    PerceptionStatusCapability,
    PerceptionValidateSensorPayloadCapability,
)
from pico.capabilities.registry import CapabilityRegistry, registry
from pico.capabilities.system.diagnostics import DiagnosticsCapability
from pico.capabilities.testing.test_capabilities import (
    ConsequentialActionCapability,
    ControlledFailureCapability,
    IdempotentCounterCapability,
    SlowTimeoutCapability,
    VerifierFailureCapability,
)


def register_default_capabilities(reg: CapabilityRegistry) -> None:
    """Register all built-in trusted capabilities in the registry."""
    reg.register(CalculatorCapability())
    reg.register(ValidateJsonCapability())
    reg.register(ReadProjectCapability())
    reg.register(ReadTaskCapability())
    reg.register(DiagnosticsCapability())
    reg.register(IdempotentCounterCapability())
    reg.register(ControlledFailureCapability())
    reg.register(SlowTimeoutCapability())
    reg.register(VerifierFailureCapability())
    reg.register(ConsequentialActionCapability())
    reg.register(PerceptionStatusCapability())
    reg.register(PerceptionGetRecentEventsCapability())
    reg.register(PerceptionValidateSensorPayloadCapability())
    reg.register(PerceptionAnalyzeMotionCapability())


# Automatically populate the default registry
register_default_capabilities(registry)

__all__ = [
    "BaseCapability",
    "CapabilityMetadata",
    "ExecutionContext",
    "ExecutionResult",
    "VerificationResult",
    "RiskTier",
    "CapabilityRegistry",
    "registry",
    "register_default_capabilities",
    "CalculatorCapability",
    "ValidateJsonCapability",
    "ReadProjectCapability",
    "ReadTaskCapability",
    "DiagnosticsCapability",
    "IdempotentCounterCapability",
    "ControlledFailureCapability",
    "SlowTimeoutCapability",
    "VerifierFailureCapability",
    "ConsequentialActionCapability",
    "PerceptionStatusCapability",
    "PerceptionGetRecentEventsCapability",
    "PerceptionValidateSensorPayloadCapability",
    "PerceptionAnalyzeMotionCapability",
]
