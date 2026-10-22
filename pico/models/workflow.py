"""Persistent workflow coordination models (Milestone P9.1)."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pico.db.base import Base

if TYPE_CHECKING:
    from pico.models.task import Task
    from pico.models.tool_execution import ToolExecution


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRun(Base):
    """
    Durable workflow run entity representing one execution of a coordinated multi-step plan.
    """
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )  # queued, running, awaiting_approval, succeeded, failed, cancelled, blocked
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL"
    )
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    task: Mapped["Task | None"] = relationship("Task")
    steps: Mapped[list["WorkflowStep"]] = relationship(
        "WorkflowStep",
        back_populates="workflow_run",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.order_index",
    )


class WorkflowStep(Base):
    """
    Individual delegated unit of work within a workflow run.
    """
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "step_key", name="uq_workflow_steps_run_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    capability_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )  # pending, ready, running, awaiting_approval, succeeded, failed, cancelled, blocked, verification_failed
    dependencies_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    input_template_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    resolved_input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tool_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    workflow_run: Mapped["WorkflowRun"] = relationship("WorkflowRun", back_populates="steps")
    tool_execution: Mapped["ToolExecution | None"] = relationship("ToolExecution")
