import math
import secrets
from typing import Any, Literal
from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError
from pico.db.session import get_db
from pico.homeassistant.actuation import (
    HomeAssistantActuationService,
    HomeAssistantClimateActuationService,
    HomeAssistantSwitchActuationService,
)
from pico.homeassistant.climate import HomeAssistantClimateService
from pico.homeassistant.contracts import (
    ActuationReadinessReport,
    ClimateDualSetpointPlan,
    ClimateObservation,
    ClimateReadinessReport,
    ClimateSetpointPlan,
    EntityListOutput,
    EntityOverviewOutput,
    EntityState,
    HomeAssistantStatus,
    SwitchActuationPlan,
    SwitchActuationReadinessReport,
    TestLightActuationPlan,
)
from pico.homeassistant.readiness import (
    evaluate_actuation_readiness,
    evaluate_climate_readiness,
    evaluate_switch_actuation_readiness,
)
from pico.homeassistant.service import HomeAssistantService
from pico.homeassistant.ws_client import is_loopback_host
from pico.repositories.tool_execution_repo import ToolExecutionRepository
from pico.schemas.tools import ToolExecutionResponse
from pico.services.execution_engine import ToolExecutionEngine


def verify_household_access(
    request: Request,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
) -> None:
    """Enforce fail-closed exposure boundary on household state endpoints.

    Invariants:
    1. Local loopback clients ('127.0.0.1', '::1', 'localhost', 'testclient') are permitted.
    2. Non-loopback remote callers MUST provide a valid X-Pico-Approval-Token matching
       the server-configured APPROVAL_SECRET_TOKEN.
    3. If APPROVAL_SECRET_TOKEN is not configured or token is missing/mismatched,
       remote access is strictly rejected with 403 Forbidden (fail-closed).
    4. Integration enablement (HOME_ASSISTANT_ENABLED) is distinct from API exposure authorization.
    """
    client_host = request.client.host if request.client else "127.0.0.1"

    if is_loopback_host(client_host):
        return

    # Fail closed for remote clients unless verified via established APPROVAL_SECRET_TOKEN
    if (
        not settings.APPROVAL_SECRET_TOKEN
        or not x_approval_token
        or not secrets.compare_digest(x_approval_token, settings.APPROVAL_SECRET_TOKEN)
    ):
        raise AuthorizationError(
            "Access to household endpoints from remote host is restricted. "
            "Valid X-Pico-Approval-Token required."
        )


router = APIRouter(
    prefix="/api/homeassistant",
    tags=["Home Assistant"],
    dependencies=[Depends(verify_household_access)],
)


@router.get("/status", response_model=HomeAssistantStatus)
async def get_homeassistant_status() -> HomeAssistantStatus:
    """Inspect Home Assistant hub integration and connection status."""
    service = HomeAssistantService()
    return await service.get_status()


@router.get("/entities", response_model=EntityListOutput)
async def list_homeassistant_entities(
    domain: str | None = Query(default=None, description="Optional domain filter (e.g. 'light', 'sensor')"),
) -> EntityListOutput:
    """Discover available smart-home entities and their reported states without mutation."""
    service = HomeAssistantService()
    return await service.list_entities(domain=domain)


@router.get("/entities/{entity_id}", response_model=EntityState)
async def get_homeassistant_entity_state(entity_id: str) -> EntityState:
    """Read the current reported state and attributes of a specific smart-home entity."""
    service = HomeAssistantService()
    return await service.get_entity_state(entity_id=entity_id)


@router.get("/overview", response_model=EntityOverviewOutput)
async def get_homeassistant_overview(
    domains: list[str] | None = Query(default=None, description="Optional target domains to highlight"),
) -> EntityOverviewOutput:
    """Retrieve a bounded smart-home state overview suitable for situational awareness."""
    service = HomeAssistantService()
    return await service.get_overview(domains=domains)


