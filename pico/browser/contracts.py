"""Contracts and data models for sandboxed browser automation (Milestone P8.3)."""

from enum import Enum
from pydantic import BaseModel, ConfigDict, Field


class NavigationStatus(str, Enum):
    """Truthful navigation outcome statuses."""
    SUCCESS = "success"
    REJECTED = "rejected"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


class PageLink(BaseModel):
    """Sanitized, bounded link discovered on a page."""
    url: str = Field(description="Sanitized absolute URL")
    text: str = Field(description="Bounded anchor text")

    model_config = ConfigDict(extra="ignore")


class PageObservation(BaseModel):
    """
    Bounded, structured observation of a web page.
    Classified strictly as INTERNAL; external web content is treated as untrusted data.
    """
    status: NavigationStatus = Field(description="Truthful navigation outcome status")
    requested_url: str = Field(description="Original requested URL")
    final_url: str | None = Field(default=None, description="Validated final URL after approved redirects")
    title: str | None = Field(default=None, description="Page title, bounded to max length")
    text_content: str = Field(default="", description="Sanitized, bounded extracted text content")
    links: list[PageLink] = Field(default_factory=list, description="Sanitized, bounded links discovered on page")
    truncated: bool = Field(default=False, description="Whether text content or links were truncated to fit bounds")
    classification: str = Field(default="INTERNAL", description="Data classification tag (strictly INTERNAL)")
    message: str | None = Field(default=None, description="Diagnostic message or reason if rejected/failed/unavailable")

    model_config = ConfigDict(extra="ignore")


class OpenPageRequest(BaseModel):
    """Parameters for opening and inspecting a page."""
    url: str = Field(min_length=1, max_length=2048, description="Target URL to navigate to")
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0, description="Navigation timeout in seconds")
    max_bytes: int = Field(default=65536, ge=1024, le=262144, description="Maximum extracted text bytes")
    max_links: int = Field(default=50, ge=0, le=200, description="Maximum links to extract")

    model_config = ConfigDict(extra="forbid")
