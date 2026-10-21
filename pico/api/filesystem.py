"""REST API router for Staged Filesystem Diffs (Milestone P8.2)."""

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.db.session import get_db
from pico.filesystem.staging import (
    discard_staged_diff_proposal,
    get_staged_diff_proposal,
    list_staged_diff_proposals,
    stage_diff_proposal,
)
from pico.repositories.staged_diff_repo import StagedDiffRepository
from pico.schemas.filesystem import StageDiffRequest, StagedDiffResponse
from pico.schemas.tools import ToolExecutionResponse
from pico.services.execution_engine import ToolExecutionEngine

router = APIRouter(prefix="/api/filesystem", tags=["Filesystem"])


@router.post(
    "/proposals",
    response_model=StagedDiffResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Stage a filesystem diff proposal",
)
def stage_proposal(
    payload: StageDiffRequest,
    db: Session = Depends(get_db),
) -> StagedDiffResponse:
    """
    Stage a new text-file change proposal (create or replace).
    Inspects and validates path, bounds, and original content, generates a unified diff,
    and returns the staged proposal. Workspace target is untouched.
    """
    proposal = stage_diff_proposal(
        db=db,
        root_id=payload.root_id,
        relative_path=payload.relative_path,
        operation=payload.operation,
        proposed_content=payload.proposed_content,
    )
    return StagedDiffResponse.model_validate(proposal)


@router.get(
    "/proposals",
    response_model=list[StagedDiffResponse],
    summary="List staged filesystem proposals",
)
def list_proposals(
    root_id: str | None = Query(default=None, description="Filter by allowlisted root ID"),
    status_filter: str | None = Query(default=None, alias="status", description="Filter by status"),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[StagedDiffResponse]:
    proposals = list_staged_diff_proposals(
        db=db,
        root_id=root_id,
        status=status_filter,
        limit=limit,
    )
    return [StagedDiffResponse.model_validate(p) for p in proposals]


@router.get(
    "/proposals/{proposal_id}",
    response_model=StagedDiffResponse,
    summary="Get and inspect a staged diff proposal",
)
def get_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
) -> StagedDiffResponse:
    proposal = get_staged_diff_proposal(db, proposal_id)
    return StagedDiffResponse.model_validate(proposal)


@router.post(
    "/proposals/{proposal_id}/discard",
    response_model=StagedDiffResponse,
    summary="Discard a staged proposal",
)
def discard_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
) -> StagedDiffResponse:
    proposal = discard_staged_diff_proposal(db, proposal_id)
    return StagedDiffResponse.model_validate(proposal)


@router.post(
    "/proposals/{proposal_id}/apply",
    response_model=ToolExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit execution to apply an approved staged diff",
)
def submit_apply(
    proposal_id: str,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """
    Submit a consequential apply execution via ToolExecutionEngine.
    Enforces 'filesystem:write' permission and server-bound cryptographic input digest.
    Links the proposal to the execution record in status 'awaiting_approval'.
    """
    repo = StagedDiffRepository(db)
    proposal = repo.get_by_id(proposal_id)
    if not proposal:
        raise EntityNotFoundError("StagedDiff", proposal_id)
    if proposal.status != "staged":
        raise ValidationError(
            f"Proposal '{proposal_id}' is in status '{proposal.status}'. Only 'staged' proposals can be applied."
        )

    effective_perms = {"filesystem:read"}
    if settings.APPROVAL_SECRET_TOKEN and x_approval_token == settings.APPROVAL_SECRET_TOKEN:
        effective_perms.add("filesystem:write")
    elif not settings.APPROVAL_SECRET_TOKEN:
        effective_perms.add("filesystem:write")

    engine = ToolExecutionEngine(db, registry)
    execution = engine.submit(
        capability_id="filesystem.apply_staged_diff",
        input_data={"proposal_id": proposal_id},
        caller_permissions=effective_perms,
    )

    repo.link_execution(proposal_id, execution.id)
    return ToolExecutionResponse.model_validate(execution)


@router.post(
    "/proposals/{proposal_id}/rollback",
    response_model=ToolExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit execution to roll back an applied diff",
)
def submit_rollback(
    proposal_id: str,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    db: Session = Depends(get_db),
) -> ToolExecutionResponse:
    """
    Submit a consequential rollback execution via ToolExecutionEngine.
    Enforces 'filesystem:write' permission and server-bound cryptographic input digest.
    Links the proposal to the execution record in status 'awaiting_approval'.
    """
    repo = StagedDiffRepository(db)
    proposal = repo.get_by_id(proposal_id)
    if not proposal:
        raise EntityNotFoundError("StagedDiff", proposal_id)
    if proposal.status != "applied":
        raise ValidationError(
            f"Proposal '{proposal_id}' is in status '{proposal.status}'. Only 'applied' proposals can be rolled back."
        )

    effective_perms = {"filesystem:read"}
    if settings.APPROVAL_SECRET_TOKEN and x_approval_token == settings.APPROVAL_SECRET_TOKEN:
        effective_perms.add("filesystem:write")
    elif not settings.APPROVAL_SECRET_TOKEN:
        effective_perms.add("filesystem:write")

    engine = ToolExecutionEngine(db, registry)
    execution = engine.submit(
        capability_id="filesystem.rollback_staged_diff",
        input_data={"proposal_id": proposal_id},
        caller_permissions=effective_perms,
    )

    repo.link_execution(proposal_id, execution.id)
    return ToolExecutionResponse.model_validate(execution)
