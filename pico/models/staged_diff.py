from datetime import datetime, timezone
from typing import Any
from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pico.db.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StagedDiff(Base):
    """
    Persistent staged filesystem diff proposal.
    Tracks proposed changes, original file digests, unified diffs,
    approval binding, execution outcomes, and rollback metadata.
    """
    __tablename__ = "staged_diffs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    root_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)  # "create" or "replace"
    original_state: Mapped[str] = mapped_column(String(64), nullable=False)  # "ABSENT" or 64-char sha256
    proposed_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    diff_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    unified_diff: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_content: Mapped[str] = mapped_column(Text, nullable=False)
    original_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="staged", index=True
    )  # "staged", "applied", "rolled_back", "discarded"
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, index=True
    )
