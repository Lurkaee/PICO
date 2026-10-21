import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from pico.models.staged_diff import StagedDiff
from pico.repositories.base import BaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StagedDiffRepository(BaseRepository[StagedDiff]):
    VALID_STATUSES = {"staged", "applied", "rolled_back", "discarded"}

    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, item_id: str) -> StagedDiff | None:
        stmt = select(StagedDiff).where(StagedDiff.id == item_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_execution_id(self, execution_id: str) -> StagedDiff | None:
        stmt = select(StagedDiff).where(StagedDiff.execution_id == execution_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def create(
        self,
        root_id: str,
        relative_path: str,
        operation: str,
        original_state: str,
        proposed_digest: str,
        diff_digest: str,
        unified_diff: str,
        proposed_content: str,
        original_content: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> StagedDiff:
        proposal_id = f"diff_{uuid.uuid4().hex}"
        item = StagedDiff(
            id=proposal_id,
            root_id=root_id,
            relative_path=relative_path,
            operation=operation,
            original_state=original_state,
            proposed_digest=proposed_digest,
            diff_digest=diff_digest,
            unified_diff=unified_diff,
            proposed_content=proposed_content,
            original_content=original_content,
            status="staged",
            metadata_json=metadata_json or {},
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def list_proposals(
        self,
        root_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[StagedDiff]:
        stmt = select(StagedDiff)
        if root_id:
            stmt = stmt.where(StagedDiff.root_id == root_id)
        if status:
            stmt = stmt.where(StagedDiff.status == status)
        stmt = stmt.order_by(desc(StagedDiff.created_at)).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def link_execution(self, proposal_id: str, execution_id: str) -> StagedDiff | None:
        item = self.get_by_id(proposal_id)
        if not item:
            return None
        item.execution_id = execution_id
        item.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(item)
        return item

    def mark_applied(self, proposal_id: str, verified_digest: str) -> StagedDiff | None:
        item = self.get_by_id(proposal_id)
        if not item:
            return None
        now = utc_now()
        item.status = "applied"
        item.applied_at = now
        item.verified_at = now
        item.verified_digest = verified_digest
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def mark_rolled_back(self, proposal_id: str) -> StagedDiff | None:
        item = self.get_by_id(proposal_id)
        if not item:
            return None
        now = utc_now()
        item.status = "rolled_back"
        item.rolled_back_at = now
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def mark_discarded(self, proposal_id: str) -> StagedDiff | None:
        item = self.get_by_id(proposal_id)
        if not item:
            return None
        now = utc_now()
        item.status = "discarded"
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item
