import time
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class WorkerRole(str, Enum):
    """Logical task role requesting model inference."""
    INTERACTIVE = "interactive"
    PLANNER = "planner"
    RESEARCHER = "researcher"
    EXECUTOR = "executor"
    SUMMARIZER = "summarizer"
    ROUTINE = "routine"
    VERIFIER = "verifier"


class PriorityClass(str, Enum):
    """Scheduling priority class."""
    INTERACTIVE = "interactive"
    WORKFLOW = "workflow"
    BACKGROUND = "background"


PRIORITY_WEIGHTS: dict[PriorityClass, int] = {
    PriorityClass.INTERACTIVE: 0,
    PriorityClass.WORKFLOW: 10,
    PriorityClass.BACKGROUND: 20,
}


class SchedulingStatus(str, Enum):
    """Lifecycle status of a scheduling ticket."""
    QUEUED = "queued"
    ACQUIRED = "acquired"
    STREAMING = "streaming"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class SchedulingRequest(BaseModel):
    """Typed request submitted to the model resource scheduler."""
    request_id: str
    role: WorkerRole = WorkerRole.INTERACTIVE
    priority: PriorityClass = PriorityClass.INTERACTIVE
    provider: str
    model_name: str
    task_category: str = "general_chat"
    data_classification: str = "INTERNAL"
    prompt_tokens: int | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    queue_timeout_seconds: float | None = None
    workflow_run_id: str | None = None
    step_key: str | None = None
    correlation_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class ExecutionLease(BaseModel):
    """Execution lease granting admission to an active inference slot."""
    lease_id: str
    request_id: str
    provider: str
    model_name: str
    is_local: bool
    acquired_at: float = Field(default_factory=time.time)


class SchedulingOutcomeSummary(BaseModel):
    """Sanitized diagnostic summary of a completed or terminated request."""
    request_id: str
    role: WorkerRole
    priority: PriorityClass
    provider: str
    model_name: str
    data_classification: str
    status: SchedulingStatus
    queue_wait_ms: float = 0.0
    inference_duration_ms: float = 0.0
    completion_tokens: int | None = None
    error_message: str | None = None
    workflow_run_id: str | None = None
    step_key: str | None = None
    timestamp: float = Field(default_factory=time.time)


class SchedulerTelemetry(BaseModel):
    """System RAM and GPU VRAM hardware telemetry snapshot."""
    system_ram_total_mb: float | None = None
    system_ram_available_mb: float | None = None
    gpu_available: bool = False
    gpu_name: str | None = None
    gpu_vram_total_mb: float | None = None
    gpu_vram_used_mb: float | None = None
    gpu_telemetry_source: str = "unavailable"
    gpu_error_reason: str | None = None


class SchedulerStatus(BaseModel):
    """Operational status and diagnostics of the model resource scheduler."""
    enabled: bool = True
    active_local_profile: str
    active_local_model: str
    max_active_local: int = 1
    current_active_local: int = 0
    max_active_cloud: int = 5
    current_active_cloud: int = 0
    queue_depth: int = 0
    max_queue_depth: int = 50
    queue_by_priority: dict[str, int] = Field(default_factory=dict)
    counts_by_status: dict[str, int] = Field(default_factory=dict)
    telemetry: SchedulerTelemetry
    recent_outcomes: list[SchedulingOutcomeSummary] = Field(default_factory=list)
