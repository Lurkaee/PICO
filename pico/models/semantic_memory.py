from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pico.db.base import Base

if TYPE_CHECKING:
    from pico.models.project import Project
    from pico.models.task import Task


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SemanticMemoryItem(Base):
    __tablename__ = "semantic_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="fact", index=True
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    object: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conflict_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="clean"
    )
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL"
    )
    source_provenance: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
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
