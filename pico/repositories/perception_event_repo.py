from datetime import datetime, timezone
from typing import TYPE_CHECKING
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pico.models.perception_event import PerceptionEvent
from pico.repositories.base import BaseRepository

if TYPE_CHECKING:
    from pico.perception.contracts import NormalizedPerceptionEvent


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PerceptionEventRepository(BaseRepository[PerceptionEvent]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def create(self, event: "NormalizedPerceptionEvent") -> PerceptionEvent:
        db_event = PerceptionEvent(
            id=event.event_id,
            correlation_id=event.correlation_id,
            modality=event.modality.value if hasattr(event.modality, "value") else str(event.modality),
            event_type=event.event_type,
            source_device_id=event.source_device_id,
            data_classification=event.data_classification,
            confidence=event.confidence,
            raw_payload_scrubbed=event.raw_payload_scrubbed,
            structured_data=event.structured_data,
            provenance=event.provenance,
            retention_policy=event.retention_policy.value if hasattr(event.retention_policy, "value") else str(event.retention_policy),
            session_id=event.session_id,
            parent_event_ids=event.parent_event_ids,
            created_at=event.timestamp or utc_now(),
        )
        self.db.add(db_event)
        self.db.commit()
        self.db.refresh(db_event)
        return db_event

    def get_by_id(self, event_id: str) -> PerceptionEvent | None:
        stmt = select(PerceptionEvent).where(PerceptionEvent.id == event_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_events(
        self,
        modality: str | None = None,
        event_type: str | None = None,
        source_device_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PerceptionEvent]:
        stmt = select(PerceptionEvent)
        if modality:
            stmt = stmt.where(PerceptionEvent.modality == modality)
        if event_type:
            stmt = stmt.where(PerceptionEvent.event_type == event_type)
        if source_device_id:
            stmt = stmt.where(PerceptionEvent.source_device_id == source_device_id)
        if session_id:
            stmt = stmt.where(PerceptionEvent.session_id == session_id)

        stmt = stmt.order_by(PerceptionEvent.created_at.desc()).offset(offset).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def count(
        self,
        modality: str | None = None,
        event_type: str | None = None,
        source_device_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        stmt = select(func.count(PerceptionEvent.id))
        if modality:
            stmt = stmt.where(PerceptionEvent.modality == modality)
        if event_type:
            stmt = stmt.where(PerceptionEvent.event_type == event_type)
        if source_device_id:
            stmt = stmt.where(PerceptionEvent.source_device_id == source_device_id)
        if session_id:
            stmt = stmt.where(PerceptionEvent.session_id == session_id)

        return self.db.execute(stmt).scalar_one() or 0

    def get_recent(
        self,
        seconds: float,
        modality: str | None = None,
        limit: int = 50,
    ) -> list[PerceptionEvent]:
        from datetime import timedelta
        cutoff = utc_now() - timedelta(seconds=seconds)
        stmt = select(PerceptionEvent).where(PerceptionEvent.created_at >= cutoff)
        if modality:
            stmt = stmt.where(PerceptionEvent.modality == modality)
        stmt = stmt.order_by(PerceptionEvent.created_at.desc()).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def prune_older_than(self, cutoff: datetime) -> int:
        stmt = delete(PerceptionEvent).where(PerceptionEvent.created_at < cutoff)
        res = self.db.execute(stmt)
        self.db.commit()
        return res.rowcount
