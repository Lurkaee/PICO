import re
import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from pico.models.semantic_memory import SemanticMemoryItem
from pico.repositories.base import BaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SemanticMemoryRepository(BaseRepository[SemanticMemoryItem]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, item_id: str) -> SemanticMemoryItem | None:
        stmt = select(SemanticMemoryItem).where(SemanticMemoryItem.id == item_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def find_by_subject_predicate(
        self, subject: str, predicate: str
    ) -> list[SemanticMemoryItem]:
        stmt = select(SemanticMemoryItem).where(
            SemanticMemoryItem.subject == subject,
            SemanticMemoryItem.predicate == predicate,
            SemanticMemoryItem.conflict_status != "superseded",
        )
        return list(self.db.execute(stmt).scalars().all())

    def save_fact(
        self,
        subject: str,
        predicate: str,
        content: str,
        object: str | None = None,
        entity_type: str = "fact",
        confidence: float = 1.0,
        is_verified: bool = False,
        data_classification: str = "INTERNAL",
        source_provenance: str = "user:stated_preference",
        project_id: str | None = None,
        task_id: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> tuple[SemanticMemoryItem, bool]:
        """Save a factual memory with conflict handling and FTS5 indexing.

        Returns:
            (memory_item, is_conflict)

        Conflict Invariants:
        - Never silently overwrite an existing fact.
        - User-verified facts take precedence over unverified model inferences.
        - If conflict exists between unverified facts, mark conflict_status='conflicted'.
        - If user confirms (is_verified=True), previous fact is marked 'superseded'.
        """
        existing_facts = self.find_by_subject_predicate(subject, predicate)
        conflict_detected = False
        conflict_status = "clean"

        if existing_facts:
            # Check if identical content exists
            for ef in existing_facts:
                if ef.content == content:
                    if is_verified and not ef.is_verified:
                        ef.is_verified = True
                        ef.confidence = 1.0
                        self.db.commit()
                        self.db.refresh(ef)
                    return ef, False

            # Existing fact differs in content
            if is_verified:
                # User-confirmed update: supersede previous facts
                for ef in existing_facts:
                    ef.conflict_status = "superseded"
                conflict_status = "clean"
            else:
                # Model-inferred or unverified new fact conflicting with existing
                conflict_detected = True
                conflict_status = "conflicted"

        item_id = f"sem_{uuid.uuid4().hex[:16]}"
        item = SemanticMemoryItem(
            id=item_id,
            entity_type=entity_type,
            subject=subject,
            predicate=predicate,
            object=object,
            content=content,
            confidence=confidence,
            is_verified=is_verified,
            conflict_status=conflict_status,
            data_classification=data_classification,
            source_provenance=source_provenance,
            project_id=project_id,
            task_id=task_id,
            metadata_json=metadata_json,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)

        # Index in SQLite FTS5 table
        try:
            self.db.execute(
                text("""
                    INSERT INTO semantic_memories_fts (memory_id, subject, predicate, object, content)
                    VALUES (:mid, :sub, :pred, :obj, :cnt)
                """),
                {
                    "mid": item.id,
                    "sub": item.subject,
                    "pred": item.predicate,
                    "obj": item.object or "",
                    "cnt": item.content,
                },
            )
            self.db.commit()
        except Exception:
            # Table might not exist or non-sqlite dialect in test
            self.db.rollback()

        return item, conflict_detected

    def search_fts(
        self,
        query: str,
        limit: int = 10,
        entity_type: str | None = None,
        project_id: str | None = None,
    ) -> list[SemanticMemoryItem]:
        """Perform full-text search across semantic memories using SQLite FTS5.

        Falls back to LIKE queries if FTS is unavailable or syntax query fails.
        """
        # Extract meaningful alphanumeric tokens (length >= 3 preferred)
        tokens = [w for w in re.findall(r'\w+', query) if len(w) >= 3]
        if not tokens:
            tokens = [w for w in re.findall(r'\w+', query) if w]

        matched_ids: list[str] = []
        if tokens:
            fts_query = " OR ".join(tokens)
            try:
                stmt = text("""
                    SELECT memory_id
                    FROM semantic_memories_fts
                    WHERE semantic_memories_fts MATCH :match_query
                    ORDER BY rank
                    LIMIT :lim
                """)
                rows = self.db.execute(stmt, {"match_query": fts_query, "lim": limit * 2}).fetchall()
                matched_ids = [str(r[0]) for r in rows]
            except Exception:
                matched_ids = []

        if matched_ids:
            sel = select(SemanticMemoryItem).where(
                SemanticMemoryItem.id.in_(matched_ids),
                SemanticMemoryItem.conflict_status != "superseded",
            )
            if entity_type is not None:
                sel = sel.where(SemanticMemoryItem.entity_type == entity_type)
            if project_id is not None:
                sel = sel.where(SemanticMemoryItem.project_id == project_id)

            items = list(self.db.execute(sel).scalars().all())
            # Preserve FTS ranking order
            id_to_item = {i.id: i for i in items}
            ordered = [id_to_item[mid] for mid in matched_ids if mid in id_to_item]
            return ordered[:limit]

        # Fallback to standard substring match
        pattern = f"%{query}%"
        fallback_stmt = select(SemanticMemoryItem).where(
            SemanticMemoryItem.conflict_status != "superseded",
            (
                SemanticMemoryItem.content.like(pattern)
                | SemanticMemoryItem.subject.like(pattern)
                | SemanticMemoryItem.predicate.like(pattern)
            ),
        )
        if entity_type is not None:
            fallback_stmt = fallback_stmt.where(SemanticMemoryItem.entity_type == entity_type)
        if project_id is not None:
            fallback_stmt = fallback_stmt.where(SemanticMemoryItem.project_id == project_id)

        fallback_stmt = (
            fallback_stmt.order_by(SemanticMemoryItem.confidence.desc(), SemanticMemoryItem.updated_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(fallback_stmt).scalars().all())

    def list(
        self,
        entity_type: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        conflict_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SemanticMemoryItem]:
        stmt = select(SemanticMemoryItem)
        if entity_type is not None:
            stmt = stmt.where(SemanticMemoryItem.entity_type == entity_type)
        if project_id is not None:
            stmt = stmt.where(SemanticMemoryItem.project_id == project_id)
        if task_id is not None:
            stmt = stmt.where(SemanticMemoryItem.task_id == task_id)
        if conflict_status is not None:
            stmt = stmt.where(SemanticMemoryItem.conflict_status == conflict_status)

        stmt = stmt.order_by(SemanticMemoryItem.updated_at.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def forget(self, item_id: str) -> bool:
        """User-directed forgetting: completely purge a fact from database and FTS."""
        item = self.get_by_id(item_id)
        if not item:
            return False

        # Remove from FTS
        try:
            self.db.execute(
                text("DELETE FROM semantic_memories_fts WHERE memory_id = :mid"),
                {"mid": item_id},
            )
        except Exception:
            pass

        self.db.delete(item)
        self.db.commit()
        return True

    def count(self, verified_only: bool = False) -> int:
        stmt = select(func.count()).select_from(SemanticMemoryItem).where(
            SemanticMemoryItem.conflict_status != "superseded"
        )
        if verified_only:
            stmt = stmt.where(SemanticMemoryItem.is_verified.is_(True))
        return self.db.execute(stmt).scalar_one()
