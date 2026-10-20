from datetime import datetime, timezone
from typing import Any
from sqlalchemy import Boolean, DateTime, Float, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from pico.db.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PerceptionEvent(Base):
    __tablename__ = "perception_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    modality: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL", index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    raw_payload_scrubbed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    structured_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provenance: Mapped[str] = mapped_column(String(255), nullable=False)
    retention_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="short_term_audit"
    )
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    parent_event_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
