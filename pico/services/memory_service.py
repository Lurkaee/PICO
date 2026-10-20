from datetime import datetime
from typing import Any
from sqlalchemy.orm import Session

from pico.core.errors import NotFoundError
from pico.models.episodic_memory import EpisodicMemoryItem
from pico.models.procedural_memory import ProceduralMemoryItem
from pico.models.semantic_memory import SemanticMemoryItem
from pico.models.working_memory import WorkingMemoryItem
from pico.repositories.episodic_memory_repo import EpisodicMemoryRepository
from pico.repositories.procedural_memory_repo import ProceduralMemoryRepository
from pico.repositories.semantic_memory_repo import SemanticMemoryRepository
from pico.repositories.working_memory_repo import WorkingMemoryRepository
from pico.schemas.memory import MemoryStatusResponse


class MemoryService:
    """Coordinated memory service managing PICO's four-part persistent memory architecture:
    1. Working Memory: Ephemeral, bounded session scratchpad & active task state.
    2. Episodic Memory: Time-indexed event log, task outcomes, and reproducible errors.
    3. Semantic Memory: Source-grounded facts & preferences with SQLite FTS5 index.
    4. Procedural Memory: Reusable skills with strict draft->reviewed->approved->active lifecycle.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.working_repo = WorkingMemoryRepository(db)
        self.episodic_repo = EpisodicMemoryRepository(db)
        self.semantic_repo = SemanticMemoryRepository(db)
        self.procedural_repo = ProceduralMemoryRepository(db)

    # --- Working Memory ---
    def put_working_state(
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
        return self.working_repo.put(
            session_id=session_id,
            key=key,
            content=content,
            item_type=item_type,
            task_id=task_id,
            project_id=project_id,
            data_classification=data_classification,
            source_provenance=source_provenance,
            ttl_seconds=ttl_seconds,
            metadata_json=metadata_json,
        )

    def get_working_state(self, session_id: str, key: str) -> WorkingMemoryItem | None:
        return self.working_repo.get_by_session_and_key(session_id, key)

    def list_working_state(
        self,
        session_id: str,
        task_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[WorkingMemoryItem]:
        return self.working_repo.list_active(
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            limit=limit,
        )

    def clear_working_memory(self, session_id: str) -> int:
        return self.working_repo.clear_session(session_id)

    # --- Episodic Memory ---
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
        return self.episodic_repo.record_event(
            event_type=event_type,
            description=description,
            source_provenance=source_provenance,
            occurred_at=occurred_at,
            task_id=task_id,
            project_id=project_id,
            session_id=session_id,
            data_classification=data_classification,
            is_inferred=is_inferred,
            deduplication_key=deduplication_key,
            metadata_json=metadata_json,
        )

    def list_events(
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
        return self.episodic_repo.list(
            event_type=event_type,
            task_id=task_id,
            project_id=project_id,
            session_id=session_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            is_inferred=is_inferred,
            limit=limit,
            offset=offset,
        )

    def count_events(
        self,
        event_type: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        return self.episodic_repo.count(
            event_type=event_type,
            task_id=task_id,
            project_id=project_id,
        )

    # --- Semantic Memory ---
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
        return self.semantic_repo.save_fact(
            subject=subject,
            predicate=predicate,
            content=content,
            object=object,
            entity_type=entity_type,
            confidence=confidence,
            is_verified=is_verified,
            data_classification=data_classification,
            source_provenance=source_provenance,
            project_id=project_id,
            task_id=task_id,
            metadata_json=metadata_json,
        )

    def search_facts(
        self,
        query: str,
        limit: int = 10,
        entity_type: str | None = None,
        project_id: str | None = None,
    ) -> list[SemanticMemoryItem]:
        return self.semantic_repo.search_fts(
            query=query,
            limit=limit,
            entity_type=entity_type,
            project_id=project_id,
        )

    def forget_fact(self, item_id: str) -> bool:
        return self.semantic_repo.forget(item_id)

    def list_facts(
        self,
        entity_type: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        conflict_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SemanticMemoryItem]:
        return self.semantic_repo.list(
            entity_type=entity_type,
            project_id=project_id,
            task_id=task_id,
            conflict_status=conflict_status,
            limit=limit,
            offset=offset,
        )

    def count_facts(self, verified_only: bool = False) -> int:
        return self.semantic_repo.count(verified_only=verified_only)

    # --- Procedural Memory ---
    def create_procedure(
        self,
        name: str,
        description: str,
        steps_json: list[dict[str, Any]],
        version: str = "1.0.0",
        preconditions_json: list[str] | None = None,
        required_capabilities_json: list[str] | None = None,
        expected_effects_json: list[str] | None = None,
        verification_criteria_json: list[str] | None = None,
        data_classification: str = "INTERNAL",
        source_provenance: str = "user:manual_procedure",
        metadata_json: dict[str, Any] | None = None,
    ) -> ProceduralMemoryItem:
        return self.procedural_repo.create(
            name=name,
            description=description,
            steps_json=steps_json,
            version=version,
            preconditions_json=preconditions_json,
            required_capabilities_json=required_capabilities_json,
            expected_effects_json=expected_effects_json,
            verification_criteria_json=verification_criteria_json,
            data_classification=data_classification,
            source_provenance=source_provenance,
            metadata_json=metadata_json,
        )

    def review_procedure(self, procedure_id: str, reviewer: str | None = None, notes: str | None = None) -> ProceduralMemoryItem:
        item = self.procedural_repo.get_by_id(procedure_id)
        if not item:
            raise NotFoundError(f"Procedural memory '{procedure_id}' not found.")
        return self.procedural_repo.transition_lifecycle(item, "reviewed", approver=reviewer, notes=notes)

    def approve_procedure(self, procedure_id: str, approver: str, notes: str | None = None) -> ProceduralMemoryItem:
        item = self.procedural_repo.get_by_id(procedure_id)
        if not item:
            raise NotFoundError(f"Procedural memory '{procedure_id}' not found.")
        return self.procedural_repo.transition_lifecycle(item, "approved", approver=approver, notes=notes)

    def activate_procedure(self, procedure_id: str) -> ProceduralMemoryItem:
        item = self.procedural_repo.get_by_id(procedure_id)
        if not item:
            raise NotFoundError(f"Procedural memory '{procedure_id}' not found.")
        return self.procedural_repo.transition_lifecycle(item, "active")

    def retire_procedure(self, procedure_id: str) -> ProceduralMemoryItem:
        item = self.procedural_repo.get_by_id(procedure_id)
        if not item:
            raise NotFoundError(f"Procedural memory '{procedure_id}' not found.")
        return self.procedural_repo.transition_lifecycle(item, "retired")

    def list_procedures(
        self,
        lifecycle_status: str | None = None,
        name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProceduralMemoryItem]:
        return self.procedural_repo.list(
            lifecycle_status=lifecycle_status,
            name=name,
            limit=limit,
            offset=offset,
        )

    def count_procedures(self, active_only: bool = False) -> int:
        return self.procedural_repo.count(active_only=active_only)

    # --- System Status ---
    def get_memory_status(self) -> MemoryStatusResponse:
        return MemoryStatusResponse(
            working_memory_active_items=self.working_repo.count_active(),
            episodic_memory_total_events=self.episodic_repo.count(),
            semantic_memory_total_facts=self.semantic_repo.count(verified_only=False),
            semantic_memory_verified_facts=self.semantic_repo.count(verified_only=True),
            procedural_memory_total=self.procedural_repo.count(active_only=False),
            procedural_memory_active=self.procedural_repo.count(active_only=True),
            storage_type="sqlite_fts5",
        )
