"""Persistent workflow compensation records (Milestone P9.3)."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pico.db.base import Base

if TYPE_CHECKING:
    from pico.models.tool_execution import ToolExecution
    from pico.models.workflow import WorkflowRun, WorkflowStep


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowCompensation(Base):
    """
    Durable record for registered and executed compensation actions.
    Tracks state transitions from registered -> awaiting_approval -> running -> succeeded / failed / uncertain.
    """
    __tablename__ = "workflow_compensations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_step_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_steps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    original_tool_execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tool_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    compensation_capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    compensation_capability_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1.0.0"
    )
    compensation_tool_execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tool_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="registered", index=True
    )  # registered, awaiting_approval, running, succeeded, failed, uncertain, blocked
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    workflow_run: Mapped["WorkflowRun"] = relationship("WorkflowRun")
    workflow_step: Mapped["WorkflowStep"] = relationship("WorkflowStep")
    original_tool_execution: Mapped["ToolExecution | None"] = relationship(
        "ToolExecution", foreign_keys=[original_tool_execution_id]
    )
    compensation_tool_execution: Mapped["ToolExecution | None"] = relationship(
        "ToolExecution", foreign_keys=[compensation_tool_execution_id]
    )
