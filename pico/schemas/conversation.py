from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from pico.schemas.common import PaginatedResponse


class ConversationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255, description="Conversation title")
    metadata: dict[str, Any] | None = Field(default=None, description="Optional metadata")


class ConversationResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class ConversationListResponse(PaginatedResponse[ConversationResponse]):
    pass
