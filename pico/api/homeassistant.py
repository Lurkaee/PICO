from typing import Literal
from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError
from pico.db.session import get_db
from pico.homeassistant.actuation import HomeAssistantActuationService
from pico.homeassistant.contracts import (
    ActuationReadinessReport,
    EntityListOutput,
    EntityOverviewOutput,
    EntityState,
    HomeAssistantStatus,
    TestLightActuationPlan,
)
from pico.homeassistant.readiness import evaluate_actuation_readiness
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
    if not settings.APPROVAL_SECRET_TOKEN or not x_approval_token or x_approval_token != settings.APPROVAL_SECRET_TOKEN:
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


# --- Milestone P7.3 Physical Actuation Operations ---

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
    if not settings.APPROVAL_SECRET_TOKEN or not x_approval_token or x_approval_token != settings.APPROVAL_SECRET_TOKEN:
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
    summary="Inspect approval and verification status of an actuation execution",
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
