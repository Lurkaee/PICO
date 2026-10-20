import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.event_ledger import LedgerEvent
from pico.repositories.base import BaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_SENSITIVE_KEY_SUBSTRINGS = {"password", "token", "secret", "api_key", "bearer", "credential"}


def redact_payload(obj: Any) -> Any:
    """Recursively redacts sensitive keys from event ledger payloads."""
    if isinstance(obj, dict):
        redacted = {}
        for k, v in obj.items():
            if any(sub in k.lower() for sub in _SENSITIVE_KEY_SUBSTRINGS):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_payload(v)
        return redacted
    elif isinstance(obj, list):
        return [redact_payload(item) for item in obj]
    return obj


class EventLedgerRepository(BaseRepository[LedgerEvent]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, item_id: str) -> LedgerEvent | None:
        stmt = select(LedgerEvent).where(LedgerEvent.id == item_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def append(
        self,
        event_type: str,
        execution_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        capability_id: str | None = None,
        capability_version: str | None = None,
        payload_json: dict[str, Any] | None = None,
        data_classification: str = "INTERNAL",
    ) -> LedgerEvent:
        now = utc_now()
        safe_payload = redact_payload(payload_json) if payload_json else None
        item = LedgerEvent(
            id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=execution_id,
            task_id=task_id,
            correlation_id=correlation_id,
            event_type=event_type,
            capability_id=capability_id,
            capability_version=capability_version,
            payload_json=safe_payload,
            data_classification=data_classification,
            occurred_at=now,
            created_at=now,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def list_events(
        self,
        execution_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LedgerEvent]:
        stmt = select(LedgerEvent)
        if execution_id is not None:
            stmt = stmt.where(LedgerEvent.execution_id == execution_id)
        if task_id is not None:
            stmt = stmt.where(LedgerEvent.task_id == task_id)
        if correlation_id is not None:
            stmt = stmt.where(LedgerEvent.correlation_id == correlation_id)
        if event_type is not None:
            stmt = stmt.where(LedgerEvent.event_type == event_type)

        stmt = stmt.order_by(LedgerEvent.occurred_at.asc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count(self, execution_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(LedgerEvent)
        if execution_id is not None:
            stmt = stmt.where(LedgerEvent.execution_id == execution_id)
        return self.db.execute(stmt).scalar_one()
