import secrets
from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from pico.api.operator import get_operator_session_optional
from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError
from pico.db.session import get_db
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.procedural_memory_repo import ProceduralMemoryRepository
from pico.repositories.tool_execution_repo import ToolExecutionRepository
from pico.schemas.tools import (

    CapabilityResponse,
    LedgerEventListResponse,
    LedgerEventResponse,
    ProcedureValidationResponse,
    ToolApprovalRequest,
    ToolExecutionCreate,
    ToolExecutionListResponse,
    ToolExecutionResponse,
)
from pico.services.execution_engine import ToolExecutionEngine

router = APIRouter(prefix="/api/tools", tags=["Tool Execution Engine"])


# --- Capabilities Discovery ---
@router.get(
    "/capabilities",
    response_model=list[CapabilityResponse],
    summary="List all explicitly registered capabilities",
)
def list_capabilities() -> list[CapabilityResponse]:
    caps = registry.list()
    return [
        CapabilityResponse(
            capability_id=c.metadata.capability_id,
            version=c.metadata.version,
            name=c.metadata.name,
            description=c.metadata.description,
            risk_tier=c.metadata.risk_tier.value,
            required_permission=c.metadata.required_permission,
            required_classification=c.metadata.required_classification,
            timeout_seconds=c.metadata.timeout_seconds,
            max_output_bytes=c.metadata.max_output_bytes,
            idempotent=c.metadata.idempotent,
            max_retries=c.metadata.max_retries,
            consequential=c.metadata.consequential,
            preconditions=c.metadata.preconditions,
            expected_effects=c.metadata.expected_effects,
            input_schema=c.input_model.model_json_schema(),
            output_schema=c.output_model.model_json_schema(),
        )
        for c in caps
    ]


@router.get(
    "/capabilities/{capability_id}",
    response_model=CapabilityResponse,
    summary="Get details and schemas for a specific capability",
)
def get_capability(capability_id: str) -> CapabilityResponse:
    c = registry.get(capability_id)
    return CapabilityResponse(
        capability_id=c.metadata.capability_id,
        version=c.metadata.version,
        name=c.metadata.name,
        description=c.metadata.description,
        risk_tier=c.metadata.risk_tier.value,
        required_permission=c.metadata.required_permission,
        required_classification=c.metadata.required_classification,
        timeout_seconds=c.metadata.timeout_seconds,
        max_output_bytes=c.metadata.max_output_bytes,
        idempotent=c.metadata.idempotent,
        max_retries=c.metadata.max_retries,
        consequential=c.metadata.consequential,
        preconditions=c.metadata.preconditions,
        expected_effects=c.metadata.expected_effects,
        input_schema=c.input_model.model_json_schema(),
        output_schema=c.output_model.model_json_schema(),
    )


