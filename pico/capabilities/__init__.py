from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)
from pico.capabilities.core.calculator import CalculatorCapability
from pico.capabilities.core.model_infer import ModelInferCapability
from pico.capabilities.core.validate_json import ValidateJsonCapability
from pico.capabilities.database.read_project import ReadProjectCapability
from pico.capabilities.database.read_task import ReadTaskCapability
from pico.capabilities.homeassistant.ha_capabilities import (
    HomeAssistantGetEntityStateCapability,
    HomeAssistantListEntitiesCapability,
    HomeAssistantOverviewCapability,
    HomeAssistantStatusCapability,
)
from pico.capabilities.filesystem import (
    ApplyStagedDiffCapability,
    InspectDiffCapability,
    RollbackStagedDiffCapability,
    StageDiffCapability,
)
from pico.capabilities.homeassistant.ha_actuation import (
    HomeAssistantTestLightActuationCapability,
)
from pico.capabilities.homeassistant.ha_readiness import (
    HomeAssistantActuationReadinessCapability,
)
from pico.capabilities.browser import OpenPageCapability
from pico.capabilities.os.inspect_windows import InspectWindowsCapability
from pico.capabilities.os.launch_application import LaunchApplicationCapability
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
    reg.register(HomeAssistantStatusCapability())
    reg.register(HomeAssistantListEntitiesCapability())
    reg.register(HomeAssistantGetEntityStateCapability())
    reg.register(HomeAssistantOverviewCapability())
    reg.register(HomeAssistantTestLightActuationCapability())
    reg.register(HomeAssistantActuationReadinessCapability())
    reg.register(InspectWindowsCapability())
    reg.register(LaunchApplicationCapability())
    reg.register(StageDiffCapability())
    reg.register(InspectDiffCapability())
    reg.register(ApplyStagedDiffCapability())
    reg.register(RollbackStagedDiffCapability())
    reg.register(OpenPageCapability())
    reg.register(ModelInferCapability())



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
    "HomeAssistantStatusCapability",
    "HomeAssistantListEntitiesCapability",
    "HomeAssistantGetEntityStateCapability",
    "HomeAssistantOverviewCapability",
    "HomeAssistantTestLightActuationCapability",
    "HomeAssistantActuationReadinessCapability",
    "InspectWindowsCapability",
    "LaunchApplicationCapability",
    "StageDiffCapability",
    "InspectDiffCapability",
    "ApplyStagedDiffCapability",
    "RollbackStagedDiffCapability",
    "OpenPageCapability",
    "ModelInferCapability",
]
