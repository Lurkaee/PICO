from typing import Literal
from pydantic import BaseModel, Field

from pico.schemas.message import DataClassification


class ChatStreamRequest(BaseModel):
    conversation_id: str = Field(description="Target conversation ID")
    message: str = Field(min_length=1, description="User message content")
    data_classification: DataClassification | None = Field(
        default="INTERNAL",
        description="Data classification level (PUBLIC, INTERNAL, SENSITIVE, SECRET). Defaults to INTERNAL.",
    )
    task_category: str | None = Field(
        default="general_chat",
        description="Task category for routing (e.g. general_chat, summarization, planning, coding).",
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    requested_profile: str | None = Field(
        default=None,
        description="Optional preferred profile (e.g. 'local-small', 'cloud-strong'). Subject to routing policy.",
    )
