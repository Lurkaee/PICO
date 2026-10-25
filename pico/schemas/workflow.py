"""Pydantic schemas for workflow coordination (Milestone P9.1)."""

from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class StepDefinition(BaseModel):
    """Specification of an individual delegated step in a workflow plan."""
    step_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        description="Unique identifier for the step within this workflow",
    )
    capability_id: str = Field(
        min_length=1,
        max_length=128,
        description="Registered capability identifier to execute",
    )
    capability_version: str = Field(
        default="1.0.0",
        max_length=32,
        description="Expected capability version",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="List of step_keys that must successfully complete before this step can run",
    )
    input_template: dict[str, Any] = Field(
        default_factory=dict,
        description="Input parameters or dependency output mappings for the capability",
    )

    model_config = ConfigDict(extra="forbid")


class WorkflowCreate(BaseModel):
    """Payload to define and submit a new coordinated workflow."""
    name: str = Field(min_length=1, max_length=255, description="Human-readable workflow name")
    description: str | None = Field(default=None, description="Optional workflow summary")
    task_id: str | None = Field(default=None, max_length=64, description="Optional parent task ID")
    session_id: str | None = Field(default=None, max_length=64, description="Optional session ID")
    correlation_id: str | None = Field(default=None, max_length=64, description="Optional correlation ID")
    idempotency_key: str | None = Field(default=None, max_length=128, description="Optional idempotency key")
    data_classification: str = Field(default="INTERNAL", max_length=32, description="Data sensitivity level")
    steps: list[StepDefinition] = Field(
        min_length=1,
        max_length=50,
        description="Ordered or DAG steps composing this workflow",
    )
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary workflow metadata")

    model_config = ConfigDict(extra="forbid")


class WorkflowVerificationResponse(BaseModel):
    """Auditable verification outcome for an individual workflow step."""
    id: str
    workflow_run_id: str
    workflow_step_id: str
    tool_execution_id: str | None = None
    policy_id: str
    policy_version: str
    verification_method: str
    status: str
    stage: str
    input_digest: str
    output_digest: str
    findings: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    reason: str | None = None
    data_classification: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowCompensationResponse(BaseModel):
    """Representation of an explicit capability compensation record."""
    id: str
    workflow_run_id: str
    workflow_step_id: str
    original_tool_execution_id: str | None = None
    compensation_capability_id: str
    compensation_capability_version: str
    compensation_tool_execution_id: str | None = None
    status: str
    input_digest: str
    order_index: int = 0
    reason: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowStepResponse(BaseModel):
    """Representation of an individual workflow step and its execution status."""
    id: str
    workflow_run_id: str
    step_key: str
    capability_id: str
    capability_version: str
    status: str
    dependencies: list[str] = Field(default_factory=list)
    input_template: dict[str, Any] = Field(default_factory=dict)
    resolved_input: dict[str, Any] | None = None
    tool_execution_id: str | None = None
    output: dict[str, Any] | None = None
    error_message: str | None = None
    order_index: int = 0
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    verification: WorkflowVerificationResponse | None = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowRunResponse(BaseModel):
    """Full representation of a workflow run and its composed steps."""
    id: str
    task_id: str | None = None
    session_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    name: str
    description: str | None = None
    status: str
    data_classification: str
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    error_message: str | None = None
    metadata: dict[str, Any] | None = None
    steps: list[WorkflowStepResponse] = Field(default_factory=list)
    compensations: list[WorkflowCompensationResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowAdvanceRequest(BaseModel):
    """Parameters to trigger or advance execution of ready workflow steps."""
    caller_permissions: list[str] = Field(
        default_factory=list,
        description="Client-supplied permission field; ignored by server-side authorization engine to enforce secure server-derived privileges",
    )
    max_steps: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of ready steps to dispatch in this advancement invocation",
    )

    model_config = ConfigDict(extra="forbid")


class WorkflowCancelRequest(BaseModel):
    """Cancellation request for a workflow run."""
    reason: str | None = Field(default=None, max_length=512, description="Reason for cancellation")

    model_config = ConfigDict(extra="forbid")


class WorkflowCompensateRequest(BaseModel):
    """Parameters to trigger reverse-dependency compensation for a workflow run."""
    caller_permissions: list[str] = Field(
        default_factory=list,
        description="Client-supplied permission field; ignored by server-side authorization engine to enforce secure server-derived privileges",
    )
    reason: str | None = Field(
        default=None,
        max_length=512,
        description="Operator reason for triggering workflow compensation",
    )

    model_config = ConfigDict(extra="forbid")


class WorkflowListResponse(BaseModel):
    """Paginated list of workflow runs."""
    items: list[WorkflowRunResponse]
    total: int
    limit: int
    offset: int

    model_config = ConfigDict(from_attributes=True)
