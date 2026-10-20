from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


class ProviderCapability(str, Enum):
    STREAMING = "STREAMING"
    JSON_SCHEMA = "JSON_SCHEMA"
    TOOL_PROPOSALS = "TOOL_PROPOSALS"
    LOCAL_INFERENCE = "LOCAL_INFERENCE"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    data_classification: str = "INTERNAL"
    metadata: dict[str, Any] | None = None


class GenerationOptions(BaseModel):
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    json_schema: dict[str, Any] | None = None
    stream: bool = True
    think: bool | None = Field(
        default=None,
        description=(
            "Control model internal thinking/reasoning process for reasoning-capable models (e.g. Ollama). "
            "Precedence: GenerationOptions.think takes priority when not None; otherwise provider default is used."
        ),
    )


class MetaEvent(BaseModel):
    type: Literal["meta"] = "meta"
    provider: str
    model: str
    decision_id: str | None = None
    data_classification: str = "INTERNAL"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    delta: str


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    message_id: str | None = None
    finish_reason: str = "stop"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


StreamEvent = MetaEvent | TokenEvent | DoneEvent | ErrorEvent


class ProviderMetadata(BaseModel):
    name: str
    default_model: str
    capabilities: list[ProviderCapability]
    is_local: bool
