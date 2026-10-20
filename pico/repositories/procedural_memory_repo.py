import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.models.procedural_memory import ProceduralMemoryItem
from pico.repositories.base import BaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProceduralMemoryRepository(BaseRepository[ProceduralMemoryItem]):
    VALID_LIFECYCLE_TRANSITIONS = {
        "draft": {"reviewed", "retired"},
        "reviewed": {"draft", "approved", "retired"},
        "approved": {"active", "reviewed", "retired"},
        "active": {"retired"},
        "retired": set(),
    }

    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, item_id: str) -> ProceduralMemoryItem | None:
        stmt = select(ProceduralMemoryItem).where(ProceduralMemoryItem.id == item_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_name(self, name: str) -> ProceduralMemoryItem | None:
        stmt = select(ProceduralMemoryItem).where(ProceduralMemoryItem.name == name)
        return self.db.execute(stmt).scalar_one_or_none()

    def create(
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
        """Create a procedural skill/workflow.

        Invariant: Newly learned/created procedures NEVER self-activate.
        They are initialized strictly in 'draft' status.
        """
        item_id = f"prc_{uuid.uuid4().hex[:16]}"
        item = ProceduralMemoryItem(
            id=item_id,
            name=name,
            version=version,
            description=description,
            lifecycle_status="draft",  # INVARIANT: Always starts in draft
            preconditions_json=preconditions_json,
            required_capabilities_json=required_capabilities_json,
            steps_json=steps_json,
            expected_effects_json=expected_effects_json,
            verification_criteria_json=verification_criteria_json,
            approved_by=None,
            approved_at=None,
            retired_at=None,
            data_classification=data_classification,
            source_provenance=source_provenance,
            metadata_json=metadata_json,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def transition_lifecycle(
        self,
        item: ProceduralMemoryItem,
        target_status: str,
        approver: str | None = None,
        notes: str | None = None,
    ) -> ProceduralMemoryItem:
        """Enforce strict lifecycle state machine and mandatory human approval.

        Transitions:
        draft -> reviewed
        reviewed -> approved (Requires human approver)
        approved -> active (Activation of approved procedure)
        active -> retired
        * -> retired
        """
        curr = item.lifecycle_status
        allowed = self.VALID_LIFECYCLE_TRANSITIONS.get(curr, set())

        if target_status not in allowed:
            raise ValidationError(
                f"Invalid lifecycle transition from '{curr}' to '{target_status}'. Allowed: {sorted(allowed)}"
            )

        now = utc_now()
        if target_status == "approved":
            clean_approver = (approver or "").strip()
            if not clean_approver:
                raise AuthorizationError(
                    "Explicit human approval is mandatory to approve a procedural memory. Approver identifier must be provided."
                )
            if clean_approver not in settings.TRUSTED_APPROVER_IDENTITIES:
                raise AuthorizationError(
                    f"Principal '{clean_approver}' is not authorized to approve procedures. Must be a trusted human operator ({settings.TRUSTED_APPROVER_IDENTITIES})."
                )
            item.approved_by = clean_approver
            item.approved_at = now
            if notes:
                meta = dict(item.metadata_json or {})
                meta["approval_notes"] = notes
                item.metadata_json = meta

        elif target_status == "active":
            if item.lifecycle_status != "approved":
                raise AuthorizationError(
                    "Cannot activate procedural memory without prior human approval."
                )

        elif target_status == "retired":
            item.retired_at = now

        item.lifecycle_status = target_status
        self.db.commit()
        self.db.refresh(item)
        return item

    def list(
        self,
        lifecycle_status: str | None = None,
        name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProceduralMemoryItem]:
        stmt = select(ProceduralMemoryItem)
        if lifecycle_status is not None:
            stmt = stmt.where(ProceduralMemoryItem.lifecycle_status == lifecycle_status)
        if name is not None:
            stmt = stmt.where(ProceduralMemoryItem.name.like(f"%{name}%"))

        stmt = stmt.order_by(ProceduralMemoryItem.updated_at.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count(self, active_only: bool = False) -> int:
        stmt = select(func.count()).select_from(ProceduralMemoryItem)
        if active_only:
            stmt = stmt.where(ProceduralMemoryItem.lifecycle_status == "active")
        return self.db.execute(stmt).scalar_one()

    def delete(self, item_id: str) -> bool:
        item = self.get_by_id(item_id)
        if not item:
            return False
        self.db.delete(item)
        self.db.commit()
        return True
