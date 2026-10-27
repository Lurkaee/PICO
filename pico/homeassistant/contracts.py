from enum import Enum
from typing import Any
from pydantic import BaseModel, Field

from pico.core.errors import PicoError


class HomeAssistantError(PicoError):
    """Base exception for Home Assistant integration errors."""
    pass


class HomeAssistantConnectionError(HomeAssistantError):
    """Raised when communication with Home Assistant fails or times out."""
    pass


class HomeAssistantConnectionState(str, Enum):
    DISABLED = "disabled"
    MISSING_CONFIGURATION = "missing_configuration"
    CONNECTION_UNAVAILABLE = "connection_unavailable"
    AUTHENTICATION_REJECTED = "authentication_rejected"
    CONNECTED = "connected"
    READ_FAILED = "read_failed"


class HomeAssistantStatus(BaseModel):
    enabled: bool = False
    configured: bool = False
    base_url: str = "http://127.0.0.1:8123"
    connection_state: HomeAssistantConnectionState = HomeAssistantConnectionState.DISABLED
    api_version: str | None = None
    message: str = "Home Assistant integration is disabled by default."
    live_subscription_enabled: bool = False
    physical_device_connectivity_claimed: bool = False


class EntityState(BaseModel):
    entity_id: str
    domain: str
    state: str
    friendly_name: str | None = None
    device_class: str | None = None
    unit_of_measurement: str | None = None
    data_classification: str = "INTERNAL"
    last_changed: str | None = None
    last_updated: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EntityListOutput(BaseModel):
    total_count: int
    domain_filter: str | None = None
    entities: list[EntityState]
    data_classification: str = "INTERNAL"


class EntityOverviewOutput(BaseModel):
    total_entities: int
    domain_counts: dict[str, int]
    highlights: list[EntityState]
    data_classification: str = "INTERNAL"


class TestLightActuationPlan(BaseModel):
    __test__ = False
    plan_id: str
    entity_id: str
    requested_state: str
    current_state: str
    service: str
    is_mutation_needed: bool
    preconditions: list[str]
    expected_postconditions: list[str]
    input_digest: str
    expires_at: str
    data_classification: str = "INTERNAL"


class TestLightActuationResult(BaseModel):
    __test__ = False
    success: bool
    entity_id: str
    requested_state: str
    reported_state: str
    dispatch_status: str
    verification_status: str
    verification_details: dict[str, Any] = Field(default_factory=dict)
    message: str
    physical_device_connectivity_claimed: bool = False


class ReadinessCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_CHECKED = "NOT_CHECKED"


class ReadinessErrorCategory(str, Enum):
    INVALID_URL = "INVALID_URL"
    INSECURE_TRANSPORT = "INSECURE_TRANSPORT"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    AUTHENTICATION_REJECTED = "AUTHENTICATION_REJECTED"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    INVALID_ENTITY = "INVALID_ENTITY"
    AUTHORIZATION_UNAVAILABLE = "AUTHORIZATION_UNAVAILABLE"
    ACTUATION_ENABLED = "ACTUATION_ENABLED"
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"
    STATE_UNKNOWN = "STATE_UNKNOWN"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    UNIT_UNKNOWN = "UNIT_UNKNOWN"
    NOT_ALLOWLISTED = "NOT_ALLOWLISTED"
    METADATA_INVALID = "METADATA_INVALID"
    TARGET_STEP_MISMATCH = "TARGET_STEP_MISMATCH"
    STEP_UNKNOWN = "STEP_UNKNOWN"
    STEP_ALIGNMENT_UNKNOWN = "STEP_ALIGNMENT_UNKNOWN"
    MODE_INCOMPATIBLE = "MODE_INCOMPATIBLE"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"


class ReadinessCheckItem(BaseModel):
    name: str
    status: ReadinessCheckStatus
    error_category: ReadinessErrorCategory | None = None
    message: str
    remediation: str | None = None


class ActuationReadinessReport(BaseModel):
    ready_for_commissioning: bool = False
    target_entity_id: str | None = None
    live_check_performed: bool = False
    live_passed: bool | None = None
    reported_state: str | None = None
    actuation_enabled: bool = False
    checks: list[ReadinessCheckItem] = Field(default_factory=list)
    summary: str


class SwitchActuationPlan(BaseModel):
    __test__ = False
    plan_id: str
    entity_id: str
    requested_state: str
    current_state: str
    service: str
    state_readiness: str = "ready"
    is_mutation_needed: bool
    preconditions: list[str]
    expected_postconditions: list[str]
    input_digest: str
    expires_at: str
    data_classification: str = "INTERNAL"


class SwitchActuationResult(BaseModel):
    __test__ = False
    success: bool
    entity_id: str
    requested_state: str
    reported_state: str
    dispatch_status: str
    verification_status: str
    verification_details: dict[str, Any] = Field(default_factory=dict)
    message: str
    physical_device_connectivity_claimed: bool = False


class SwitchActuationReadinessReport(BaseModel):
    ready_for_commissioning: bool = False
    target_entity_id: str | None = None
    allowlisted_entities: list[str] = Field(default_factory=list)
    stage_c_enabled: bool = False
    actuation_enabled: bool = False
    live_check_performed: bool = False
    live_passed: bool | None = None
    reported_state: str | None = None
    state_readiness: str | None = None
    checks: list[ReadinessCheckItem] = Field(default_factory=list)
    summary: str


class ClimateObservation(BaseModel):
    """Bounded, typed climate observation for read-only awareness."""
    entity_id: str
    domain: str = "climate"
    state: str
    current_temperature: float | None = None
    target_temperature: float | None = None
    min_temp: float | None = None
    max_temp: float | None = None
    target_temp_step: float | None = None
    supported_features: int | None = None
    temperature_unit: str | None = None
    hvac_mode: str | None = None
    hvac_modes: list[str] = Field(default_factory=list)
    state_readiness: str = "ready"
    reasons: list[str] = Field(default_factory=list)
    data_classification: str = "INTERNAL"
    attributes: dict[str, Any] = Field(default_factory=dict)
    is_mutation_performed: bool = False
    physical_device_connectivity_claimed: bool = False


class ClimateSetpointPlan(BaseModel):
    """Read-only feasibility assessment for a requested temperature setpoint.

    This plan is an assessment only. It is not an approval ticket, executable command,
    or authorization to actuate or alter physical climate state.
    """
    __test__ = False
    plan_id: str
    entity_id: str
    requested_temperature: float
    requested_unit: str | None = None
    grid_origin: float | None = None
    current_temperature: float | None = None
    target_temperature: float | None = None
    min_temp: float | None = None
    max_temp: float | None = None
    target_temp_step: float | None = None
    supported_features: int | None = None
    temperature_unit: str | None = None
    hvac_mode: str | None = None
    hvac_modes: list[str] = Field(default_factory=list)
    is_feasible: bool = False
    feasibility_status: str
    reasons: list[str] = Field(default_factory=list)
    is_mutation_performed: bool = False
    input_digest: str
    data_classification: str = "INTERNAL"


class ClimateReadinessReport(BaseModel):
    """Commissioning and awareness readiness report for Stage D climate inspection."""
    ready_for_observation: bool = False
    target_entity_id: str | None = None
    allowlisted_entities: list[str] = Field(default_factory=list)
    climate_awareness_enabled: bool = False
    actuation_enabled: bool = False
    live_check_performed: bool = False
    live_passed: bool | None = None
    state_readiness: str | None = None
    observation: ClimateObservation | None = None
    checks: list[ReadinessCheckItem] = Field(default_factory=list)
    summary: str
