from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from pico.schemas.common import PaginatedResponse

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TaskPriority = Literal["low", "medium", "high", "urgent"]


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255, description="Task title")
    description: str | None = Field(default=None, description="Task description")
    project_id: str | None = Field(default=None, description="Linked project ID")
    conversation_id: str | None = Field(default=None, description="Originating conversation ID")
    status: TaskStatus = Field(default="pending", description="Task workflow status")
    priority: TaskPriority = Field(default="medium", description="Task priority")
    due_date: datetime | None = Field(default=None, description="Due date timestamp")
    metadata: dict[str, Any] | None = Field(default=None)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None)
    project_id: str | None = Field(default=None)
    conversation_id: str | None = Field(default=None)
    status: TaskStatus | None = Field(default=None)
    priority: TaskPriority | None = Field(default=None)
    due_date: datetime | None = Field(default=None)
    metadata: dict[str, Any] | None = Field(default=None)


class TaskResponse(BaseModel):
    id: str
    project_id: str | None = None
    conversation_id: str | None = None
    title: str
    description: str | None = None
    status: TaskStatus
    priority: TaskPriority
    due_date: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class TaskListResponse(PaginatedResponse[TaskResponse]):
    pass