# --- Milestone P7.3 Physical Actuation Operations (Single Configured Light) ---

class TestLightPlanRequest(BaseModel):
    target_state: Literal["on", "off"] = Field(
        description="Explicit target state: 'on' or 'off'. Toggle is strictly prohibited."
    )
    target_entity_id: str | None = Field(
        default=None,
        description="Optional target entity ID. If supplied, must match the configured test light.",
    )


class TestLightProposeRequest(BaseModel):
    target_state: Literal["on", "off"] = Field(
        description="Explicit target state: 'on' or 'off'. Toggle is strictly prohibited."
    )
    target_entity_id: str | None = Field(
        default=None,
        description="Optional target entity ID. If supplied, must match the configured test light.",
    )
    reason: str | None = Field(
        default=None,
        description="Optional human-readable reason for the action proposal.",
    )


@router.post(
    "/actuation/plan",
    response_model=TestLightActuationPlan,
    summary="Create a structured test-light action plan (read-only, no mutation)",
)
async def plan_test_light_actuation(
    payload: TestLightPlanRequest,
) -> TestLightActuationPlan:
    """Create a structured action plan for the single configured test light.

    This operation performs read-only inspection of the current entity state.
    It NEVER causes a physical action or service dispatch.
    """
    service = HomeAssistantActuationService()
    return await service.create_plan(
        target_state=payload.target_state,
        target_entity_id=payload.target_entity_id,
    )


