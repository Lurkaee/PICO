from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pico.db.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OperatorPairingChallenge(Base):
    """Short-lived pairing challenge created by trusted local CLI."""

    __tablename__ = "operator_pairing_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    salt: Mapped[str] = mapped_column(String(32), nullable=False)
    approver: Mapped[str] = mapped_column(String(128), nullable=False, default="local_operator")

    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OperatorSessionModel(Base):
    """Authenticated local operator session record."""

    __tablename__ = "operator_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # session_id
    approver: Mapped[str] = mapped_column(String(128), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
