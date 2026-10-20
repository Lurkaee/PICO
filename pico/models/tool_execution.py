from datetime import datetime, timezone
from typing import Any
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pico.db.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ToolExecution(Base):
    __tablename__ = "tool_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    capability_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True, index=True
    )
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String(32), nullable=True, default="pending")
    verification_details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    required_permission: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL"
    )
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, index=True
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Relationships
    events: Mapped[list["LedgerEvent"]] = relationship(
        "LedgerEvent",
        back_populates="execution",
        cascade="all, delete-orphan",
    )
