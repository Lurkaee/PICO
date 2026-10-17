from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from pico.schemas.common import PaginatedResponse

MessageRole = Literal["user", "assistant", "system", "tool"]
DataClassification = Literal["PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"]


class MessageCreate(BaseModel):
    role: MessageRole = Field(description="Role of the message sender")
    content: str = Field(min_length=1, description="Message text content")
    data_classification: DataClassification = Field(
        default="INTERNAL",
        description="Privacy classification level: PUBLIC, INTERNAL, SENSITIVE, SECRET",
    )
    token_count: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] | None = Field(default=None)


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: MessageRole
    content: str
    data_classification: DataClassification
    token_count: int | None = None
    created_at: datetime
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class MessageListResponse(PaginatedResponse[MessageResponse]):
    pass
