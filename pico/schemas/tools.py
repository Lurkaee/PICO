from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class CapabilityResponse(BaseModel):
    capability_id: str
    version: str
    name: str
    description: str
    risk_tier: str
    required_permission: str | None
    required_classification: str
    timeout_seconds: float
    max_output_bytes: int
    idempotent: bool
    max_retries: int
    consequential: bool
    preconditions: list[str]
    expected_effects: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class ToolExecutionCreate(BaseModel):
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(default="1.0.0", max_length=32)
    input_data: dict[str, Any]
    task_id: str | None = None
    session_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] | None = None


class ToolExecutionResponse(BaseModel):
    id: str
    task_id: str | None = None
    session_id: str | None = None
    capability_id: str
    capability_version: str
    status: str
    idempotency_key: str | None = None
    input_digest: str
    input_data: dict[str, Any] = Field(validation_alias="input_json")
    output_data: dict[str, Any] | None = Field(default=None, validation_alias="output_json")
    error_message: str | None = None
    error_details: dict[str, Any] | None = Field(default=None, validation_alias="error_details_json")
    verification_status: str | None = None
    verification_details: dict[str, Any] | None = Field(default=None, validation_alias="verification_details_json")
    retry_count: int
    max_retries: int
    timeout_seconds: float
    required_permission: str | None = None
    data_classification: str
    approved_by: str | None = None
    approved_at: datetime | None = None
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="metadata_json")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ToolExecutionListResponse(BaseModel):
    items: list[ToolExecutionResponse]
    total: int
    limit: int
    offset: int


class ToolApprovalRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=128)
    notes: str | None = None


class LedgerEventResponse(BaseModel):
    id: str
    execution_id: str | None = None
    task_id: str | None = None
    correlation_id: str | None = None
    event_type: str
    capability_id: str | None = None
    capability_version: str | None = None
    payload: dict[str, Any] | None = Field(default=None, validation_alias="payload_json")
    data_classification: str
    occurred_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class LedgerEventListResponse(BaseModel):
    items: list[LedgerEventResponse]
    total: int
    limit: int
    offset: int


class ProcedureValidationResponse(BaseModel):
    procedure_id: str
    is_valid: bool
    total_steps: int
    resolved_capabilities: list[str]
    missing_capabilities: list[str]
    requires_approval: bool
    details: list[dict[str, Any]]
