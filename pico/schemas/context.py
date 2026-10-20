from typing import Any, Literal
from pydantic import BaseModel, Field

from pico.providers.types import ChatMessage


class ContextItem(BaseModel):
    """A typed item of context with cryptographic/relational provenance and classification."""

    id: str = Field(description="Unique identifier for the context item")
    item_type: str = Field(
        description="Type of context item: 'system_prompt', 'privacy_rule', 'safety_directive', 'conversation_message', 'project_context', 'task_context', 'memory_fact', 'sensor_event'"
    )
    role: Literal["system", "user", "assistant", "tool"] = Field(
        default="user", description="Role for conversational model consumption"
    )
    content: str = Field(description="Raw text content of the context item")
    source_provenance: str = Field(
        description="Relational or cryptographic source URI (e.g., 'msg:msg_123', 'proj:proj_456', 'system:core')"
    )
    data_classification: str | None = Field(
        default="INTERNAL",
        description="Canonical classification: 'PUBLIC', 'INTERNAL', 'SENSITIVE', 'SECRET'",
    )
    priority: int = Field(
        default=50,
        description="Inclusion priority during budget pressure (higher priority is included first)",
    )
    is_mandatory: bool = Field(
        default=False,
        description="Whether this item is a mandatory instruction (e.g. system or privacy rule) that must survive history pruning",
    )
    token_count: int = Field(
        default=0,
        description="Estimated or measured token footprint of this item",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured metadata associated with this item",
    )


class SelectionDecision(BaseModel):
    """Audit record of a selection decision made by the Context Compiler."""

    item_id: str
    item_type: str
    source_provenance: str
    data_classification: str
    token_count: int
    action: Literal["included", "truncated", "excluded"]
    reason: str


class CompiledContext(BaseModel):
    """The final assembled, budgeted, and classified context ready for model inference."""

    messages: list[ChatMessage] = Field(
        description="Ordered chat messages formatted for provider consumption"
    )
    items: list[ContextItem] = Field(
        description="List of typed context items included in this compiled prompt"
    )
    total_tokens: int = Field(description="Total token footprint of all included items plus framing")
    content_tokens: int = Field(default=0, description="Token footprint of the message contents alone")
    framing_tokens: int = Field(default=0, description="ChatML / template envelope overhead tokens")
    token_budget: int = Field(description="Active effective prompt token budget limit enforced")
    context_window: int = Field(default=4096, description="Model provider maximum context window limit")
    completion_reserve: int = Field(default=512, description="Tokens reserved for model completion output")
    safety_headroom: int = Field(default=256, description="Headroom reserved for tokenizer variance and safety")
    token_counting_method: str = Field(
        description="Documented token counting method used"
    )
    is_exact_count: bool = Field(
        default=False,
        description="Whether the complete compiled provider request token count is exact (True) or includes estimated framing/bounds (False). Note: estimated message envelope framing overhead renders compiled chat requests False.",
    )
    bound_type: str = Field(
        default="utf8_byte_upper_bound",
        description="Nature of the count: 'exact_content_estimated_framing', 'utf8_byte_upper_bound', or 'calibrated_estimate'",
    )
    chat_template: str = Field(
        default="chatml",
        description="Chat template envelope assumed for framing overhead calculation",
    )
    budget_strategy: str = Field(
        default="conservative-prompt-budget-v1",
        description="Documented budgeting strategy name",
    )
    effective_classification: str = Field(
        description="Effective classification across all included items (highest severity dominates)"
    )
    included_items_count: int = Field(description="Count of context items included in prompt")
    excluded_items_count: int = Field(description="Count of context items excluded or truncated")
    selection_decisions: list[SelectionDecision] = Field(
        default_factory=list,
        description="Audit trace of all item inclusion/exclusion decisions",
    )
