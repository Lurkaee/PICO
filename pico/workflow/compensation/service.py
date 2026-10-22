"""Workflow compensation service executing controlled, reverse-dependency compensation (Milestone P9.3)."""

from typing import Any
from sqlalchemy.orm import Session

from pico.capabilities import CapabilityRegistry, registry as default_registry
from pico.core.errors import EntityNotFoundError, ValidationError
from pico.models.workflow import WorkflowRun, WorkflowStep
from pico.models.workflow_compensation import WorkflowCompensation
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.workflow_repo import WorkflowRepository
from pico.services.execution_engine import ToolExecutionEngine, compute_input_digest
from pico.workflow.compensation.registry import CompensationRegistry, default_compensation_registry


class WorkflowCompensationService:
    """
    Coordinates verified compensation for completed workflow steps.
    Executes in deterministic reverse-dependency order through ToolExecutionEngine.
    Enforces that consequential compensation actions require explicit human operator approval.
    """

    def __init__(
        self,
        db: Session,
        capability_registry: CapabilityRegistry | None = None,
        compensation_registry: CompensationRegistry | None = None,
        execution_engine: ToolExecutionEngine | None = None,
    ) -> None:
        self.db = db
        self.capability_registry = capability_registry or default_registry
        self.comp_registry = compensation_registry or default_compensation_registry
        self.repo = WorkflowRepository(db)
        self.ledger_repo = EventLedgerRepository(db)
        self.engine = execution_engine or ToolExecutionEngine(
            db, capability_registry=self.capability_registry
        )

    def register_step_compensation(
        self,
        step: WorkflowStep,
        original_input: dict[str, Any] | None = None,
        original_output: dict[str, Any] | None = None,
        run: WorkflowRun | None = None,
        execution_id: str | None = None,
    ) -> WorkflowCompensation | None:
        """
        Registers an explicit compensation action if a verified compensation definition exists.
        """
        defn = self.comp_registry.get_compensation(step.capability_id)
        if not defn:
            return None

        orig_in = original_input if original_input is not None else (step.resolved_input_json or {})
        orig_out = original_output if original_output is not None else (step.output_json or {})
        exec_id = execution_id or step.tool_execution_id

        # Build validated compensation input
        comp_input = defn.input_builder(orig_in, orig_out)
        comp_digest = compute_input_digest(comp_input)


        comp = self.repo.create_compensation(
            workflow_run_id=step.workflow_run_id,
            workflow_step_id=step.id,
            original_tool_execution_id=exec_id,
            compensation_capability_id=defn.target_capability_id,
            compensation_capability_version=defn.target_capability_version,
            status="registered",
            input_json=comp_input,
            input_digest=comp_digest,
            order_index=step.order_index,
            reason=f"Registered compensation for step '{step.step_key}'",
        )

        self.ledger_repo.append(
            event_type="workflow_compensation_registered",
            task_id=step.workflow_run.task_id if step.workflow_run else None,
            correlation_id=step.workflow_run.correlation_id if step.workflow_run else None,
            execution_id=step.tool_execution_id,
            capability_id=defn.target_capability_id,
            capability_version=defn.target_capability_version,
            payload_json={
                "workflow_id": step.workflow_run_id,
                "step_key": step.step_key,
                "compensation_id": comp.id,
                "target_capability": defn.target_capability_id,
            },
            data_classification=step.workflow_run.data_classification if step.workflow_run else "INTERNAL",
        )
        self.db.commit()
        return comp

    async def compensate_workflow(
        self,
        run_id: str,
        caller_permissions: set[str] | list[str] | None = None,
        reason: str | None = None,
    ) -> WorkflowRun:
        """
        Executes registered compensations for a workflow run in reverse dependency order.
        Consequential actions halt at awaiting_approval and require explicit human authorization.
        """
        run = self.repo.get_by_id(run_id)
        if not run:
            raise EntityNotFoundError("WorkflowRun", run_id)

        effective_perms = set(caller_permissions or [])
        compensations = self.repo.get_compensations_for_run(run.id)

        if not compensations:
            # No compensations registered
            self.ledger_repo.append(
                event_type="workflow_compensation_empty",
                task_id=run.task_id,
                correlation_id=run.correlation_id,
                payload_json={"workflow_id": run.id, "reason": "No compensable steps found."},
                data_classification=run.data_classification,
            )
            self.db.commit()
            return run

        self.ledger_repo.append(
            event_type="workflow_compensation_started",
            task_id=run.task_id,
            correlation_id=run.correlation_id,
            payload_json={"workflow_id": run.id, "count": len(compensations), "reason": reason},
            data_classification=run.data_classification,
        )

        for comp in compensations:
            if comp.status == "succeeded":
                continue

            # Check if execution already exists
            exec_record = None
            if comp.compensation_tool_execution_id:
                exec_record = self.engine.exec_repo.get_by_id(comp.compensation_tool_execution_id)

            if not exec_record:
                # Submit compensation action through ToolExecutionEngine
                target_cap = self.capability_registry.get(
                    comp.compensation_capability_id, version=comp.compensation_capability_version
                )
                try:
                    exec_record = self.engine.submit(
                        capability_id=comp.compensation_capability_id,
                        input_data=comp.input_json,
                        capability_version=comp.compensation_capability_version,
                        task_id=run.task_id,
                        session_id=run.session_id,
                        correlation_id=run.correlation_id,
                        caller_permissions=effective_perms,
                        context_classification=run.data_classification,
                    )
                except Exception as exc:
                    self.repo.update_compensation_status(
                        comp.id, status="failed", reason=f"Submission failed: {exc}"
                    )
                    self.db.commit()
                    break

                self.repo.update_compensation_status(
                    comp.id,
                    status="awaiting_approval" if exec_record.status == "awaiting_approval" else "running",
                    compensation_tool_execution_id=exec_record.id,
                )
                self.db.commit()

            # Consequential check: if awaiting approval, pause workflow compensation
            if exec_record.status == "awaiting_approval":
                self.repo.update_run_status(run.id, status="awaiting_approval")
                self.ledger_repo.append(
                    event_type="workflow_compensation_awaiting_approval",
                    task_id=run.task_id,
                    correlation_id=run.correlation_id,
                    execution_id=exec_record.id,
                    payload_json={"workflow_id": run.id, "compensation_id": comp.id},
                    data_classification=run.data_classification,
                )
                self.db.commit()
                return self.repo.get_by_id(run.id) or run

            # Execute approved or non-consequential compensation
            exec_result = await self.engine.execute(
                exec_record.id, caller_permissions=effective_perms
            )

            if exec_result.status == "succeeded":
                self.repo.update_compensation_status(comp.id, status="completed")
                self.repo.update_step_status(comp.workflow_step_id, status="compensated")
                self.ledger_repo.append(
                    event_type="workflow_compensation_succeeded",
                    task_id=run.task_id,
                    correlation_id=run.correlation_id,
                    execution_id=exec_record.id,
                    payload_json={"workflow_id": run.id, "compensation_id": comp.id},
                    data_classification=run.data_classification,
                )
            else:
                failure_reason = exec_result.error_message or "Compensation execution failed."
                self.repo.update_compensation_status(
                    comp.id, status="failed", reason=failure_reason
                )
                self.ledger_repo.append(
                    event_type="workflow_compensation_failed",
                    task_id=run.task_id,
                    correlation_id=run.correlation_id,
                    execution_id=exec_record.id,
                    payload_json={"workflow_id": run.id, "compensation_id": comp.id, "error": failure_reason},
                    data_classification=run.data_classification,
                )
                self.db.commit()
                # Stop automation on failure; require operator intervention without blind retry
                break

            self.db.commit()

        # Update overall workflow run status based on compensation outcomes
        all_comps = self.repo.get_compensations_for_run(run.id)
        if any(c.status == "awaiting_approval" for c in all_comps):
            self.repo.update_run_status(run.id, status="awaiting_approval")
        elif any(c.status == "failed" for c in all_comps):
            self.repo.update_run_status(run.id, status="failed")
        elif all(c.status == "completed" for c in all_comps) and all_comps:
            self.repo.update_run_status(run.id, status="compensated")
            self.ledger_repo.append(
                event_type="workflow_compensated",
                task_id=run.task_id,
                correlation_id=run.correlation_id,
                payload_json={"workflow_id": run.id, "compensations_count": len(all_comps)},
                data_classification=run.data_classification,
            )
        self.db.commit()

        refreshed = self.repo.get_by_id(run.id)
        return refreshed or run

    # Alias for compatibility with WorkflowService
    compensate_run = compensate_workflow
