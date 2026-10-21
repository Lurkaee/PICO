"""Contracts, schemas, and data models for OS and desktop automation."""

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class WindowState(str, Enum):
    VISIBLE = "visible"
    MINIMIZED = "minimized"
    FOREGROUND = "foreground"
    HIDDEN = "hidden"


class WindowMetadata(BaseModel):
    handle: int | str = Field(description="Opaque window handle or identifier")
    title: str = Field(max_length=512, description="Sanitized window title")
    process_id: int | None = Field(default=None, description="PID of associated process if observable")
    is_visible: bool = Field(default=True, description="Whether window is visible")
    is_minimized: bool = Field(default=False, description="Whether window is currently minimized (iconic)")
    is_foreground: bool = Field(default=False, description="Whether window currently has foreground focus")

    model_config = ConfigDict(extra="ignore")


class WindowInspectionResult(BaseModel):
    status: str = Field(description="Inspection status: 'success', 'unavailable', or 'unsupported'")
    platform: str = Field(description="Operating system identifier (e.g. 'windows', 'linux', 'darwin')")
    windows: list[WindowMetadata] = Field(default_factory=list, description="Bounded list of enumerated windows")
    total_windows_found: int = Field(default=0, description="Total matching windows discovered before bounding")
    truncated: bool = Field(default=False, description="Whether the window list was truncated to fit limit")
    message: str | None = Field(default=None, description="Diagnostic message or reason if unavailable/unsupported")

    model_config = ConfigDict(extra="ignore")


class LaunchVerificationStatus(str, Enum):
    REQUEST_REJECTED = "request_rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    LAUNCH_DISPATCHED = "launch_dispatched"
    PROCESS_CREATION_CONFIRMED = "process_creation_confirmed"
    LAUNCH_UNCERTAIN = "launch_uncertain"
    LAUNCH_FAILED = "launch_failed"
    INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED = "independent_window_observation_confirmed"


class LaunchProfile(BaseModel):
    alias: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    executable_path: str = Field(min_length=1, max_length=512)
    args: list[str] = Field(default_factory=list)
    working_dir: str | None = None
    description: str | None = None

    model_config = ConfigDict(extra="ignore")


class ApplicationLaunchResult(BaseModel):
    alias: str
    status: LaunchVerificationStatus
    executable_path: str
    process_id: int | None = None
    verification_status: str
    verification_details: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None

    model_config = ConfigDict(extra="ignore")
