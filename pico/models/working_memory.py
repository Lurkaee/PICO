from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pico.db.base import Base

if TYPE_CHECKING:
    from pico.models.project import Project
    from pico.models.task import Task


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkingMemoryItem(Base):
    __tablename__ = "working_memory_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    item_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="working_state"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL"
    )
    source_provenance: Mapped[str] = mapped_column(
        String(255), nullable=False, default="session:working_memory"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Relationships
    project: Mapped["Project | None"] = relationship("Project")
    task: Mapped["Task | None"] = relationship("Task")
