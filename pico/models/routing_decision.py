from datetime import datetime, timezone
from typing import Any
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pico.db.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RoutingDecision(Base):
    __tablename__ = "routing_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_category: Mapped[str] = mapped_column(String(64), nullable=False, default="general_chat")
    data_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERNAL"
    )
    selected_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_rule: Mapped[str] = mapped_column(String(255), nullable=False)
    cloud_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fallback_occurred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, default="success", index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost_cents: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
