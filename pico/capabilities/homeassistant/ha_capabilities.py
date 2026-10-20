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
from pico.homeassistant.service import HomeAssistantService


# 1. Status Capability
class HomeAssistantStatusInput(BaseModel):
    check_connection: bool = True


class HomeAssistantStatusOutput(BaseModel):
    enabled: bool
    configured: bool
    base_url: str
    connection_state: str
    api_version: str | None = None
    message: str
    live_subscription_enabled: bool = False
    physical_device_connectivity_claimed: bool = False


class HomeAssistantStatusCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="homeassistant.status",
        version="1.0.0",
        name="Home Assistant Status",
        description="Inspect Home Assistant hub integration and connection status.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="homeassistant:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=4096,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = HomeAssistantStatusInput
    output_model = HomeAssistantStatusOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        service = HomeAssistantService()
        status = await service.get_status()
        return ExecutionResult(
            success=True,
            output_data=status.model_dump(),
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        try:
            self.output_model.model_validate(output_data)
            # Invariant: never claim physical device connectivity
            if output_data.get("physical_device_connectivity_claimed") is True:
                return VerificationResult(
                    passed=False,
                    reason="Capability erroneously claimed physical device connectivity.",
                )
            return VerificationResult(passed=True)
        except Exception as exc:
            return VerificationResult(passed=False, reason=f"Verification failed: {str(exc)}")


# 2. List Entities Capability
class HomeAssistantListEntitiesInput(BaseModel):
    domain: str | None = Field(default=None, description="Optional domain filter (e.g. 'light', 'sensor')")


class SimpleEntityItem(BaseModel):
    entity_id: str
    domain: str
    state: str
    friendly_name: str | None = None
    device_class: str | None = None
    unit_of_measurement: str | None = None
    data_classification: str = "INTERNAL"


class HomeAssistantListEntitiesOutput(BaseModel):
    total_count: int
    domain_filter: str | None = None
    entities: list[SimpleEntityItem]
    data_classification: str = "INTERNAL"


class HomeAssistantListEntitiesCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="homeassistant.list_entities",
        version="1.0.0",
        name="Home Assistant List Entities",
        description="Discover available smart-home entities and their current states without mutation.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="homeassistant:read",
        required_classification="INTERNAL",
        timeout_seconds=10.0,
        max_output_bytes=65536,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = HomeAssistantListEntitiesInput
    output_model = HomeAssistantListEntitiesOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        service = HomeAssistantService()
        domain = input_data.get("domain")
        result = await service.list_entities(domain=domain)
        return ExecutionResult(
            success=True,
            output_data=result.model_dump(),
        )


# 3. Get Entity State Capability
class HomeAssistantGetEntityStateInput(BaseModel):
    entity_id: str = Field(description="Unique entity identifier (e.g. 'light.kitchen')")


class HomeAssistantGetEntityStateOutput(BaseModel):
    entity_id: str
    domain: str
    state: str
    friendly_name: str | None = None
    device_class: str | None = None
    unit_of_measurement: str | None = None
    data_classification: str = "INTERNAL"
    attributes: dict[str, Any] = Field(default_factory=dict)


class HomeAssistantGetEntityStateCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="homeassistant.get_entity_state",
        version="1.0.0",
        name="Home Assistant Get Entity State",
        description="Read the current reported state and attributes of a single smart-home entity.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="homeassistant:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=8192,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = HomeAssistantGetEntityStateInput
    output_model = HomeAssistantGetEntityStateOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        service = HomeAssistantService()
        entity_id = input_data.get("entity_id", "")
        result = await service.get_entity_state(entity_id=entity_id)
        return ExecutionResult(
            success=True,
            output_data=result.model_dump(),
        )


# 4. State Overview Capability
class HomeAssistantOverviewInput(BaseModel):
    domains: list[str] | None = Field(default=None, description="Optional target domains to highlight")


class HomeAssistantOverviewOutput(BaseModel):
    total_entities: int
    domain_counts: dict[str, int]
    highlights: list[SimpleEntityItem]
    data_classification: str = "INTERNAL"


class HomeAssistantOverviewCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="homeassistant.overview",
        version="1.0.0",
        name="Home Assistant State Overview",
        description="Retrieve a bounded smart-home state overview suitable for assistant situational awareness.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="homeassistant:read",
        required_classification="INTERNAL",
        timeout_seconds=10.0,
        max_output_bytes=32768,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = HomeAssistantOverviewInput
    output_model = HomeAssistantOverviewOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        service = HomeAssistantService()
        domains = input_data.get("domains")
        result = await service.get_overview(domains=domains)
        return ExecutionResult(
            success=True,
            output_data=result.model_dump(),
        )