@router.post(
    "/actuation/propose",
    response_model=ToolExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Propose an actuation execution (created in awaiting_approval status)",
)
def propose_test_light_actuation(
    payload: TestLightProposeRequest,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """Propose an actuation of the configured test light.

    Creates an execution record in 'awaiting_approval' status bound to a SHA-256 digest.
    Physical actuation fails closed without a verified X-Pico-Approval-Token matching APPROVAL_SECRET_TOKEN.
    """
    if (
        not settings.APPROVAL_SECRET_TOKEN
        or not x_approval_token
        or not secrets.compare_digest(x_approval_token, settings.APPROVAL_SECRET_TOKEN)
    ):
        raise AuthorizationError(
            "Physical actuation proposal fails closed without valid X-Pico-Approval-Token matching APPROVAL_SECRET_TOKEN."
        )

    engine = ToolExecutionEngine(db, registry)
    execution = engine.submit(
        capability_id="homeassistant.test_light_actuate",
        input_data={
            "target_state": payload.target_state,
            "target_entity_id": payload.target_entity_id,
            "reason": payload.reason,
        },
        caller_permissions={"homeassistant:actuate"},
    )
    return ToolExecutionResponse.model_validate(execution)


@router.get(
    "/actuation/readiness",
    response_model=ActuationReadinessReport,
    summary="Evaluate commissioning and physical action readiness (strictly read-only)",
)
async def get_actuation_readiness(
    check_live: bool = Query(
        default=False,
        description="Whether to perform an explicit live read-only connectivity check against the configured test light. Offline preflight is performed when False.",
    ),
) -> ActuationReadinessReport:
    """Evaluate commissioning and physical action readiness for the configured test light.

    This endpoint NEVER dispatches a mutation request, executes actions, or enables actuation.
    Live inspection requires explicit operator opt-in via check_live=True and performs
    only read-only state inspection.
    """
    return await evaluate_actuation_readiness(check_live=check_live)


@router.get(
    "/actuation/{execution_id}",
    response_model=ToolExecutionResponse,
    summary="Inspect approval and verification status of a test light actuation execution",
)
def get_actuation_status(
    execution_id: str,
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """Inspect the execution record, human approval ticket, and independent verification outcome."""
    repo = ToolExecutionRepository(db)
    execution = repo.get_by_id(execution_id)
    if not execution or execution.capability_id != "homeassistant.test_light_actuate":
        raise EntityNotFoundError("ActuationExecution", execution_id)
    return ToolExecutionResponse.model_validate(execution)


# --- Milestone P7.4.1 Stage C Switch Physical Actuation Operations ---

class SwitchPlanRequest(BaseModel):
    target_entity_id: str = Field(
        description="Exact switch entity ID. Must belong to the Stage C allowlist.",
    )
    target_state: Literal["on", "off"] = Field(
        description="Explicit target state: 'on' or 'off'. Toggle is strictly prohibited."
    )


class SwitchProposeRequest(BaseModel):
    target_entity_id: str = Field(
        description="Exact switch entity ID. Must belong to the Stage C allowlist.",
    )
    target_state: Literal["on", "off"] = Field(
        description="Explicit target state: 'on' or 'off'. Toggle is strictly prohibited."
    )
    reason: str | None = Field(
        default=None,
        description="Optional human-readable reason for the switch actuation proposal.",
    )


@router.post(
    "/switch/plan",
    response_model=SwitchActuationPlan,
    summary="Create a structured switch action plan (read-only, no mutation)",
)
async def plan_switch_actuation(
    payload: SwitchPlanRequest,
) -> SwitchActuationPlan:
    """Create a structured action plan for an allowlisted switch entity.

    Performs read-only inspection of the current entity state.
    NEVER causes physical action, service dispatch, or state mutation.
    """
    service = HomeAssistantSwitchActuationService()
    return await service.create_plan(
        target_entity_id=payload.target_entity_id,
        target_state=payload.target_state,
    )


@router.post(
    "/switch/propose",
    response_model=ToolExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Propose a switch actuation execution (created in awaiting_approval status)",
)
def propose_switch_actuation(
    payload: SwitchProposeRequest,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """Propose an actuation of an allowlisted switch.

    Creates an execution record in 'awaiting_approval' status bound to a SHA-256 digest.
    Physical actuation fails closed without a verified X-Pico-Approval-Token matching APPROVAL_SECRET_TOKEN.
    """
    if (
        not settings.APPROVAL_SECRET_TOKEN
        or not x_approval_token
        or not secrets.compare_digest(x_approval_token, settings.APPROVAL_SECRET_TOKEN)
    ):
        raise AuthorizationError(
            "Physical switch actuation proposal fails closed without valid X-Pico-Approval-Token matching APPROVAL_SECRET_TOKEN."
        )

    engine = ToolExecutionEngine(db, registry)
    execution = engine.submit(
        capability_id="homeassistant.allowlisted_switch_actuate",
        input_data={
            "target_entity_id": payload.target_entity_id,
            "target_state": payload.target_state,
            "reason": payload.reason,
        },
        caller_permissions={"homeassistant:actuate"},
    )
    return ToolExecutionResponse.model_validate(execution)


@router.get(
    "/switch/readiness",
    response_model=SwitchActuationReadinessReport,
    summary="Evaluate Stage C switch commissioning and physical action readiness (strictly read-only)",
)
async def get_switch_actuation_readiness(
    check_live: bool = Query(
        default=False,
        description="Whether to perform an explicit live read-only connectivity check. Offline preflight is performed when False.",
    ),
    target_entity_id: str | None = Query(
        default=None,
        description="Optional specific switch entity ID to check against Stage C allowlist.",
    ),
) -> SwitchActuationReadinessReport:
    """Evaluate commissioning, allowlist validity, and physical action readiness for switches.

    This endpoint NEVER dispatches a mutation request, executes actions, or enables actuation.
    """
    return await evaluate_switch_actuation_readiness(
        check_live=check_live,
        target_entity_id=target_entity_id,
    )


@router.get(
    "/switch/{execution_id}",
    response_model=ToolExecutionResponse,
    summary="Inspect approval and verification status of a switch actuation execution",
)
def get_switch_actuation_status(
    execution_id: str,
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """Inspect the execution record, human approval ticket, and independent verification outcome for a switch actuation."""
    repo = ToolExecutionRepository(db)
    execution = repo.get_by_id(execution_id)
    if not execution or execution.capability_id != "homeassistant.allowlisted_switch_actuate":
        raise EntityNotFoundError("SwitchActuationExecution", execution_id)
    return ToolExecutionResponse.model_validate(execution)


# --- Milestone P7.4.2.1 Stage D Read-Only Climate Awareness & Planning Operations ---

class ClimatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_entity_id: str = Field(
        description="Exact climate entity ID. Must belong to the Stage D allowlist.",
    )
    target_temperature: float = Field(
        description="Explicit requested target temperature setpoint.",
    )
    target_unit: str | None = Field(
        default=None,
        description="Optional explicit temperature unit (e.g. '°C', '°F').",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_untrusted_grid_origin(cls, data: Any) -> Any:
        if isinstance(data, dict) and "grid_origin" in data:
            raise ValueError(
                "Client-supplied 'grid_origin' is forbidden. Grid origins are server-side "
                "operator configurations and cannot be supplied or overridden in client requests."
            )
        return data


@router.post(
    "/climate/plan",
    response_model=ClimateSetpointPlan,
    summary="Assess feasibility of a requested climate temperature setpoint (read-only, no mutation)",
)
async def plan_climate_setpoint(
    payload: ClimatePlanRequest,
) -> ClimateSetpointPlan:
    """Create a structured feasibility assessment plan for an allowlisted climate entity.

    Performs read-only inspection of the current entity state and temperature bounds.
    NEVER causes physical action, service dispatch, or state mutation.
    """
    service = HomeAssistantClimateService()
    return await service.assess_setpoint_feasibility(
        target_entity_id=payload.target_entity_id,
        requested_temperature=payload.target_temperature,
        requested_unit=payload.target_unit,
    )


class ClimateDualSetpointPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_entity_id: str = Field(
        description="Exact climate entity ID. Must belong to the Stage D allowlist.",
    )
    target_temp_low: float = Field(
        description="Explicit requested lower target temperature setpoint.",
    )
    target_temp_high: float = Field(
        description="Explicit requested upper target temperature setpoint.",
    )
    target_unit: str | None = Field(
        default=None,
        description="Optional explicit temperature unit (e.g. '°C', '°F').",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_request_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "grid_origin" in data:
                raise ValueError(
                    "Client-supplied 'grid_origin' is forbidden. Grid origins are server-side "
                    "operator configurations and cannot be supplied or overridden in client requests."
                )
            for field in ("target_temp_low", "target_temp_high"):
                val = data.get(field)
                if isinstance(val, bool):
                    raise ValueError(f"Field '{field}' must be a numeric temperature; boolean values are strictly forbidden.")
                if val is not None and isinstance(val, (int, float)):
                    if not math.isfinite(float(val)):
                        raise ValueError(f"Field '{field}' must be a finite numeric temperature.")
        return data


@router.post(
    "/climate/dual-setpoint/plan",
    response_model=ClimateDualSetpointPlan,
    summary="Assess feasibility of a requested dual-setpoint temperature range (read-only, no mutation)",
)
async def plan_climate_dual_setpoint(
    payload: ClimateDualSetpointPlanRequest,
) -> ClimateDualSetpointPlan:
    """Create a structured dual-setpoint feasibility assessment plan for an allowlisted climate entity.

    Performs read-only inspection of the current entity state, dual-setpoint capability,
    operating mode, and temperature bounds.
    NEVER causes physical action, service dispatch, or state mutation.
    """
    service = HomeAssistantClimateService()
    return await service.assess_dual_setpoint_feasibility(
        target_entity_id=payload.target_entity_id,
        target_temp_low=payload.target_temp_low,
        target_temp_high=payload.target_temp_high,
        requested_unit=payload.target_unit,
    )



@router.get(
    "/climate/observe/{entity_id}",
    response_model=ClimateObservation,
    summary="Inspect climate entity state and temperature bounds (read-only, no mutation)",
)
async def get_climate_observation(
    entity_id: str,
) -> ClimateObservation:
    """Retrieve bounded, typed climate observation for an allowlisted climate entity.

    Performs read-only inspection of the current entity state.
    NEVER causes physical action, service dispatch, or state mutation.
    """
    service = HomeAssistantClimateService()
    return await service.get_climate_observation(entity_id=entity_id)


@router.get(
    "/climate/readiness",
    response_model=ClimateReadinessReport,
    summary="Evaluate Stage D climate awareness and readiness (strictly read-only)",
)
async def get_climate_readiness(
    check_live: bool = Query(
        default=False,
        description="Whether to perform an explicit live read-only connectivity check. Offline preflight is performed when False.",
    ),
    target_entity_id: str | None = Query(
        default=None,
        description="Optional specific climate entity ID to check against Stage D allowlist.",
    ),
) -> ClimateReadinessReport:
    """Evaluate configuration, allowlist validity, and observation readiness for climate entities.

    This endpoint NEVER dispatches a mutation request, executes actions, or enables actuation.
    """
    return await evaluate_climate_readiness(
        check_live=check_live,
        target_entity_id=target_entity_id,
    )


class ClimateProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_entity_id: str = Field(
        description="Exact climate entity ID in Stage D climate allowlist.",
    )
    target_temperature: float = Field(
        description="Explicit target temperature setpoint.",
    )
    target_unit: str | None = Field(
        default=None,
        description="Optional explicit temperature unit (e.g. '°C', '°F').",
    )
    reason: str | None = Field(
        default=None,
        description="Optional operator reason for actuation proposal.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_untrusted_grid_origin(cls, data: Any) -> Any:
        if isinstance(data, dict) and "grid_origin" in data:
            raise ValueError(
                "Client-supplied 'grid_origin' is forbidden. Grid origins are server-side "
                "operator configurations and cannot be supplied or overridden in client requests."
            )
        return data


@router.post(
    "/climate/propose",
    response_model=ToolExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Propose a climate setpoint actuation execution (created in awaiting_approval status)",
)
def propose_climate_actuation(
    payload: ClimateProposeRequest,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """Propose an actuation of an allowlisted climate entity setpoint.

    Creates an execution record in 'awaiting_approval' status bound to a SHA-256 digest.
    Physical actuation fails closed without a verified X-Pico-Approval-Token matching APPROVAL_SECRET_TOKEN.
    """
    if (
        not settings.APPROVAL_SECRET_TOKEN
        or not x_approval_token
        or not secrets.compare_digest(x_approval_token, settings.APPROVAL_SECRET_TOKEN)
    ):
        raise AuthorizationError(
            "Physical climate actuation proposal fails closed without valid X-Pico-Approval-Token matching APPROVAL_SECRET_TOKEN."
        )

    engine = ToolExecutionEngine(db, registry)
    execution = engine.submit(
        capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
        input_data={
            "target_entity_id": payload.target_entity_id,
            "target_temperature": payload.target_temperature,
            "target_unit": payload.target_unit,
            "reason": payload.reason,
        },
        caller_permissions={"homeassistant:actuate"},
    )
    return ToolExecutionResponse.model_validate(execution)


@router.get(
    "/climate/execution/{execution_id}",
    response_model=ToolExecutionResponse,
    summary="Inspect approval and verification status of a climate actuation execution",
)
def get_climate_actuation_status(
    execution_id: str,
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """Inspect the execution record, human approval ticket, and independent verification outcome for a climate actuation."""
    repo = ToolExecutionRepository(db)
    execution = repo.get_by_id(execution_id)
    if not execution or execution.capability_id != "homeassistant.allowlisted_climate_setpoint_actuate":
        raise EntityNotFoundError("ClimateActuationExecution", execution_id)
    return ToolExecutionResponse.model_validate(execution)