# --- Tool Executions ---
@router.post(
    "/executions",
    response_model=ToolExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a capability execution request",
)
async def submit_execution(
    payload: ToolExecutionCreate,
    execute_now: bool = Query(default=True, description="Immediately execute if non-consequential and queued"),
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    operator_session=Depends(get_operator_session_optional),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    engine = ToolExecutionEngine(db, registry)
    # Default local dev permissions grant standard development scopes (NEVER physical actuation)
    default_perms = {
        "tools:calculate",
        "tools:validate",
        "projects:read",
        "tasks:read",
        "system:diagnostics",
        "test:execute",
        "admin:consequential",
    }
    effective_perms = set(default_perms)
    configured_secret = (settings.APPROVAL_SECRET_TOKEN or "").strip()
    is_authorized = bool(operator_session is not None or (configured_secret and x_approval_token and secrets.compare_digest(x_approval_token, configured_secret)))

    # Physical actuation and OS automation require verified server-side APPROVAL_SECRET_TOKEN or active Operator Session
    if payload.capability_id == "homeassistant.test_light_actuate":
        if is_authorized:
            effective_perms.add("homeassistant:actuate")
    elif payload.capability_id == "os.inspect_windows":
        if is_authorized:
            effective_perms.add("os:inspect")
    elif payload.capability_id == "os.launch_application":
        if is_authorized:
            effective_perms.add("os:launch")
    elif payload.capability_id in ("filesystem.apply_staged_diff", "filesystem.rollback_staged_diff"):
        if is_authorized:
            effective_perms.add("filesystem:write")
    elif payload.capability_id == "browser.open_page":
        if is_authorized:
            effective_perms.add("browser:open")

    execution = engine.submit(
        capability_id=payload.capability_id,
        input_data=payload.input_data,
        capability_version=payload.capability_version,
        task_id=payload.task_id,
        session_id=payload.session_id,
        idempotency_key=payload.idempotency_key,
        caller_permissions=effective_perms,
        metadata=payload.metadata,
    )

    if execute_now and execution.status == "queued":
        execution = await engine.execute(execution.id, caller_permissions=effective_perms)

    return ToolExecutionResponse.model_validate(execution)


@router.get(
    "/executions",
    response_model=ToolExecutionListResponse,
    summary="List tool executions",
)
def list_executions(
    status_filter: str | None = Query(default=None, alias="status"),
    capability_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ToolExecutionListResponse:
    repo = ToolExecutionRepository(db)
    items = repo.list(
        status=status_filter,
        capability_id=capability_id,
        task_id=task_id,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    total = repo.count(status=status_filter)
    return ToolExecutionListResponse(
        items=[ToolExecutionResponse.model_validate(it) for it in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/executions/{execution_id}",
    response_model=ToolExecutionResponse,
    summary="Get execution status and verification result",
)
def get_execution(
    execution_id: str,
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    repo = ToolExecutionRepository(db)
    item = repo.get_by_id(execution_id)
    if not item:
        raise EntityNotFoundError("ToolExecution", execution_id)
    return ToolExecutionResponse.model_validate(item)


@router.post(
    "/executions/{execution_id}/execute",
    response_model=ToolExecutionResponse,
    summary="Execute a queued or approved tool execution",
)
async def run_execution(
    execution_id: str,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    operator_session=Depends(get_operator_session_optional),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    engine = ToolExecutionEngine(db, registry)
    repo = ToolExecutionRepository(db)
    item = repo.get_by_id(execution_id)
    if not item:
        raise EntityNotFoundError("ToolExecution", execution_id)

    default_perms = {
        "tools:calculate",
        "tools:validate",
        "projects:read",
        "tasks:read",
        "system:diagnostics",
        "test:execute",
    }
    effective_perms = set(default_perms)
    configured_secret = (settings.APPROVAL_SECRET_TOKEN or "").strip()
    is_authorized = bool(
        operator_session is not None
        or (configured_secret and x_approval_token and secrets.compare_digest(x_approval_token, configured_secret))
    )

    if is_authorized:
        effective_perms.add("admin:consequential")
        if item.capability_id == "homeassistant.test_light_actuate":
            effective_perms.add("homeassistant:actuate")
        elif item.capability_id == "os.inspect_windows":
            effective_perms.add("os:inspect")
        elif item.capability_id == "os.launch_application":
            effective_perms.add("os:launch")
        elif item.capability_id in ("filesystem.apply_staged_diff", "filesystem.rollback_staged_diff"):
            effective_perms.add("filesystem:write")
        elif item.capability_id == "browser.open_page":
            effective_perms.add("browser:open")

    execution = await engine.execute(execution_id, caller_permissions=effective_perms)
    return ToolExecutionResponse.model_validate(execution)


@router.post(
    "/executions/{execution_id}/approve",
    response_model=ToolExecutionResponse,
    summary="Explicit human approval for consequential action",
)
def approve_execution(
    execution_id: str,
    payload: ToolApprovalRequest,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    operator_session=Depends(get_operator_session_optional),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    repo = ToolExecutionRepository(db)
    item = repo.get_by_id(execution_id)
    if not item:
        raise EntityNotFoundError("ToolExecution", execution_id)

    capability = registry.get(item.capability_id, version=item.capability_version)
    is_consequential = bool(capability.metadata.consequential) if capability else True

    configured_secret = (settings.APPROVAL_SECRET_TOKEN or "").strip()
    has_token = bool(configured_secret and x_approval_token and secrets.compare_digest(x_approval_token, configured_secret))
    has_session = operator_session is not None

    if is_consequential:
        if not has_token and not has_session:
            raise AuthorizationError(
                "Consequential action approval fails closed without valid X-Pico-Approval-Token or active Operator Session."
            )
        authenticated_principal = "authenticated_token_holder" if has_token else "local_operator"
        authoritative_approver = operator_session.approver if has_session else "authenticated_token_holder"
    elif has_token:
        authenticated_principal = "authenticated_token_holder"
        authoritative_approver = "authenticated_token_holder"
    elif has_session:
        authenticated_principal = "local_operator"
        authoritative_approver = operator_session.approver
    else:
        raise AuthorizationError(
            "Action approval fails closed without valid X-Pico-Approval-Token or active Operator Session."
        )

    engine = ToolExecutionEngine(db, registry)
    execution = engine.approve(
        execution_id=execution_id,
        approver=authoritative_approver,
        notes=payload.notes,
        approval_token=x_approval_token,
        authenticated_principal=authenticated_principal,
    )
    return ToolExecutionResponse.model_validate(execution)


@router.post(
    "/executions/{execution_id}/cancel",
    response_model=ToolExecutionResponse,
    summary="Cancel a queued or awaiting-approval execution",
)
def cancel_execution(
    execution_id: str,
    reason: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    engine = ToolExecutionEngine(db, registry)
    execution = engine.cancel(execution_id, reason=reason)
    return ToolExecutionResponse.model_validate(execution)


@router.get(
    "/executions/{execution_id}/events",
    response_model=LedgerEventListResponse,
    summary="Get append-only audit event trace for an execution",
)
def get_execution_events(
    execution_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> LedgerEventListResponse:
    repo = EventLedgerRepository(db)
    events = repo.list_events(execution_id=execution_id, limit=limit, offset=offset)
    total = repo.count(execution_id=execution_id)
    return LedgerEventListResponse(
        items=[LedgerEventResponse.model_validate(ev) for ev in events],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- Procedural Memory Integration ---
@router.post(
    "/procedures/{procedure_id}/validate",
    response_model=ProcedureValidationResponse,
    summary="Validate a procedural memory skill against the capability registry",
)
def validate_procedure(
    procedure_id: str,
    db: Session = Depends(get_db),
) -> ProcedureValidationResponse:
    proc_repo = ProceduralMemoryRepository(db)
    proc = proc_repo.get_by_id(procedure_id)
    if not proc:
        raise EntityNotFoundError("ProceduralMemory", procedure_id)

    steps = proc.steps_json or []
    resolved = []
    missing = []
    requires_approval = False
    details = []

    for idx, s in enumerate(steps):
        # Steps might reference capability_id or action
        cap_id = s.get("capability_id") or s.get("action")
        if cap_id and registry.has(cap_id):
            cap = registry.get(cap_id)
            resolved.append(cap_id)
            if cap.metadata.consequential:
                requires_approval = True
            details.append({
                "step_index": idx,
                "capability_id": cap_id,
                "status": "resolved",
                "risk_tier": cap.metadata.risk_tier.value,
                "consequential": cap.metadata.consequential,
            })
        else:
            missing.append(cap_id or f"step_{idx}")
            details.append({
                "step_index": idx,
                "capability_id": cap_id,
                "status": "missing_or_unregistered",
            })

    is_valid = len(missing) == 0 and len(resolved) > 0 and proc.lifecycle_status != "retired"
    return ProcedureValidationResponse(
        procedure_id=procedure_id,
        is_valid=is_valid,
        total_steps=len(steps),
        resolved_capabilities=resolved,
        missing_capabilities=missing,
        requires_approval=requires_approval,
        details=details,
    )
