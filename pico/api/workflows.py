"""REST API routes for workflow coordination (Milestone P9.1)."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pico.db.session import get_db
from pico.schemas.workflow import (
    WorkflowAdvanceRequest,
    WorkflowCancelRequest,
    WorkflowCompensateRequest,
    WorkflowCompensationResponse,
    WorkflowCreate,
    WorkflowListResponse,
    WorkflowRunResponse,
    WorkflowVerificationResponse,
)
from pico.workflow.service import WorkflowService

router = APIRouter(prefix="/api/workflows", tags=["Workflows"])


@router.post(
    "",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit workflow",
    description="Registers a validated, bounded dependency workflow graph for execution.",
)
def submit_workflow(
    payload: WorkflowCreate,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    service = WorkflowService(db)
    return service.submit_workflow(payload)


@router.get(
    "",
    response_model=WorkflowListResponse,
    summary="List workflows",
    description="Lists and filters workflow runs with pagination.",
)
def list_workflows(
    task_id: str | None = Query(default=None, description="Filter by parent task ID"),
    status: str | None = Query(default=None, description="Filter by workflow run status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> WorkflowListResponse:
    service = WorkflowService(db)
    items, total = service.list_workflows(
        task_id=task_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return WorkflowListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{workflow_id}",
    response_model=WorkflowRunResponse,
    summary="Get workflow by ID",
    description="Retrieves a workflow run and the status of its composed steps.",
)
def get_workflow(
    workflow_id: str,
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    service = WorkflowService(db)
    return service.get_workflow(workflow_id)


@router.post(
    "/{workflow_id}/advance",
    response_model=WorkflowRunResponse,
    summary="Advance workflow execution",
    description="Dispatches eligible ready steps in dependency order through the execution engine with verification gating.",
)
async def advance_workflow(
    workflow_id: str,
    payload: WorkflowAdvanceRequest = WorkflowAdvanceRequest(),
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    service = WorkflowService(db)
    return await service.advance_workflow(
        run_id=workflow_id,
        caller_permissions=set(payload.caller_permissions),
        max_steps=payload.max_steps,
    )


@router.post(
    "/{workflow_id}/cancel",
    response_model=WorkflowRunResponse,
    summary="Cancel workflow run",
    description="Cancels an active workflow run and stops pending steps.",
)
def cancel_workflow(
    workflow_id: str,
    payload: WorkflowCancelRequest = WorkflowCancelRequest(),
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    service = WorkflowService(db)
    return service.cancel_workflow(workflow_id, reason=payload.reason)


@router.get(
    "/{workflow_id}/verifications",
    response_model=list[WorkflowVerificationResponse],
    summary="Get workflow verification records",
    description="Retrieves durable verification audit records for all steps in the workflow run.",
)
def get_workflow_verifications(
    workflow_id: str,
    db: Session = Depends(get_db),
) -> list[WorkflowVerificationResponse]:
    service = WorkflowService(db)
    return service.get_workflow_verifications(workflow_id)


@router.get(
    "/{workflow_id}/steps/{step_key}/verification",
    response_model=WorkflowVerificationResponse,
    summary="Get step verification record",
    description="Retrieves the durable verification audit record for a specific workflow step.",
)
def get_step_verification(
    workflow_id: str,
    step_key: str,
    db: Session = Depends(get_db),
) -> WorkflowVerificationResponse:
    service = WorkflowService(db)
    return service.get_step_verification(workflow_id, step_key)


@router.post(
    "/{workflow_id}/compensate",
    response_model=WorkflowRunResponse,
    summary="Compensate workflow",
    description="Executes registered compensations for succeeded steps in reverse-dependency order.",
)
async def compensate_workflow(
    workflow_id: str,
    payload: WorkflowCompensateRequest = WorkflowCompensateRequest(),
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    service = WorkflowService(db)
    return await service.compensate_workflow(
        run_id=workflow_id,
        reason=payload.reason,
        caller_permissions=set(payload.caller_permissions),
    )


@router.get(
    "/{workflow_id}/compensations",
    response_model=list[WorkflowCompensationResponse],
    summary="Get workflow compensation records",
    description="Retrieves all compensation records registered or executed for the workflow run.",
)
def get_workflow_compensations(
    workflow_id: str,
    db: Session = Depends(get_db),
) -> list[WorkflowCompensationResponse]:
    service = WorkflowService(db)
    return service.get_workflow_compensations(workflow_id)
