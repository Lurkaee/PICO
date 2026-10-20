from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from pico.schemas.common import PaginatedResponse

DataClassification = Literal["PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"]
ProceduralLifecycleStatus = Literal["draft", "reviewed", "approved", "active", "retired"]


# --- Working Memory ---
class WorkingMemoryCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, description="Session or conversation identifier")
    key: str = Field(min_length=1, max_length=128, description="Key identifying the state entry")
    content: str = Field(min_length=1, description="Content or summary payload")
    item_type: str = Field(default="working_state", max_length=32)
    task_id: str | None = Field(default=None, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    data_classification: DataClassification = Field(default="INTERNAL")
    source_provenance: str = Field(default="session:working_memory", max_length=255)
    ttl_seconds: int | None = Field(default=None, ge=1, description="Optional time-to-live in seconds")
    metadata: dict[str, Any] | None = Field(default=None)


class WorkingMemoryResponse(BaseModel):
    id: str
    session_id: str
    key: str
    item_type: str
    content: str
    task_id: str | None = None
    project_id: str | None = None
    data_classification: str
    source_provenance: str
    is_active: bool
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="metadata_json")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# --- Episodic Memory ---
class EpisodicMemoryCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    occurred_at: datetime | None = None
    task_id: str | None = Field(default=None, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    data_classification: DataClassification = Field(default="INTERNAL")
    source_provenance: str = Field(min_length=1, max_length=255)
    is_inferred: bool = Field(default=False)
    deduplication_key: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] | None = Field(default=None)


class EpisodicMemoryResponse(BaseModel):
    id: str
    event_type: str
    description: str
    occurred_at: datetime
    task_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    data_classification: str
    source_provenance: str
    is_inferred: bool
    deduplication_key: str | None = None
    created_at: datetime
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="metadata_json")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class EpisodicMemoryListResponse(PaginatedResponse[EpisodicMemoryResponse]):
    pass


# --- Semantic Memory ---
class SemanticMemoryCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    predicate: str = Field(min_length=1, max_length=128)
    object: str | None = Field(default=None, max_length=255)
    content: str = Field(min_length=1, description="Factual statement text")
    entity_type: str = Field(default="fact", max_length=64)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    is_verified: bool = Field(default=False)
    data_classification: DataClassification = Field(default="INTERNAL")
    source_provenance: str = Field(min_length=1, max_length=255)
    project_id: str | None = Field(default=None, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    metadata: dict[str, Any] | None = Field(default=None)


class SemanticMemoryUpdate(BaseModel):
    content: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    is_verified: bool | None = None
    conflict_status: str | None = None
    data_classification: DataClassification | None = None
    metadata: dict[str, Any] | None = None


class SemanticMemoryResponse(BaseModel):
    id: str
    entity_type: str
    subject: str
    predicate: str
    object: str | None = None
    content: str
    confidence: float
    is_verified: bool
    conflict_status: str
    data_classification: str
    source_provenance: str
    project_id: str | None = None
    task_id: str | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="metadata_json")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class SemanticMemoryListResponse(PaginatedResponse[SemanticMemoryResponse]):
    pass


class SemanticMemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=255)
    limit: int = Field(default=10, ge=1, le=50)
    entity_type: str | None = None
    project_id: str | None = None


# --- Procedural Memory ---
class ProceduralMemoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", max_length=32)
    description: str = Field(min_length=1)
    preconditions: list[str] | None = Field(default=None)
    required_capabilities: list[str] | None = Field(default=None)
    steps: list[dict[str, Any]] = Field(min_length=1, description="Ordered operational steps")
    expected_effects: list[str] | None = Field(default=None)
    verification_criteria: list[str] | None = Field(default=None)
    data_classification: DataClassification = Field(default="INTERNAL")
    source_provenance: str = Field(default="user:manual_procedure", max_length=255)
    metadata: dict[str, Any] | None = Field(default=None)


class ProceduralMemoryUpdate(BaseModel):
    description: str | None = None
    preconditions: list[str] | None = None
    required_capabilities: list[str] | None = None
    steps: list[dict[str, Any]] | None = None
    expected_effects: list[str] | None = None
    verification_criteria: list[str] | None = None
    metadata: dict[str, Any] | None = None


class ProceduralMemoryApprovalRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=128, description="Human reviewer ID")
    notes: str | None = Field(default=None, description="Approval audit notes")


class ProceduralMemoryResponse(BaseModel):
    id: str
    name: str
    version: str
    description: str
    lifecycle_status: ProceduralLifecycleStatus
    steps: list[dict[str, Any]] = Field(validation_alias="steps_json")
    preconditions: list[str] | None = Field(default=None, validation_alias="preconditions_json")
    required_capabilities: list[str] | None = Field(default=None, validation_alias="required_capabilities_json")
    expected_effects: list[str] | None = Field(default=None, validation_alias="expected_effects_json")
    verification_criteria: list[str] | None = Field(default=None, validation_alias="verification_criteria_json")
    approved_by: str | None = None
    approved_at: datetime | None = None
    retired_at: datetime | None = None
    data_classification: str
    source_provenance: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="metadata_json")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ProceduralMemoryListResponse(PaginatedResponse[ProceduralMemoryResponse]):
    pass


# --- System Status ---
class MemoryStatusResponse(BaseModel):
    working_memory_active_items: int
    episodic_memory_total_events: int
    semantic_memory_total_facts: int
    semantic_memory_verified_facts: int
    procedural_memory_total: int
    procedural_memory_active: int
    storage_type: str = "sqlite_fts5"
