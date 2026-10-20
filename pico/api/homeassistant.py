from fastapi import APIRouter, Depends, Header, Query, Request

from pico.core.config import settings
from pico.core.errors import AuthorizationError
from pico.homeassistant.contracts import (
    EntityListOutput,
    EntityOverviewOutput,
    EntityState,
    HomeAssistantStatus,
)
from pico.homeassistant.service import HomeAssistantService
from pico.homeassistant.ws_client import is_loopback_host


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
