from fastapi import APIRouter, Query

from pico.homeassistant.contracts import (
    EntityListOutput,
    EntityOverviewOutput,
    EntityState,
    HomeAssistantStatus,
)
from pico.homeassistant.service import HomeAssistantService

router = APIRouter(prefix="/api/homeassistant", tags=["Home Assistant"])


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
