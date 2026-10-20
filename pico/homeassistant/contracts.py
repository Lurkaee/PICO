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
