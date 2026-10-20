import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.working_memory import WorkingMemoryItem
from pico.repositories.base import BaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkingMemoryRepository(BaseRepository[WorkingMemoryItem]):
    def __init__(self, db: Session, max_items_per_session: int = 50) -> None:
        super().__init__(db)
        self.max_items_per_session = max_items_per_session

    def get_by_id(self, item_id: str) -> WorkingMemoryItem | None:
        stmt = select(WorkingMemoryItem).where(WorkingMemoryItem.id == item_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_session_and_key(self, session_id: str, key: str) -> WorkingMemoryItem | None:
        stmt = select(WorkingMemoryItem).where(
            WorkingMemoryItem.session_id == session_id,
            WorkingMemoryItem.key == key,
            WorkingMemoryItem.is_active.is_(True),
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_active(
        self,
        session_id: str,
        task_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[WorkingMemoryItem]:
        """Retrieve active, non-expired working memory items for the current session."""
        now = utc_now()
        stmt = select(WorkingMemoryItem).where(
            WorkingMemoryItem.session_id == session_id,
            WorkingMemoryItem.is_active.is_(True),
        )
        if task_id is not None:
            stmt = stmt.where(WorkingMemoryItem.task_id == task_id)
        if project_id is not None:
            stmt = stmt.where(WorkingMemoryItem.project_id == project_id)

        # Filter out expired items
        stmt = stmt.where(
            (WorkingMemoryItem.expires_at.is_(None)) | (WorkingMemoryItem.expires_at > now)
        )
        stmt = stmt.order_by(WorkingMemoryItem.created_at.desc()).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def put(
        self,
        session_id: str,
        key: str,
        content: str,
        item_type: str = "working_state",
        task_id: str | None = None,
        project_id: str | None = None,
        data_classification: str = "INTERNAL",
        source_provenance: str = "session:working_memory",
        ttl_seconds: int | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> WorkingMemoryItem:
        """Create or update a bounded working memory item.

        Enforces:
        - Bounded session memory: Prunes oldest inactive/expired items if count exceeds limit.
        - Idempotency per key within session.
        """
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None

        existing = self.get_by_session_and_key(session_id, key)
        if existing:
            existing.content = content
            existing.item_type = item_type
            existing.task_id = task_id
            existing.project_id = project_id
            existing.data_classification = data_classification
            existing.source_provenance = source_provenance
            existing.expires_at = expires_at
            existing.metadata_json = metadata_json
            existing.is_active = True
            self.db.commit()
            self.db.refresh(existing)
            return existing

        # Check bounded size policy for this session
        active_count_stmt = select(func.count()).select_from(WorkingMemoryItem).where(
            WorkingMemoryItem.session_id == session_id,
            WorkingMemoryItem.is_active.is_(True),
        )
        current_active = self.db.execute(active_count_stmt).scalar_one()

        if current_active >= self.max_items_per_session:
            # Prune oldest active item in this session
            oldest_stmt = (
                select(WorkingMemoryItem)
                .where(
                    WorkingMemoryItem.session_id == session_id,
                    WorkingMemoryItem.is_active.is_(True),
                )
                .order_by(WorkingMemoryItem.created_at.asc())
                .limit(1)
            )
            oldest = self.db.execute(oldest_stmt).scalar_one_or_none()
            if oldest:
                oldest.is_active = False

        item_id = f"wmem_{uuid.uuid4().hex[:16]}"
        item = WorkingMemoryItem(
            id=item_id,
            session_id=session_id,
            key=key,
            item_type=item_type,
            content=content,
            task_id=task_id,
            project_id=project_id,
            data_classification=data_classification,
            source_provenance=source_provenance,
            is_active=True,
            expires_at=expires_at,
            metadata_json=metadata_json,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def clear_session(self, session_id: str) -> int:
        """Clear all active working memory entries for a session."""
        stmt = select(WorkingMemoryItem).where(
            WorkingMemoryItem.session_id == session_id,
            WorkingMemoryItem.is_active.is_(True),
        )
        items = list(self.db.execute(stmt).scalars().all())
        count = len(items)
        for item in items:
            item.is_active = False
        self.db.commit()
        return count

    def delete(self, item_id: str) -> bool:
        item = self.get_by_id(item_id)
        if not item:
            return False
        self.db.delete(item)
        self.db.commit()
        return True

    def count_active(self) -> int:
        now = utc_now()
        stmt = select(func.count()).select_from(WorkingMemoryItem).where(
            WorkingMemoryItem.is_active.is_(True),
            (WorkingMemoryItem.expires_at.is_(None)) | (WorkingMemoryItem.expires_at > now),
        )
        return self.db.execute(stmt).scalar_one()
