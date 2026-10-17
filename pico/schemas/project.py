from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from pico.schemas.common import PaginatedResponse

ProjectStatus = Literal["active", "archived", "completed"]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="Project unique name")
    description: str | None = Field(default=None, description="Detailed project description")
    status: ProjectStatus = Field(default="active", description="Project status")
    metadata: dict[str, Any] | None = Field(default=None)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None)
    status: ProjectStatus | None = Field(default=None)
    metadata: dict[str, Any] | None = Field(default=None)


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class ProjectListResponse(PaginatedResponse[ProjectResponse]):
    pass
