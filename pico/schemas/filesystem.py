"""Pydantic schemas for filesystem staged diff API."""

from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class StageDiffRequest(BaseModel):
    root_id: str = Field(..., description="Allowlisted filesystem root identifier")
    relative_path: str = Field(..., description="Workspace-relative path to target text file")
    operation: str = Field(..., description="Operation: 'create' or 'replace'")
    proposed_content: str = Field(..., description="Proposed UTF-8 text file content")


class StagedDiffResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    root_id: str
    relative_path: str
    operation: str
    original_state: str
    proposed_digest: str
    diff_digest: str
    unified_diff: str
    status: str
    execution_id: str | None = None
    applied_at: datetime | None = None
    verified_at: datetime | None = None
    verified_digest: str | None = None
    rolled_back_at: datetime | None = None
    metadata_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
