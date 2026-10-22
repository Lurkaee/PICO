"""Workflow coordination service (Milestone P9.1)."""

import asyncio
from typing import Any
from sqlalchemy.orm import Session

from pico.capabilities import CapabilityRegistry, registry as default_registry
from pico.core.errors import EntityNotFoundError, ValidationError, ConflictError
from pico.models.workflow import WorkflowRun, WorkflowStep
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.workflow_repo import WorkflowRepository
from pico.schemas.workflow import (
    StepDefinition,
    WorkflowCreate,
    WorkflowRunResponse,
    WorkflowStepResponse,
)
from pico.services.execution_engine import ToolExecutionEngine
from pico.workflow.resolver import resolve_step_input
from pico.workflow.validator import validate_workflow_graph


def step_to_response(step: WorkflowStep) -> WorkflowStepResponse:
    """Converts a WorkflowStep ORM object to a typed Pydantic response."""
    return WorkflowStepResponse(
        id=step.id,
        workflow_run_id=step.workflow_run_id,
        step_key=step.step_key,
        capability_id=step.capability_id,
        capability_version=step.capability_version,
        status=step.status,
        dependencies=step.dependencies_json or [],
        input_template=step.input_template_json or {},
        resolved_input=step.resolved_input_json,
        tool_execution_id=step.tool_execution_id,
        output=step.output_json,
        error_message=step.error_message,
        order_index=step.order_index,
        created_at=step.created_at,
        updated_at=step.updated_at,
        started_at=step.started_at,
        completed_at=step.completed_at,
    )


