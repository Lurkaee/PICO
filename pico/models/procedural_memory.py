from datetime import datetime, timezone
from typing import Any
from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pico.db.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProceduralMemoryItem(Base):
    __tablename__ = "procedural_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", index=True
    )
    preconditions_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    required_capabilities_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    steps_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    expected_effects_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    verification_criteria_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL"
    )
    source_provenance: Mapped[str] = mapped_column(
        String(255), nullable=False, default="user:manual_procedure"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
