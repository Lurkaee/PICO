import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from sqlalchemy.orm import Session

from pico.capabilities.base import BaseCapability, ExecutionContext
from pico.capabilities.registry import CapabilityRegistry, registry as default_registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.models.tool_execution import ToolExecution
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.tool_execution_repo import ToolExecutionRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_input_digest(input_data: dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 digest of normalized JSON input."""
    canonical_json = json.dumps(input_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class ToolExecutionEngine:
    def __init__(
        self,
        db: Session,
        capability_registry: CapabilityRegistry | None = None,
    ) -> None:
        self.db = db
        self.registry = capability_registry or default_registry
        self.exec_repo = ToolExecutionRepository(db)
        self.ledger_repo = EventLedgerRepository(db)

    def submit(
        self,
        capability_id: str,
        input_data: dict[str, Any],
        capability_version: str = "1.0.0",
        task_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        caller_permissions: set[str] | None = None,
        context_classification: str = "INTERNAL",
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolExecution:
        """
        Submit a capability execution request with schema validation,
        idempotency check, and authorization gating.
        """
        # 1. Idempotency Check
        if idempotency_key and idempotency_key.strip():
            existing = self.exec_repo.get_by_idempotency_key(idempotency_key.strip())
            if existing:
                self.ledger_repo.append(
                    event_type="idempotent_submission_matched",
                    execution_id=existing.id,
                    task_id=existing.task_id,
                    correlation_id=correlation_id,
                    capability_id=existing.capability_id,
                    payload_json={"idempotency_key": idempotency_key, "current_status": existing.status},
                    data_classification=existing.data_classification,
                )
                return existing

        # 2. Capability Resolution
        if not self.registry.has(capability_id):
            self.ledger_repo.append(
                event_type="authorization_denied",
                task_id=task_id,
                correlation_id=correlation_id,
                capability_id=capability_id,
                payload_json={"reason": "Unknown or unregistered capability"},
                data_classification=context_classification,
            )
            raise EntityNotFoundError("Capability", capability_id)

        capability = self.registry.get(capability_id, version=capability_version)

        # 3. Input Validation against declared Pydantic schema
        try:
            capability.input_model.model_validate(input_data)
        except Exception as exc:
            self.ledger_repo.append(
                event_type="validation_failed",
                task_id=task_id,
                correlation_id=correlation_id,
                capability_id=capability_id,
                payload_json={"error": str(exc)},
                data_classification=context_classification,
            )
            raise ValidationError(f"Invalid input for capability '{capability_id}': {str(exc)}")

        # 4. Input Digest Calculation
        input_digest = compute_input_digest(input_data)

        # 5. Permission & Authorization Check
        required_perm = capability.metadata.required_permission
        effective_perms = caller_permissions or set()
        if required_perm and required_perm not in effective_perms:
            self.ledger_repo.append(
                event_type="authorization_denied",
                task_id=task_id,
                correlation_id=correlation_id,
                capability_id=capability_id,
                capability_version=capability.metadata.version,
                payload_json={"required_permission": required_perm, "caller_permissions": sorted(list(effective_perms))},
                data_classification=context_classification,
            )
            raise AuthorizationError(
                f"Unauthorized: Caller lacks required permission '{required_perm}' for capability '{capability_id}'."
            )

        # 6. Check if consequential action requires prior approval
        initial_status = "awaiting_approval" if capability.metadata.consequential else "queued"

        # 7. Create Execution Record
        execution = self.exec_repo.create(
            capability_id=capability_id,
            input_json=input_data,
            input_digest=input_digest,
            capability_version=capability.metadata.version,
            task_id=task_id,
            session_id=session_id,
            idempotency_key=idempotency_key.strip() if idempotency_key else None,
            status=initial_status,
            required_permission=required_perm,
            timeout_seconds=capability.metadata.timeout_seconds,
            max_retries=capability.metadata.max_retries,
            data_classification=context_classification,
            metadata_json=metadata,
        )

        # 8. Record Ledger Events
        self.ledger_repo.append(
            event_type="task_submitted",
            execution_id=execution.id,
            task_id=task_id,
            correlation_id=correlation_id,
            capability_id=capability_id,
            capability_version=capability.metadata.version,
            payload_json={"input_digest": input_digest, "status": initial_status},
            data_classification=context_classification,
        )

        if capability.metadata.consequential:
            self.ledger_repo.append(
                event_type="approval_required",
                execution_id=execution.id,
                task_id=task_id,
                correlation_id=correlation_id,
                capability_id=capability_id,
                payload_json={"input_digest": input_digest, "risk_tier": capability.metadata.risk_tier.value},
                data_classification=context_classification,
            )

        return execution

    def approve(
        self,
        execution_id: str,
        approver: str,
        notes: str | None = None,
        correlation_id: str | None = None,
    ) -> ToolExecution:
        """
        Record explicit human approval for an awaiting_approval capability.
        Binds approval to the exact input_digest.
        """
        execution = self.exec_repo.get_by_id(execution_id)
        if not execution:
            raise EntityNotFoundError("ToolExecution", execution_id)

        clean_approver = (approver or "").strip()
        if not clean_approver or clean_approver not in settings.TRUSTED_APPROVER_IDENTITIES:
            self.ledger_repo.append(
                event_type="approval_denied",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json={"attempted_approver": clean_approver, "reason": "Untrusted approver identity"},
                data_classification=execution.data_classification,
            )
            raise AuthorizationError(
                f"Principal '{clean_approver}' is not authorized to approve executions. Must be in {settings.TRUSTED_APPROVER_IDENTITIES}."
            )

        if execution.status != "awaiting_approval":
            raise ValidationError(
                f"Execution '{execution_id}' is in status '{execution.status}', expected 'awaiting_approval'."
            )

        # Approve and transition to queued
        approved_item = self.exec_repo.record_approval(
            execution_id=execution_id,
            approver=clean_approver,
            input_digest=execution.input_digest,
        )

        self.ledger_repo.append(
            event_type="approval_recorded",
            execution_id=execution.id,
            task_id=execution.task_id,
            correlation_id=correlation_id,
            capability_id=execution.capability_id,
            payload_json={
                "approved_by": clean_approver,
                "input_digest": execution.input_digest,
                "notes": notes,
            },
            data_classification=execution.data_classification,
        )
        return approved_item

    async def execute(
        self,
        execution_id: str,
        caller_permissions: set[str] | None = None,
        correlation_id: str | None = None,
    ) -> ToolExecution:
        """
        Executes a queued execution item through handler sandbox and independent verifier.
        """
        execution = self.exec_repo.get_by_id(execution_id)
        if not execution:
            raise EntityNotFoundError("ToolExecution", execution_id)

        if execution.status not in ("queued", "retry_scheduled"):
            raise ValidationError(
                f"Execution '{execution_id}' is in status '{execution.status}', cannot execute."
            )

        capability = self.registry.get(execution.capability_id, version=execution.capability_version)

        # Consequential check: ensure approval is valid and input hasn't mutated
        if capability.metadata.consequential:
            if not execution.approved_by or not execution.approved_at:
                raise AuthorizationError(f"Consequential capability '{execution.capability_id}' lacks human approval.")
            if execution.approval_input_digest != execution.input_digest:
                raise AuthorizationError("Approval invalidated: Input digest does not match approved digest.")

        # Transition to running
        execution = self.exec_repo.update_status(execution_id, status="running")
        self.ledger_repo.append(
            event_type="execution_started",
            execution_id=execution.id,
            task_id=execution.task_id,
            correlation_id=correlation_id,
            capability_id=execution.capability_id,
            capability_version=execution.capability_version,
            payload_json={"retry_count": execution.retry_count},
            data_classification=execution.data_classification,
        )

        ctx = ExecutionContext(
            execution_id=execution.id,
            task_id=execution.task_id,
            session_id=execution.session_id,
            correlation_id=correlation_id,
            caller_permissions=caller_permissions or set(),
            context_classification=execution.data_classification,
            approver=execution.approved_by,
            db=self.db,
        )

        # Execute handler with timeout enforcement
        timeout = execution.timeout_seconds
        try:
            handler_coro = capability.execute(execution.input_json, ctx)
            result = await asyncio.wait_for(handler_coro, timeout=timeout)
        except asyncio.TimeoutError:
            self.ledger_repo.append(
                event_type="timed_out",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json={"timeout_seconds": timeout},
                data_classification=execution.data_classification,
            )
            return self.exec_repo.update_status(
                execution_id,
                status="timed_out",
                error_message=f"Execution exceeded deadline of {timeout}s.",
            )
        except Exception as exc:
            return await self._handle_execution_failure(
                execution, capability, str(exc), correlation_id
            )

        # Output payload size enforcement
        if result.success and result.output_data:
            out_bytes = len(json.dumps(result.output_data).encode("utf-8"))
            if out_bytes > capability.metadata.max_output_bytes:
                err = f"Output size ({out_bytes} bytes) exceeded limit of {capability.metadata.max_output_bytes} bytes."
                return await self._handle_execution_failure(
                    execution, capability, err, correlation_id
                )

        if not result.success:
            return await self._handle_execution_failure(
                execution, capability, result.error_message or "Handler reported failure.", correlation_id
            )

        # Transition to verifying
        execution = self.exec_repo.update_status(
            execution_id,
            status="verifying",
            output_json=result.output_data,
        )
        self.ledger_repo.append(
            event_type="verification_started",
            execution_id=execution.id,
            task_id=execution.task_id,
            correlation_id=correlation_id,
            capability_id=execution.capability_id,
            payload_json={},
            data_classification=execution.data_classification,
        )

        # Independent Deterministic Verifier
        ver_res = await capability.verify(
            execution.input_json, result.output_data or {}, result.evidence, ctx
        )

        if ver_res.passed:
            self.ledger_repo.append(
                event_type="verification_passed",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json=ver_res.details,
                data_classification=execution.data_classification,
            )
            self.ledger_repo.append(
                event_type="execution_succeeded",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json={"retry_count": execution.retry_count},
                data_classification=execution.data_classification,
            )
            return self.exec_repo.update_status(
                execution_id,
                status="succeeded",
                output_json=result.output_data,
                verification_status="passed",
                verification_details_json=ver_res.details,
            )
        else:
            self.ledger_repo.append(
                event_type="verification_failed",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json={"reason": ver_res.reason, "details": ver_res.details},
                data_classification=execution.data_classification,
            )
            return self.exec_repo.update_status(
                execution_id,
                status="verification_failed",
                error_message=f"Independent verification failed: {ver_res.reason}",
                verification_status="failed",
                verification_details_json=ver_res.details,
            )

    async def _handle_execution_failure(
        self,
        execution: ToolExecution,
        capability: BaseCapability,
        error_msg: str,
        correlation_id: str | None,
    ) -> ToolExecution:
        # Check retry eligibility: capability must be idempotent and retry limit not reached
        can_retry = capability.metadata.idempotent and execution.retry_count < execution.max_retries

        if can_retry:
            self.ledger_repo.append(
                event_type="retry_scheduled",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json={"retry_attempt": execution.retry_count + 1, "error": error_msg},
                data_classification=execution.data_classification,
            )
            updated = self.exec_repo.update_status(
                execution.id,
                status="retry_scheduled",
                error_message=error_msg,
                increment_retry=True,
            )
            # Re-execute immediately or caller can trigger next attempt
            return await self.execute(execution.id, correlation_id=correlation_id)
        else:
            self.ledger_repo.append(
                event_type="execution_failed",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json={"error": error_msg, "retry_count": execution.retry_count},
                data_classification=execution.data_classification,
            )
            return self.exec_repo.update_status(
                execution.id,
                status="failed",
                error_message=error_msg,
            )

    def cancel(
        self, execution_id: str, reason: str | None = None, correlation_id: str | None = None
    ) -> ToolExecution:
        """Cancel a pending, queued, or awaiting_approval execution."""
        execution = self.exec_repo.get_by_id(execution_id)
        if not execution:
            raise EntityNotFoundError("ToolExecution", execution_id)

        cancellable_statuses = ("queued", "awaiting_approval", "retry_scheduled")
        if execution.status not in cancellable_statuses:
            raise ValidationError(
                f"Cannot cancel execution '{execution_id}' in status '{execution.status}'."
            )

        updated = self.exec_repo.update_status(
            execution_id,
            status="cancelled",
            error_message=reason or "Cancelled by user request.",
        )
        self.ledger_repo.append(
            event_type="cancelled",
            execution_id=execution.id,
            task_id=execution.task_id,
            correlation_id=correlation_id,
            capability_id=execution.capability_id,
            payload_json={"reason": reason},
            data_classification=execution.data_classification,
        )
        return updated

    def recover_interrupted_executions(self) -> list[str]:
        """
        Recovers from server crash/restart by marking executions stuck in 'running'
        or 'verifying' as failed with 'INTERRUPTED_BY_RESTART'.
        Ensures unfinished tasks are never silently left hanging or reported succeeded.
        """
        interrupted = self.exec_repo.list(status="running") + self.exec_repo.list(status="verifying")
        recovered_ids = []
        for item in interrupted:
            self.exec_repo.update_status(
                item.id,
                status="failed",
                error_message="Interrupted by system restart or process termination.",
            )
            self.ledger_repo.append(
                event_type="recovered",
                execution_id=item.id,
                task_id=item.task_id,
                capability_id=item.capability_id,
                payload_json={"previous_status": item.status, "reason": "INTERRUPTED_BY_RESTART"},
                data_classification=item.data_classification,
            )
            recovered_ids.append(item.id)
        return recovered_ids