def run_to_response(run: WorkflowRun) -> WorkflowRunResponse:
    """Converts a WorkflowRun ORM object to a typed Pydantic response."""
    return WorkflowRunResponse(
        id=run.id,
        task_id=run.task_id,
        session_id=run.session_id,
        correlation_id=run.correlation_id,
        idempotency_key=run.idempotency_key,
        name=run.name,
        description=run.description,
        status=run.status,
        data_classification=run.data_classification,
        total_steps=run.total_steps,
        completed_steps=run.completed_steps,
        failed_steps=run.failed_steps,
        error_message=run.error_message,
        metadata=run.metadata_json,
        steps=[step_to_response(s) for s in run.steps],
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


class WorkflowService:
    """
    Coordinates durable, dependency-aware workflow execution over PICO's ToolExecutionEngine.
    """

    def __init__(
        self,
        db: Session,
        capability_registry: CapabilityRegistry | None = None,
        execution_engine: ToolExecutionEngine | None = None,
    ):
        self.db = db
        self.capability_registry = capability_registry or default_registry
        self.repo = WorkflowRepository(db)
        self.ledger_repo = EventLedgerRepository(db)
        self.engine = execution_engine or ToolExecutionEngine(
            db, capability_registry=self.capability_registry
        )

    def submit_workflow(self, payload: WorkflowCreate) -> WorkflowRunResponse:
        """
        Validates the dependency graph and registers a durable workflow run with initial steps.
        Enforces idempotency and records audit ledger events.
        """
        # 1. Idempotency Check
        if payload.idempotency_key and payload.idempotency_key.strip():
            existing = self.repo.get_by_idempotency_key(payload.idempotency_key.strip())
            if existing:
                self.ledger_repo.append(
                    event_type="workflow_idempotent_submission_matched",
                    task_id=existing.task_id,
                    correlation_id=existing.correlation_id,
                    payload_json={
                        "workflow_id": existing.id,
                        "idempotency_key": payload.idempotency_key,
                        "status": existing.status,
                    },
                    data_classification=existing.data_classification,
                )
                self.db.commit()
                return run_to_response(existing)

        # 2. Dependency Graph and Schema Validation (Pre-dispatch gate)
        sorted_steps = validate_workflow_graph(payload, self.capability_registry)

        # 3. Create WorkflowRun in Database
        run = self.repo.create_run(
            name=payload.name,
            description=payload.description,
            task_id=payload.task_id,
            session_id=payload.session_id,
            correlation_id=payload.correlation_id,
            idempotency_key=payload.idempotency_key.strip() if payload.idempotency_key else None,
            data_classification=payload.data_classification,
            metadata=payload.metadata,
            total_steps=len(sorted_steps),
        )

        # 4. Create Initial Steps
        for idx, step_def in enumerate(sorted_steps):
            initial_status = "ready" if len(step_def.dependencies) == 0 else "pending"
            self.repo.add_step(
                workflow_run_id=run.id,
                step_key=step_def.step_key,
                capability_id=step_def.capability_id,
                capability_version=step_def.capability_version,
                dependencies=step_def.dependencies,
                input_template=step_def.input_template,
                order_index=idx,
                initial_status=initial_status,
            )

        # 5. Audit Ledger Log
        self.ledger_repo.append(
            event_type="workflow_submitted",
            task_id=run.task_id,
            correlation_id=run.correlation_id,
            payload_json={
                "workflow_id": run.id,
                "name": run.name,
                "step_count": len(sorted_steps),
                "data_classification": run.data_classification,
            },
            data_classification=run.data_classification,
        )

        self.db.commit()
        refreshed = self.repo.get_by_id(run.id)
        if not refreshed:
            raise EntityNotFoundError("WorkflowRun", run.id)
        return run_to_response(refreshed)

    def get_workflow(self, run_id: str) -> WorkflowRunResponse:
        """Retrieve workflow run and steps by ID."""
        run = self.repo.get_by_id(run_id)
        if not run:
            raise EntityNotFoundError("WorkflowRun", run_id)
        return run_to_response(run)

    def list_workflows(
        self,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowRunResponse], int]:
        """List workflow runs with optional filters."""
        runs, total = self.repo.list_runs(
            task_id=task_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [run_to_response(r) for r in runs], total

    async def advance_workflow(
        self,
        run_id: str,
        caller_permissions: set[str] | list[str] | None = None,
        max_steps: int = 10,
    ) -> WorkflowRunResponse:
        """
        Advances the workflow coordinator, executing eligible ready steps in dependency order
        through the ToolExecutionEngine.
        """
        run = self.repo.get_by_id(run_id)
        if not run:
            raise EntityNotFoundError("WorkflowRun", run_id)

        # Terminal runs cannot be advanced
        if run.status in ("succeeded", "failed", "cancelled"):
            return run_to_response(run)

        effective_perms = set(caller_permissions or [])

        # Transition run to running if currently queued
        if run.status == "queued":
            self.repo.update_run_status(run.id, status="running")
            self.db.commit()
            run = self.repo.get_by_id(run.id)
            if not run:
                raise EntityNotFoundError("WorkflowRun", run_id)

        steps_dispatched = 0

        while steps_dispatched < max_steps:
            steps = self.repo.get_steps_for_run(run.id)
            step_map = {s.step_key: s for s in steps}

            # 1. Check if any step in awaiting_approval has been approved or rejected
            approved_step: WorkflowStep | None = None
            for s in steps:
                if s.status == "awaiting_approval" and s.tool_execution_id:
                    exec_record = self.engine.exec_repo.get_by_id(s.tool_execution_id)
                    if exec_record:
                        if (exec_record.status in ("queued", "approved") and exec_record.approved_at is not None) or exec_record.status == "approved":
                            approved_step = s
                            break
                        elif exec_record.status in ("rejected", "cancelled"):
                            self.repo.update_step_status(
                                s.id,
                                status="failed",
                                error_message=f"Execution {exec_record.status} by approver.",
                            )
                            for down_s in steps:
                                if down_s.status in ("pending", "ready"):
                                    self.repo.update_step_status(down_s.id, status="blocked")
                            self.repo.update_run_status(
                                run.id,
                                status="failed",
                                error_message=f"Step '{s.step_key}' execution was {exec_record.status}.",
                                failed_steps=(run.failed_steps + 1),
                            )
                            self.db.commit()
                            refreshed_run = self.repo.get_by_id(run.id)
                            return run_to_response(refreshed_run) if refreshed_run else run_to_response(run)

            # 2. If an approved step exists, execute it directly
            if approved_step:
                step = approved_step
                self.repo.update_step_status(step.id, status="running")
                self.db.commit()

                exec_result = await self.engine.execute(
                    step.tool_execution_id, caller_permissions=effective_perms
                )
                steps_dispatched += 1

                if exec_result.status == "succeeded":
                    self.repo.update_step_status(
                        step.id,
                        status="succeeded",
                        output_json=exec_result.output_json,
                    )
                    run = self.repo.get_by_id(run.id)
                    new_completed = (run.completed_steps if run else 0) + 1
                    self.repo.update_run_status(run.id, status="running", completed_steps=new_completed)
                    self.ledger_repo.append(
                        event_type="workflow_step_succeeded",
                        task_id=run.task_id,
                        correlation_id=run.correlation_id,
                        execution_id=step.tool_execution_id,
                        payload_json={"workflow_id": run.id, "step_key": step.step_key},
                        data_classification=run.data_classification,
                    )
                else:
                    failure_status = "verification_failed" if exec_result.status == "verification_failed" else "failed"
                    self.repo.update_step_status(
                        step.id,
                        status=failure_status,
                        output_json=exec_result.output_json,
                        error_message=exec_result.error_message or "Execution failed.",
                    )
                    for s_item in self.repo.get_steps_for_run(run.id):
                        if s_item.status in ("pending", "ready"):
                            self.repo.update_step_status(s_item.id, status="blocked")
                    run = self.repo.get_by_id(run.id)
                    new_failed = (run.failed_steps if run else 0) + 1
                    self.repo.update_run_status(
                        run.id,
                        status="failed",
                        error_message=f"Step '{step.step_key}' {failure_status}: {exec_result.error_message}",
                        failed_steps=new_failed,
                    )
                    self.ledger_repo.append(
                        event_type="workflow_step_failed",
                        task_id=run.task_id,
                        correlation_id=run.correlation_id,
                        execution_id=step.tool_execution_id,
                        payload_json={
                            "workflow_id": run.id,
                            "step_key": step.step_key,
                            "status": failure_status,
                            "error": exec_result.error_message,
                        },
                        data_classification=run.data_classification,
                    )
                    self.db.commit()
                    break

                self.db.commit()
                continue

            # 3. Otherwise find ready or newly ready steps
            ready_step: WorkflowStep | None = None
            for s in steps:
                if s.status == "ready":
                    ready_step = s
                    break
                elif s.status == "pending":
                    # Check if all dependencies are satisfied
                    all_deps_succeeded = True
                    for dep_key in s.dependencies_json:
                        dep_s = step_map.get(dep_key)
                        if not dep_s or dep_s.status != "succeeded":
                            all_deps_succeeded = False
                            break
                    if all_deps_succeeded:
                        self.repo.update_step_status(s.id, status="ready")
                        self.db.commit()
                        ready_step = self.repo.get_step_by_key(run.id, s.step_key)
                        break

            # If no eligible ready step, evaluate overall status and exit loop
            if not ready_step:
                # 1. Any step awaiting approval?
                if any(s.status == "awaiting_approval" for s in steps):
                    self.repo.update_run_status(run.id, status="awaiting_approval")
                # 2. Any step failed or blocked?
                elif any(s.status in ("failed", "verification_failed") for s in steps):
                    # Mark all pending/ready steps as blocked
                    for s in steps:
                        if s.status in ("pending", "ready"):
                            self.repo.update_step_status(s.id, status="blocked")
                    self.repo.update_run_status(run.id, status="failed")
                    self.ledger_repo.append(
                        event_type="workflow_failed",
                        task_id=run.task_id,
                        correlation_id=run.correlation_id,
                        payload_json={"workflow_id": run.id, "reason": "One or more steps failed."},
                        data_classification=run.data_classification,
                    )
                # 3. All steps succeeded?
                elif all(s.status == "succeeded" for s in steps):
                    self.repo.update_run_status(
                        run.id,
                        status="succeeded",
                        completed_steps=len(steps),
                    )
                    self.ledger_repo.append(
                        event_type="workflow_succeeded",
                        task_id=run.task_id,
                        correlation_id=run.correlation_id,
                        payload_json={"workflow_id": run.id, "total_steps": len(steps)},
                        data_classification=run.data_classification,
                    )
                self.db.commit()
                break


            # Process ready step
            step = ready_step
            self.repo.update_step_status(step.id, status="running")
            self.db.commit()

            # Gather completed outputs of upstream dependencies
            completed_outputs = {
                s.step_key: (s.output_json or {})
                for s in steps
                if s.status == "succeeded"
            }

            capability = self.capability_registry.get(
                step.capability_id, version=step.capability_version
            )

            # Resolve inputs
            try:
                resolved_input = resolve_step_input(
                    step.input_template_json, completed_outputs, capability
                )
            except Exception as exc:
                # Input resolution failure fails the step and blocks dependents
                self.repo.update_step_status(
                    step.id,
                    status="failed",
                    error_message=f"Input resolution failed: {str(exc)}",
                )
                self.repo.update_run_status(
                    run.id,
                    status="failed",
                    error_message=f"Step '{step.step_key}' resolution failed: {str(exc)}",
                    failed_steps=(run.failed_steps + 1),
                )
                self.db.commit()
                break

            # Submit to ToolExecutionEngine
            try:
                exec_record = self.engine.submit(
                    capability_id=step.capability_id,
                    input_data=resolved_input,
                    capability_version=step.capability_version,
                    task_id=run.task_id,
                    session_id=run.session_id,
                    correlation_id=run.correlation_id,
                    caller_permissions=effective_perms,
                    context_classification=run.data_classification,
                )
            except Exception as exc:
                # Authorization or submission failure
                self.repo.update_step_status(
                    step.id,
                    status="failed",
                    resolved_input=resolved_input,
                    error_message=str(exc),
                )
                self.repo.update_run_status(
                    run.id,
                    status="failed",
                    error_message=f"Step '{step.step_key}' submission failed: {str(exc)}",
                    failed_steps=(run.failed_steps + 1),
                )
                self.db.commit()
                break

            self.repo.update_step_status(
                step.id,
                status="running" if exec_record.status != "awaiting_approval" else "awaiting_approval",
                resolved_input=resolved_input,
                tool_execution_id=exec_record.id,
            )
            self.db.commit()

            # If the step requires human approval, halt advancement until approved
            if exec_record.status == "awaiting_approval":
                self.repo.update_run_status(run.id, status="awaiting_approval")
                self.ledger_repo.append(
                    event_type="workflow_step_awaiting_approval",
                    task_id=run.task_id,
                    correlation_id=run.correlation_id,
                    execution_id=exec_record.id,
                    payload_json={"workflow_id": run.id, "step_key": step.step_key},
                    data_classification=run.data_classification,
                )
                self.db.commit()
                break

            # Execute non-consequential or pre-approved execution
            exec_result = await self.engine.execute(
                exec_record.id, caller_permissions=effective_perms
            )
            steps_dispatched += 1

            if exec_result.status == "succeeded":
                self.repo.update_step_status(
                    step.id,
                    status="succeeded",
                    output_json=exec_result.output_json,
                )
                # Refresh run completed steps count
                run = self.repo.get_by_id(run.id)
                new_completed = (run.completed_steps if run else 0) + 1
                self.repo.update_run_status(run.id, status="running", completed_steps=new_completed)
                self.ledger_repo.append(
                    event_type="workflow_step_succeeded",
                    task_id=run.task_id,
                    correlation_id=run.correlation_id,
                    execution_id=exec_record.id,
                    payload_json={"workflow_id": run.id, "step_key": step.step_key},
                    data_classification=run.data_classification,
                )
            else:
                failure_status = "verification_failed" if exec_result.status == "verification_failed" else "failed"
                self.repo.update_step_status(
                    step.id,
                    status=failure_status,
                    output_json=exec_result.output_json,
                    error_message=exec_result.error_message or "Execution failed.",
                )
                # Mark downstream steps blocked
                for s in self.repo.get_steps_for_run(run.id):
                    if s.status in ("pending", "ready"):
                        self.repo.update_step_status(s.id, status="blocked")
                run = self.repo.get_by_id(run.id)
                new_failed = (run.failed_steps if run else 0) + 1
                self.repo.update_run_status(
                    run.id,
                    status="failed",
                    error_message=f"Step '{step.step_key}' {failure_status}: {exec_result.error_message}",
                    failed_steps=new_failed,
                )
                self.ledger_repo.append(
                    event_type="workflow_step_failed",
                    task_id=run.task_id,
                    correlation_id=run.correlation_id,
                    execution_id=exec_record.id,
                    payload_json={
                        "workflow_id": run.id,
                        "step_key": step.step_key,
                        "status": failure_status,
                        "error": exec_result.error_message,
                    },
                    data_classification=run.data_classification,
                )
                self.db.commit()
                break

            self.db.commit()

        # Check final status after loop
        updated_run = self.repo.get_by_id(run.id)
        if not updated_run:
            raise EntityNotFoundError("WorkflowRun", run_id)
        steps_after = self.repo.get_steps_for_run(run.id)
        if all(s.status == "succeeded" for s in steps_after) and updated_run.status != "succeeded":
            self.repo.update_run_status(
                run.id, status="succeeded", completed_steps=len(steps_after)
            )
            self.db.commit()
            updated_run = self.repo.get_by_id(run.id)

        return run_to_response(updated_run)

    def cancel_workflow(self, run_id: str, reason: str | None = None) -> WorkflowRunResponse:
        """
        Cancels an active workflow run and marks pending or running steps cancelled.
        Pre-dispatched tool executions are cancelled if eligible.
        """
        run = self.repo.get_by_id(run_id)
        if not run:
            raise EntityNotFoundError("WorkflowRun", run_id)

        if run.status in ("succeeded", "failed", "cancelled"):
            return run_to_response(run)

        cancel_reason = reason or "Workflow cancelled by operator."
        steps = self.repo.get_steps_for_run(run.id)

        try:
            from pico.scheduler import get_scheduler
            get_scheduler().cancel_workflow_requests_sync(run.id)
        except Exception:
            pass

        for s in steps:
            if s.status in ("pending", "ready", "awaiting_approval", "running"):
                if s.tool_execution_id:
                    try:
                        self.engine.cancel_execution(s.tool_execution_id, reason=cancel_reason)
                    except Exception:
                        pass
                self.repo.update_step_status(
                    s.id,
                    status="cancelled",
                    error_message=cancel_reason,
                )

        self.repo.update_run_status(
            run.id,
            status="cancelled",
            error_message=cancel_reason,
        )

        self.ledger_repo.append(
            event_type="workflow_cancelled",
            task_id=run.task_id,
            correlation_id=run.correlation_id,
            payload_json={"workflow_id": run.id, "reason": cancel_reason},
            data_classification=run.data_classification,
        )

        self.db.commit()
        refreshed = self.repo.get_by_id(run.id)
        if not refreshed:
            raise EntityNotFoundError("WorkflowRun", run_id)
        return run_to_response(refreshed)

    def recover_interrupted_workflows(self) -> list[str]:
        """
        Reconciles interrupted workflow runs after an application crash or restart.
        Ensures truthful state reporting without blindly replaying non-idempotent actions.
        """
        interrupted_runs = self.repo.get_interrupted_runs()
        reconciled_ids: list[str] = []

        for run in interrupted_runs:
            steps = self.repo.get_steps_for_run(run.id)
            has_failed_step = False

            for s in steps:
                if s.status == "running":
                    if s.tool_execution_id:
                        exec_record = self.engine.exec_repo.get_by_id(s.tool_execution_id)
                        if exec_record:
                            if exec_record.status == "succeeded":
                                self.repo.update_step_status(
                                    s.id, status="succeeded", output_json=exec_record.output_json
                                )
                            elif exec_record.status in ("failed", "verification_failed", "cancelled"):
                                self.repo.update_step_status(
                                    s.id,
                                    status=exec_record.status,
                                    error_message=exec_record.error_message or "Execution interrupted by system restart.",
                                )
                                has_failed_step = True
                            else:
                                # Interrupted mid-flight
                                self.repo.update_step_status(
                                    s.id,
                                    status="failed",
                                    error_message="Step execution interrupted by crash or restart.",
                                )
                                has_failed_step = True
                    else:
                        self.repo.update_step_status(
                            s.id,
                            status="failed",
                            error_message="Step interrupted before tool dispatch.",
                        )
                        has_failed_step = True

            if has_failed_step:
                for s in steps:
                    if s.status in ("pending", "ready"):
                        self.repo.update_step_status(s.id, status="blocked")
                self.repo.update_run_status(
                    run.id,
                    status="failed",
                    error_message="Workflow execution was interrupted by system restart.",
                )

            reconciled_ids.append(run.id)
            self.ledger_repo.append(
                event_type="workflow_recovery_reconciled",
                task_id=run.task_id,
                correlation_id=run.correlation_id,
                payload_json={"workflow_id": run.id, "has_failed_step": has_failed_step},
                data_classification=run.data_classification,
            )

        self.db.commit()
        return reconciled_ids
