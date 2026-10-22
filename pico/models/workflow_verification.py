"""Persistent workflow verification records (Milestone P9.3)."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pico.db.base import Base

if TYPE_CHECKING:
    from pico.models.tool_execution import ToolExecution
    from pico.models.workflow import WorkflowRun, WorkflowStep


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowVerification(Base):
    """
    Durable verification decision and evidence record for a workflow step output.
    Audits structural integrity, execution evidence, semantic correctness, and policy decision.
    """
    __tablename__ = "workflow_verifications"

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
    tool_execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tool_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    verification_method: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # passed, failed, inconclusive, requires_review
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # stage_a_structural, stage_b_evidence, stage_c_semantic, stage_d_decision
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    output_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    findings_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL"
    )

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
    tool_execution: Mapped["ToolExecution | None"] = relationship("ToolExecution")
