"""Workflow verification service for independent auditing of step outputs (Milestone P9.3)."""

from typing import Any
from sqlalchemy.orm import Session

from pico.capabilities import CapabilityRegistry, registry as default_registry
from pico.models.tool_execution import ToolExecution
from pico.models.workflow import WorkflowRun, WorkflowStep
from pico.models.workflow_verification import WorkflowVerification
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.workflow_repo import WorkflowRepository
from pico.workflow.verification.policies import get_verification_policy
from pico.workflow.verification.types import (
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
)


class WorkflowVerificationService:
    """
    Coordinates multi-stage independent verification of workflow step outputs before
    releasing results to downstream dependent steps.
    """

    def __init__(
        self,
        db: Session,
        capability_registry: CapabilityRegistry | None = None,
        scheduler: Any = None,
    ) -> None:
        self.db = db
        self.registry = capability_registry or default_registry
        self.repo = WorkflowRepository(db)
        self.ledger_repo = EventLedgerRepository(db)
        self._scheduler = scheduler

    def _get_scheduler(self) -> Any:
        if self._scheduler is not None:
            return self._scheduler
        try:
            from pico.scheduler import get_scheduler
            return get_scheduler()
        except Exception:
            return None

    async def verify_step(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        execution: ToolExecution | str | None = None,
        execution_id: str | None = None,
        output_data: dict[str, Any] | None = None,
        policy_id: str | None = None,
        step_goal_or_description: str | None = None,
    ) -> VerificationResult:
        """
        Executes independent verification policy for a completed tool execution.
        Persists durable WorkflowVerification record and audit ledger entries.
        """
        policy = get_verification_policy(policy_id)
        scheduler = self._get_scheduler()

        exec_id = execution.id if isinstance(execution, ToolExecution) else (execution or execution_id or step.tool_execution_id)
        exec_record = execution if isinstance(execution, ToolExecution) else (
            self.db.query(ToolExecution).filter_by(id=exec_id).first() if exec_id else None
        )

        resolved_in = step.resolved_input_json or (exec_record.input_json if exec_record else {}) or {}
        resolved_out = output_data if output_data is not None else ((exec_record.output_json if exec_record else {}) or step.output_json or {})

        req = VerificationRequest(
            workflow_run_id=run.id,
            step_id=step.id,
            step_key=step.step_key,
            capability_id=step.capability_id,
            capability_version=step.capability_version,
            resolved_input=resolved_in,
            output_data=resolved_out,
            evidence=(exec_record.verification_details_json if exec_record and exec_record.verification_details_json else {}),
            tool_execution_id=exec_id,
            tool_execution_record=exec_record,
            data_classification=run.data_classification,
            step_goal_or_description=step_goal_or_description,
            policy_override=policy_id,
        )

        # Audit ledger log: verification started
        self.ledger_repo.append(
            event_type="workflow_verification_started",
            task_id=run.task_id,
            correlation_id=run.correlation_id,
            execution_id=exec_id,
            capability_id=step.capability_id,
            capability_version=step.capability_version,
            payload_json={
                "workflow_id": run.id,
                "step_key": step.step_key,
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
            },
            data_classification=run.data_classification,
        )

        result = await policy.verify(req, self.registry, self.db, scheduler=scheduler)

        # Persist durable verification record
        self.repo.create_verification(
            workflow_run_id=run.id,
            workflow_step_id=step.id,
            tool_execution_id=exec_id,
            policy_id=result.policy_id,
            policy_version=result.policy_version,
            verification_method=result.method.value,
            status=result.status.value,
            stage=result.stage.value,
            input_digest=result.input_digest,
            output_digest=result.output_digest,
            evidence=result.evidence,
            findings=result.findings,
            reason=result.reason,
            data_classification=result.data_classification,
        )

        # Audit ledger log: verification decision
        event_name = (
            "workflow_verification_passed"
            if result.status == VerificationStatus.PASSED
            else "workflow_verification_failed"
            if result.status == VerificationStatus.FAILED
            else "workflow_verification_inconclusive"
        )
        self.ledger_repo.append(
            event_type=event_name,
            task_id=run.task_id,
            correlation_id=run.correlation_id,
            execution_id=exec_id,
            capability_id=step.capability_id,
            capability_version=step.capability_version,
            payload_json={
                "workflow_id": run.id,
                "step_key": step.step_key,
                "status": result.status.value,
                "stage": result.stage.value,
                "method": result.method.value,
                "reason": result.reason,
            },
            data_classification=run.data_classification,
        )

        self.db.commit()
        return result
