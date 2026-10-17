from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

TaskCategoryType = Literal[
    "intent_classification",
    "entity_extraction",
    "summarization",
    "planning",
    "coding",
    "verification",
    "general_chat",
]


class RoutingDecisionCreate(BaseModel):
    conversation_id: str | None = None
    message_id: str | None = None
    step_index: int = 0
    task_category: str = "general_chat"
    data_classification: str = "INTERNAL"
    selected_profile: str
    provider: str
    model_name: str
    decision_rule: str
    cloud_approved: bool = False
    fallback_occurred: bool = False
    outcome: str = "success"
    error_message: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float | None = None
    estimated_cost_cents: float | None = None
    metadata: dict[str, Any] | None = None


class RoutingDecisionResponse(BaseModel):
    id: str
    conversation_id: str | None = None
    message_id: str | None = None
    step_index: int
    task_category: str
    data_classification: str
    selected_profile: str
    provider: str
    model_name: str
    decision_rule: str
    cloud_approved: bool
    fallback_occurred: bool
    outcome: str
    error_message: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float | None = None
    estimated_cost_cents: float | None = None
    created_at: datetime
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)
