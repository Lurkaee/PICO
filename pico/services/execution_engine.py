import asyncio
import hashlib
import json
import secrets
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

        # 2.5 Input Preparation / Canonical Enrichment
        try:
            prepared_input = capability.prepare_input(dict(input_data), db=self.db)
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

        # 3. Input Validation against declared Pydantic schema
        try:
            capability.input_model.model_validate(prepared_input)
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
        input_digest = compute_input_digest(prepared_input)

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
            input_json=prepared_input,
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
        approver: str | None = None,
        notes: str | None = None,
        correlation_id: str | None = None,
        approval_token: str | None = None,
        authenticated_principal: str | None = None,
    ) -> ToolExecution:
        """
        Record explicit human approval for an awaiting_approval capability.
        Binds approval to the exact input_digest.
        Derives recorded approver from server-validated credentials.
        """
        execution = self.exec_repo.get_by_id(execution_id)
        if not execution:
            raise EntityNotFoundError("ToolExecution", execution_id)

        # Derive honest server-defined approval principal
        capability = self.registry.get(execution.capability_id, version=execution.capability_version)
        is_consequential = bool(capability.metadata.consequential) if capability else True

        if is_consequential:
            configured_secret = (settings.APPROVAL_SECRET_TOKEN or "").strip()
            if authenticated_principal == "local_operator":
                recorded_principal = "local_operator"
            elif not configured_secret or not approval_token or not secrets.compare_digest(approval_token, configured_secret):
                self.ledger_repo.append(
                    event_type="approval_denied",
                    execution_id=execution.id,
                    task_id=execution.task_id,
                    correlation_id=correlation_id,
                    capability_id=execution.capability_id,
                    payload_json={"reason": "Missing or invalid server approval secret token or operator session"},
                    data_classification=execution.data_classification,
                )
                action_desc = (
                    "Physical actuation"
                    if execution.capability_id == "homeassistant.test_light_actuate"
                    else "Application launch"
                    if execution.capability_id == "os.launch_application"
                    else "Filesystem modification"
                    if "filesystem" in execution.capability_id
                    else "Consequential action"
                )
                raise AuthorizationError(
                    f"{action_desc} approval fails closed without valid server-side APPROVAL_SECRET_TOKEN or active Operator Session."
                )
            else:
                recorded_principal = "authenticated_token_holder"
        elif authenticated_principal:
            recorded_principal = authenticated_principal
        elif approval_token and settings.APPROVAL_SECRET_TOKEN and secrets.compare_digest(approval_token, settings.APPROVAL_SECRET_TOKEN):
            recorded_principal = "authenticated_token_holder"
        else:
            # Fallback for internal direct-engine non-consequential invocations
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
            recorded_principal = clean_approver

        if execution.status != "awaiting_approval":
            raise ValidationError(
                f"Execution '{execution_id}' is in status '{execution.status}', expected 'awaiting_approval'."
            )

        # Retain caller-provided approver strictly as descriptive metadata
        meta = dict(execution.metadata_json or {})
        if approver:
            meta["claimed_approver"] = approver.strip()

        # Approve and transition to queued
        approved_item = self.exec_repo.record_approval(
            execution_id=execution_id,
            approver=recorded_principal,
            input_digest=execution.input_digest,
            metadata_json=meta,
        )

        self.ledger_repo.append(
            event_type="approval_recorded",
            execution_id=execution.id,
            task_id=execution.task_id,
            correlation_id=correlation_id,
            capability_id=execution.capability_id,
            payload_json={
                "approved_by": recorded_principal,
                "claimed_approver": approver.strip() if approver else None,
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
        *,
        claim_barrier: Any = None,
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

        # 1. Enforce permission at the real execution boundary
        required_perm = capability.metadata.required_permission
        effective_perms = caller_permissions or set()
        if required_perm and required_perm not in effective_perms:
            self.ledger_repo.append(
                event_type="authorization_denied",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                capability_version=capability.metadata.version,
                payload_json={"required_permission": required_perm, "caller_permissions": sorted(list(effective_perms))},
                data_classification=execution.data_classification,
            )
            raise AuthorizationError(
                f"Unauthorized: Caller lacks required permission '{required_perm}' for capability '{execution.capability_id}'."
            )

        # 2. Consequential action checks: human approval, single-use dispatch, and triple-digest agreement
        validated_input_snapshot = dict(execution.input_json)
        recomputed_digest = compute_input_digest(validated_input_snapshot)
        if capability.metadata.consequential:
            if not execution.approved_by or not execution.approved_at or not execution.approval_input_digest:
                raise AuthorizationError(f"Consequential capability '{execution.capability_id}' lacks human approval.")

            # Require all 3 to agree:
            # - Recomputed digest from current input
            # - Stored input digest
            # - Digest bound to approved execution
            if (
                execution.input_digest != recomputed_digest
                or execution.approval_input_digest != execution.input_digest
                or execution.approval_input_digest != recomputed_digest
            ):
                self.ledger_repo.append(
                    event_type="authorization_denied",
                    execution_id=execution.id,
                    task_id=execution.task_id,
                    correlation_id=correlation_id,
                    capability_id=execution.capability_id,
                    payload_json={
                        "reason": "Digest mismatch on execution",
                        "stored_digest": execution.input_digest,
                        "approved_digest": execution.approval_input_digest,
                        "recomputed_digest": recomputed_digest,
                    },
                    data_classification=execution.data_classification,
                )
                raise AuthorizationError(
                    "Approval invalidated: Persisted input digest does not match recomputed or approved digest."
                )

            # OS application launch profile verification at execution boundary
            if execution.capability_id == "os.launch_application":
                from pico.os_automation.allowlist import (
                    compute_launch_profile_fingerprint,
                    validate_launch_profile,
                )
                from pico.os_automation.manager import get_allowlist_manager

                alias = validated_input_snapshot.get("alias")
                bound_fp = validated_input_snapshot.get("launch_fingerprint")
                mgr = get_allowlist_manager()
                current_prof = mgr.get_profile(alias) if alias else None
                if not bound_fp or not current_prof:
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: Launch profile missing or not allowlisted at dispatch",
                            "alias": alias,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Application alias '{alias}' is missing or not allowlisted at dispatch."
                    )
                validated_curr_prof = validate_launch_profile(current_prof)
                current_fp = compute_launch_profile_fingerprint(validated_curr_prof)
                if current_fp != bound_fp:
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: Launch profile modified after approval",
                            "alias": alias,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Launch profile for alias '{alias}' was modified after approval was granted."
                    )

            # Filesystem staged diff verification at execution boundary
            elif execution.capability_id == "filesystem.apply_staged_diff":
                from pico.filesystem.adapter import get_filesystem_adapter
                from pico.filesystem.contracts import OperationType
                from pico.filesystem.roots import get_roots_manager
                from pico.repositories.staged_diff_repo import StagedDiffRepository

                pid = validated_input_snapshot.get("proposal_id")
                staged_repo = StagedDiffRepository(self.db)
                proposal = staged_repo.get_by_id(pid) if pid else None

                if not proposal or proposal.status != "staged":
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: Staged proposal missing or not in staged status",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Staged proposal '{pid}' is missing or not in 'staged' status at dispatch."
                    )

                # Re-verify root allowlist
                roots_mgr = get_roots_manager()
                try:
                    canonical_root, target_path = roots_mgr.resolve_safe_path(
                        root_id=proposal.root_id,
                        relative_path=proposal.relative_path,
                        must_exist=(proposal.operation == OperationType.REPLACE.value),
                        parent_must_exist=True,
                    )
                except Exception as exc:
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": f"Approval invalidated: Root allowlist or path check failed: {exc}",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Filesystem root or path check failed at dispatch: {exc}"
                    )

                adapter = get_filesystem_adapter()
                expected_orig = validated_input_snapshot.get("expected_original_state")
                if proposal.operation == OperationType.CREATE.value:
                    if adapter.file_exists(target_path, root_path=canonical_root):
                        self.ledger_repo.append(
                            event_type="authorization_denied",
                            execution_id=execution.id,
                            task_id=execution.task_id,
                            correlation_id=correlation_id,
                            capability_id=execution.capability_id,
                            payload_json={
                                "reason": "Approval invalidated: Target file already exists on disk prior to creation",
                                "proposal_id": pid,
                            },
                            data_classification=execution.data_classification,
                        )
                        raise AuthorizationError(
                            f"Approval invalidated: Target file already exists at '{proposal.relative_path}' prior to creation."
                        )
                else:
                    if not adapter.file_exists(target_path, root_path=canonical_root):
                        self.ledger_repo.append(
                            event_type="authorization_denied",
                            execution_id=execution.id,
                            task_id=execution.task_id,
                            correlation_id=correlation_id,
                            capability_id=execution.capability_id,
                            payload_json={
                                "reason": "Approval invalidated: Target file missing on disk prior to replacement",
                                "proposal_id": pid,
                            },
                            data_classification=execution.data_classification,
                        )
                        raise AuthorizationError(
                            f"Approval invalidated: Target file does not exist at '{proposal.relative_path}'."
                        )
                    curr_digest = adapter.get_content_digest(target_path, root_path=canonical_root)
                    if curr_digest != expected_orig:
                        self.ledger_repo.append(
                            event_type="authorization_denied",
                            execution_id=execution.id,
                            task_id=execution.task_id,
                            correlation_id=correlation_id,
                            capability_id=execution.capability_id,
                            payload_json={
                                "reason": "Approval invalidated: Target file modified after approval",
                                "proposal_id": pid,
                            },
                            data_classification=execution.data_classification,
                        )
                        raise AuthorizationError(
                            f"Approval invalidated: Target file content at '{proposal.relative_path}' was modified after approval was granted."
                        )

                # Verify staged content and diff digests have not been tampered with
                from pico.filesystem.contracts import compute_content_digest, compute_diff_digest
                if compute_content_digest(proposal.proposed_content) != validated_input_snapshot.get("expected_proposed_digest"):
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: Staged proposal content was modified after approval",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Staged proposal '{pid}' content was modified after approval was granted."
                    )

                if compute_diff_digest(proposal.unified_diff) != validated_input_snapshot.get("expected_diff_digest"):
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: Staged proposal unified diff was modified after approval",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Staged proposal '{pid}' unified diff was modified after approval was granted."
                    )

            elif execution.capability_id == "filesystem.rollback_staged_diff":
                from pico.filesystem.adapter import get_filesystem_adapter
                from pico.filesystem.roots import get_roots_manager
                from pico.repositories.staged_diff_repo import StagedDiffRepository

                pid = validated_input_snapshot.get("proposal_id")
                staged_repo = StagedDiffRepository(self.db)
                proposal = staged_repo.get_by_id(pid) if pid else None

                if not proposal or proposal.status != "applied":
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: Proposal missing or not in applied status",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Proposal '{pid}' is missing or not in 'applied' status at dispatch."
                    )

                roots_mgr = get_roots_manager()
                try:
                    canonical_root, target_path = roots_mgr.resolve_safe_path(
                        root_id=proposal.root_id,
                        relative_path=proposal.relative_path,
                        must_exist=True,
                        parent_must_exist=True,
                    )
                except Exception as exc:
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": f"Approval invalidated: Root allowlist or path check failed: {exc}",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Filesystem root or path check failed at dispatch: {exc}"
                    )

                adapter = get_filesystem_adapter()
                expected_applied = validated_input_snapshot.get("expected_applied_digest")
                if not adapter.file_exists(target_path, root_path=canonical_root):
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: Target file missing for rollback",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: Target file does not exist at '{proposal.relative_path}'."
                    )
                curr_digest = adapter.get_content_digest(target_path, root_path=canonical_root)
                if curr_digest != expected_applied:
                    self.ledger_repo.append(
                        event_type="authorization_denied",
                        execution_id=execution.id,
                        task_id=execution.task_id,
                        correlation_id=correlation_id,
                        capability_id=execution.capability_id,
                        payload_json={
                            "reason": "Approval invalidated: File modified externally since application",
                            "proposal_id": pid,
                        },
                        data_classification=execution.data_classification,
                    )
                    raise AuthorizationError(
                        f"Approval invalidated: File content at '{proposal.relative_path}' was modified externally since application."
                    )

            # Single-use approval and replay protection
            exec_metadata = dict(execution.metadata_json or {})
            if exec_metadata.get("dispatch_consumed") or exec_metadata.get("dispatched_at"):
                self.ledger_repo.append(
                    event_type="authorization_denied",
                    execution_id=execution.id,
                    task_id=execution.task_id,
                    correlation_id=correlation_id,
                    capability_id=execution.capability_id,
                    payload_json={"reason": "Replay attempt: approval ticket already consumed"},
                    data_classification=execution.data_classification,
                )
                raise AuthorizationError(
                    f"Approval for execution '{execution_id}' has already been consumed and cannot be replayed."
                )
        else:
            if execution.input_digest != recomputed_digest:
                raise AuthorizationError(
                    "Input integrity failure: Persisted input digest does not match recomputed digest."
                )

        # 3. Transition to running and consume approval ticket atomically via database compare-and-set claim
        barrier = claim_barrier or getattr(self, "_claim_barrier", None)
        if barrier is not None:
            if isinstance(barrier, asyncio.Barrier):
                await barrier.wait()
            elif callable(barrier):
                res = barrier()
                if asyncio.iscoroutine(res):
                    await res

        exec_metadata = dict(execution.metadata_json or {})
        if capability.metadata.consequential:
            exec_metadata["dispatch_consumed"] = True
            exec_metadata["dispatched_at"] = utc_now().isoformat()

        claimed = self.exec_repo.claim_for_execution(
            execution_id=execution_id,
            expected_digest=recomputed_digest,
            expected_input=validated_input_snapshot,
            expected_approval_digest=execution.approval_input_digest if capability.metadata.consequential else None,
            is_consequential=capability.metadata.consequential,
            metadata_json=exec_metadata,
        )
        if not claimed:
            self.ledger_repo.append(
                event_type="authorization_denied",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                payload_json={"reason": "Atomic claim failed: concurrent dispatch conflict, stale input, or already consumed ticket"},
                data_classification=execution.data_classification,
            )
            raise AuthorizationError(
                f"Execution '{execution_id}' could not be claimed: concurrent dispatch conflict, stale input, or already consumed ticket."
            )

        # 4. Post-claim integrity verification: Ensure refreshed persisted input exactly matches validated snapshot
        if (
            claimed.input_json != validated_input_snapshot
            or compute_input_digest(claimed.input_json) != recomputed_digest
            or claimed.input_digest != recomputed_digest
            or (capability.metadata.consequential and claimed.approval_input_digest != recomputed_digest)
        ):
            self.ledger_repo.append(
                event_type="authorization_denied",
                execution_id=execution.id,
                task_id=execution.task_id,
                correlation_id=correlation_id,
                capability_id=execution.capability_id,
                capability_version=capability.metadata.version,
                payload_json={
                    "reason": "Post-claim input snapshot divergence: persisted input does not match validated snapshot",
                    "execution_id": execution_id,
                },
                data_classification=execution.data_classification,
            )
            raise AuthorizationError(
                f"Execution '{execution_id}' input verification failed: persisted input was modified after validation."
            )

        execution = claimed
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
                execution, capability, str(exc), correlation_id, caller_permissions=effective_perms
            )

        # Output payload size enforcement
        if result.success and result.output_data:
            out_bytes = len(json.dumps(result.output_data).encode("utf-8"))
            if out_bytes > capability.metadata.max_output_bytes:
                err = f"Output size ({out_bytes} bytes) exceeded limit of {capability.metadata.max_output_bytes} bytes."
                return await self._handle_execution_failure(
                    execution, capability, err, correlation_id, caller_permissions=effective_perms
                )

        if not result.success:
            return await self._handle_execution_failure(
                execution, capability, result.error_message or "Handler reported failure.", correlation_id, caller_permissions=effective_perms
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
        caller_permissions: set[str] | None = None,
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
            return await self.execute(execution.id, caller_permissions=caller_permissions, correlation_id=correlation_id)
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
