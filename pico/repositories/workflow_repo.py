"""Repository for WorkflowRun and WorkflowStep entities (Milestone P9.1)."""

import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select, func
from sqlalchemy.orm import Session, selectinload

from pico.models.workflow import WorkflowRun, WorkflowStep


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, run_id: str) -> WorkflowRun | None:
        """Fetch workflow run by ID with eager loading of steps."""
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .options(selectinload(WorkflowRun.steps))
        )
        return self.db.scalars(stmt).first()

    def get_by_idempotency_key(self, idempotency_key: str) -> WorkflowRun | None:
        """Fetch workflow run by unique idempotency key."""
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.idempotency_key == idempotency_key)
            .options(selectinload(WorkflowRun.steps))
        )
        return self.db.scalars(stmt).first()

    def create_run(
        self,
        name: str,
        description: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        data_classification: str = "INTERNAL",
        metadata: dict[str, Any] | None = None,
        total_steps: int = 0,
    ) -> WorkflowRun:
        """Create a new WorkflowRun record."""
        run_id = f"wf_{uuid.uuid4().hex[:16]}"
        run = WorkflowRun(
            id=run_id,
            name=name,
            description=description,
            task_id=task_id,
            session_id=session_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            status="queued",
            data_classification=data_classification,
            total_steps=total_steps,
            completed_steps=0,
            failed_steps=0,
            metadata_json=metadata,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(run)
        self.db.flush()
        return run

    def add_step(
        self,
        workflow_run_id: str,
        step_key: str,
        capability_id: str,
        capability_version: str = "1.0.0",
        dependencies: list[str] | None = None,
        input_template: dict[str, Any] | None = None,
        order_index: int = 0,
        initial_status: str = "pending",
    ) -> WorkflowStep:
        """Create an individual WorkflowStep associated with a run."""
        step_id = f"step_{uuid.uuid4().hex[:16]}"
        step = WorkflowStep(
            id=step_id,
            workflow_run_id=workflow_run_id,
            step_key=step_key,
            capability_id=capability_id,
            capability_version=capability_version,
            status=initial_status,
            dependencies_json=dependencies or [],
            input_template_json=input_template or {},
            order_index=order_index,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(step)
        self.db.flush()
        return step

    def list_runs(
        self,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowRun], int]:
        """List and count workflow runs with optional filtering."""
        query = select(WorkflowRun).options(selectinload(WorkflowRun.steps))
        count_query = select(func.count()).select_from(WorkflowRun)

        if task_id:
            query = query.where(WorkflowRun.task_id == task_id)
            count_query = count_query.where(WorkflowRun.task_id == task_id)
        if status:
            query = query.where(WorkflowRun.status == status)
            count_query = count_query.where(WorkflowRun.status == status)

        total = self.db.scalar(count_query) or 0
        runs = list(self.db.scalars(query.order_by(WorkflowRun.created_at.desc()).limit(limit).offset(offset)).all())
        return runs, total

    def get_steps_for_run(self, workflow_run_id: str) -> list[WorkflowStep]:
        """Fetch all steps for a run ordered by order_index."""
        stmt = (
            select(WorkflowStep)
            .where(WorkflowStep.workflow_run_id == workflow_run_id)
            .order_by(WorkflowStep.order_index.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_step_by_key(self, workflow_run_id: str, step_key: str) -> WorkflowStep | None:
        """Fetch a specific step by its unique key within a run."""
        stmt = select(WorkflowStep).where(
            WorkflowStep.workflow_run_id == workflow_run_id,
            WorkflowStep.step_key == step_key,
        )
        return self.db.scalars(stmt).first()

    def update_run_status(
        self,
        run_id: str,
        status: str,
        error_message: str | None = None,
        completed_steps: int | None = None,
        failed_steps: int | None = None,
    ) -> WorkflowRun | None:
        """Update workflow run status and completion counters."""
        run = self.get_by_id(run_id)
        if not run:
            return None

        run.status = status
        run.updated_at = utc_now()
        if error_message is not None:
            run.error_message = error_message
        if completed_steps is not None:
            run.completed_steps = completed_steps
        if failed_steps is not None:
            run.failed_steps = failed_steps

        if status == "running" and run.started_at is None:
            run.started_at = utc_now()
        elif status in ("succeeded", "failed", "cancelled"):
            if run.completed_at is None:
                run.completed_at = utc_now()

        self.db.flush()
        return run

    def update_step_status(
        self,
        step_id: str,
        status: str,
        resolved_input: dict[str, Any] | None = None,
        tool_execution_id: str | None = None,
        output_json: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> WorkflowStep | None:
        """Update step lifecycle state and execution artifacts."""
        stmt = select(WorkflowStep).where(WorkflowStep.id == step_id)
        step = self.db.scalars(stmt).first()
        if not step:
            return None

        step.status = status
        step.updated_at = utc_now()
        if resolved_input is not None:
            step.resolved_input_json = resolved_input
        if tool_execution_id is not None:
            step.tool_execution_id = tool_execution_id
        if output_json is not None:
            step.output_json = output_json
        if error_message is not None:
            step.error_message = error_message

        if status == "running" and step.started_at is None:
            step.started_at = utc_now()
        elif status in ("succeeded", "failed", "cancelled", "blocked", "verification_failed"):
            if step.completed_at is None:
                step.completed_at = utc_now()

        self.db.flush()
        return step

    def get_interrupted_runs(self) -> list[WorkflowRun]:
        """Fetch runs left in non-terminal running state across system restart."""
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.status.in_(["running", "awaiting_approval"]))
            .options(selectinload(WorkflowRun.steps))
        )
        return list(self.db.scalars(stmt).all())
