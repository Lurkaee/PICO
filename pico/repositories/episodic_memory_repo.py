import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.episodic_memory import EpisodicMemoryItem
from pico.repositories.base import BaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EpisodicMemoryRepository(BaseRepository[EpisodicMemoryItem]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, item_id: str) -> EpisodicMemoryItem | None:
        stmt = select(EpisodicMemoryItem).where(EpisodicMemoryItem.id == item_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_deduplication_key(self, dedup_key: str) -> EpisodicMemoryItem | None:
        stmt = select(EpisodicMemoryItem).where(
            EpisodicMemoryItem.deduplication_key == dedup_key
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def record_event(
        self,
        event_type: str,
        description: str,
        source_provenance: str,
        occurred_at: datetime | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        data_classification: str = "INTERNAL",
        is_inferred: bool = False,
        deduplication_key: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> EpisodicMemoryItem:
        """Record an episodic event with deduplication guarantee."""
        if deduplication_key:
            existing = self.get_by_deduplication_key(deduplication_key)
            if existing:
                existing.description = description
                existing.occurred_at = occurred_at or utc_now()
                if metadata_json:
                    existing.metadata_json = metadata_json
                self.db.commit()
                self.db.refresh(existing)
                return existing

        item_id = f"epm_{uuid.uuid4().hex[:16]}"
        item = EpisodicMemoryItem(
            id=item_id,
            event_type=event_type,
            description=description,
            occurred_at=occurred_at or utc_now(),
            task_id=task_id,
            project_id=project_id,
            session_id=session_id,
            data_classification=data_classification,
            source_provenance=source_provenance,
            is_inferred=is_inferred,
            deduplication_key=deduplication_key,
            metadata_json=metadata_json,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def list(
        self,
        event_type: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
        is_inferred: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EpisodicMemoryItem]:
        stmt = select(EpisodicMemoryItem)
        if event_type is not None:
            stmt = stmt.where(EpisodicMemoryItem.event_type == event_type)
        if task_id is not None:
            stmt = stmt.where(EpisodicMemoryItem.task_id == task_id)
        if project_id is not None:
            stmt = stmt.where(EpisodicMemoryItem.project_id == project_id)
        if session_id is not None:
            stmt = stmt.where(EpisodicMemoryItem.session_id == session_id)
        if occurred_after is not None:
            stmt = stmt.where(EpisodicMemoryItem.occurred_at >= occurred_after)
        if occurred_before is not None:
            stmt = stmt.where(EpisodicMemoryItem.occurred_at <= occurred_before)
        if is_inferred is not None:
            stmt = stmt.where(EpisodicMemoryItem.is_inferred.is_(is_inferred))

        stmt = stmt.order_by(EpisodicMemoryItem.occurred_at.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count(
        self,
        event_type: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(EpisodicMemoryItem)
        if event_type is not None:
            stmt = stmt.where(EpisodicMemoryItem.event_type == event_type)
        if task_id is not None:
            stmt = stmt.where(EpisodicMemoryItem.task_id == task_id)
        if project_id is not None:
            stmt = stmt.where(EpisodicMemoryItem.project_id == project_id)
        return self.db.execute(stmt).scalar_one()

    def delete(self, item_id: str) -> bool:
        item = self.get_by_id(item_id)
        if not item:
            return False
        self.db.delete(item)
        self.db.commit()
        return True
