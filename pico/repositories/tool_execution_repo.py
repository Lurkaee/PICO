import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.tool_execution import ToolExecution
from pico.repositories.base import BaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ToolExecutionRepository(BaseRepository[ToolExecution]):
    VALID_STATUSES = {
        "queued",
        "awaiting_approval",
        "running",
        "verifying",
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
        "verification_failed",
        "retry_scheduled",
    }

    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, item_id: str) -> ToolExecution | None:
        stmt = select(ToolExecution).where(ToolExecution.id == item_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def create(
        self,
        capability_id: str,
        input_json: dict[str, Any],
        input_digest: str,
        capability_version: str = "1.0.0",
        task_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        status: str = "queued",
        required_permission: str | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 0,
        data_classification: str = "INTERNAL",
        metadata_json: dict[str, Any] | None = None,
    ) -> ToolExecution:
        now = utc_now()
        item = ToolExecution(
            id=f"exec_{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            session_id=session_id,
            capability_id=capability_id,
            capability_version=capability_version,
            status=status,
            idempotency_key=idempotency_key,
            input_json=input_json,
            input_digest=input_digest,
            required_permission=required_permission,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            data_classification=data_classification,
            queued_at=now,
            created_at=now,
            updated_at=now,
            metadata_json=metadata_json,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def get_by_idempotency_key(self, key: str) -> ToolExecution | None:
        stmt = select(ToolExecution).where(ToolExecution.idempotency_key == key)
        return self.db.execute(stmt).scalar_one_or_none()

    def update_status(
        self,
        execution_id: str,
        status: str,
        output_json: dict[str, Any] | None = None,
        error_message: str | None = None,
        error_details_json: dict[str, Any] | None = None,
        verification_status: str | None = None,
        verification_details_json: dict[str, Any] | None = None,
        increment_retry: bool = False,
    ) -> ToolExecution | None:
        item = self.get_by_id(execution_id)
        if not item:
            return None

        now = utc_now()
        item.status = status
        item.updated_at = now

        if status == "running" and not item.started_at:
            item.started_at = now
        elif status in ("succeeded", "failed", "cancelled", "timed_out", "verification_failed"):
            item.completed_at = now

        if output_json is not None:
            item.output_json = output_json
        if error_message is not None:
            item.error_message = error_message
        if error_details_json is not None:
            item.error_details_json = error_details_json
        if verification_status is not None:
            item.verification_status = verification_status
        if verification_details_json is not None:
            item.verification_details_json = verification_details_json
        if increment_retry:
            item.retry_count += 1

        self.db.commit()
        self.db.refresh(item)
        return item

    def record_approval(
        self, execution_id: str, approver: str, input_digest: str
    ) -> ToolExecution | None:
        item = self.get_by_id(execution_id)
        if not item:
            return None

        now = utc_now()
        item.approved_by = approver
        item.approved_at = now
        item.approval_input_digest = input_digest
        item.status = "queued"
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def list(
        self,
        status: str | None = None,
        capability_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ToolExecution]:
        stmt = select(ToolExecution)
        if status is not None:
            stmt = stmt.where(ToolExecution.status == status)
        if capability_id is not None:
            stmt = stmt.where(ToolExecution.capability_id == capability_id)
        if task_id is not None:
            stmt = stmt.where(ToolExecution.task_id == task_id)
        if session_id is not None:
            stmt = stmt.where(ToolExecution.session_id == session_id)

        stmt = stmt.order_by(ToolExecution.created_at.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count(self, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(ToolExecution)
        if status is not None:
            stmt = stmt.where(ToolExecution.status == status)
        return self.db.execute(stmt).scalar_one()
