from datetime import datetime
from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, NotFoundError
from pico.db.session import get_db
from pico.schemas.memory import (
    EpisodicMemoryCreate,
    EpisodicMemoryListResponse,
    EpisodicMemoryResponse,
    MemoryStatusResponse,
    ProceduralMemoryApprovalRequest,
    ProceduralMemoryCreate,
    ProceduralMemoryListResponse,
    ProceduralMemoryResponse,
    SemanticMemoryCreate,
    SemanticMemoryListResponse,
    SemanticMemoryResponse,
    WorkingMemoryCreate,
    WorkingMemoryResponse,
)
from pico.services.memory_service import MemoryService

router = APIRouter(prefix="/api/memory", tags=["Memory"])


# --- System Status ---
@router.get(
    "/status",
    response_model=MemoryStatusResponse,
    summary="Get memory subsystem metrics",
)
def get_memory_status(db: Session = Depends(get_db)) -> MemoryStatusResponse:
    service = MemoryService(db)
    return service.get_memory_status()


# --- Working Memory ---
@router.post(
    "/working",
    response_model=WorkingMemoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save or update working memory item",
)
def put_working_state(
    payload: WorkingMemoryCreate,
    db: Session = Depends(get_db),
) -> WorkingMemoryResponse:
    service = MemoryService(db)
    item = service.put_working_state(
        session_id=payload.session_id,
        key=payload.key,
        content=payload.content,
        item_type=payload.item_type,
        task_id=payload.task_id,
        project_id=payload.project_id,
        data_classification=payload.data_classification,
        source_provenance=payload.source_provenance,
        ttl_seconds=payload.ttl_seconds,
        metadata_json=payload.metadata,
    )
    return WorkingMemoryResponse.model_validate(item)


@router.get(
    "/working/{session_id}",
    response_model=list[WorkingMemoryResponse],
    summary="List active working memory for session",
)
def list_working_memory(
    session_id: str,
    task_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[WorkingMemoryResponse]:
    service = MemoryService(db)
    items = service.list_working_state(
        session_id=session_id,
        task_id=task_id,
        project_id=project_id,
        limit=limit,
    )
    return [WorkingMemoryResponse.model_validate(it) for it in items]


@router.delete(
    "/working/{session_id}",
    status_code=status.HTTP_200_OK,
    summary="Clear active working memory for session",
)
def clear_working_memory(
    session_id: str,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    service = MemoryService(db)
    cleared_count = service.clear_working_memory(session_id)
    return {"cleared_count": cleared_count}


# --- Episodic Memory ---
@router.post(
    "/events",
    response_model=EpisodicMemoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record an episodic event",
)
def record_event(
    payload: EpisodicMemoryCreate,
    db: Session = Depends(get_db),
) -> EpisodicMemoryResponse:
    service = MemoryService(db)
    item = service.record_event(
        event_type=payload.event_type,
        description=payload.description,
        source_provenance=payload.source_provenance,
        occurred_at=payload.occurred_at,
        task_id=payload.task_id,
        project_id=payload.project_id,
        session_id=payload.session_id,
        data_classification=payload.data_classification,
        is_inferred=payload.is_inferred,
        deduplication_key=payload.deduplication_key,
        metadata_json=payload.metadata,
    )
    return EpisodicMemoryResponse.model_validate(item)


@router.get(
    "/events",
    response_model=EpisodicMemoryListResponse,
    summary="List and filter episodic events",
)
def list_events(
    event_type: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    occurred_after: datetime | None = Query(default=None),
    occurred_before: datetime | None = Query(default=None),
    is_inferred: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> EpisodicMemoryListResponse:
    service = MemoryService(db)
    items = service.list_events(
        event_type=event_type,
        task_id=task_id,
        project_id=project_id,
        session_id=session_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        is_inferred=is_inferred,
        limit=limit,
        offset=offset,
    )
    total = service.count_events(
        event_type=event_type,
        task_id=task_id,
        project_id=project_id,
    )
    return EpisodicMemoryListResponse(
        items=[EpisodicMemoryResponse.model_validate(it) for it in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- Semantic Memory ---
@router.post(
    "/facts",
    response_model=SemanticMemoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save or remember a factual memory",
)
def save_fact(
    payload: SemanticMemoryCreate,
    db: Session = Depends(get_db),
) -> SemanticMemoryResponse:
    service = MemoryService(db)
    item, _ = service.save_fact(
        subject=payload.subject,
        predicate=payload.predicate,
        content=payload.content,
        object=payload.object,
        entity_type=payload.entity_type,
        confidence=payload.confidence,
        is_verified=payload.is_verified,
        data_classification=payload.data_classification,
        source_provenance=payload.source_provenance,
        project_id=payload.project_id,
        task_id=payload.task_id,
        metadata_json=payload.metadata,
    )
    return SemanticMemoryResponse.model_validate(item)


@router.get(
    "/facts/search",
    response_model=list[SemanticMemoryResponse],
    summary="Full-text search semantic memories (FTS5)",
)
def search_facts(
    q: str = Query(min_length=1, max_length=255, description="Search term or phrase"),
    entity_type: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[SemanticMemoryResponse]:
    service = MemoryService(db)
    items = service.search_facts(
        query=q,
        limit=limit,
        entity_type=entity_type,
        project_id=project_id,
    )
    return [SemanticMemoryResponse.model_validate(it) for it in items]


@router.get(
    "/facts",
    response_model=SemanticMemoryListResponse,
    summary="List stored semantic memories",
)
def list_facts(
    entity_type: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    conflict_status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> SemanticMemoryListResponse:
    service = MemoryService(db)
    items = service.list_facts(
        entity_type=entity_type,
        project_id=project_id,
        task_id=task_id,
        conflict_status=conflict_status,
        limit=limit,
        offset=offset,
    )
    total = service.count_facts(verified_only=False)
    return SemanticMemoryListResponse(
        items=[SemanticMemoryResponse.model_validate(it) for it in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/facts/{fact_id}",
    status_code=status.HTTP_200_OK,
    summary="User-directed forget: delete a factual memory item",
)
def forget_fact(
    fact_id: str,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    service = MemoryService(db)
    success = service.forget_fact(fact_id)
    if not success:
        raise EntityNotFoundError("SemanticMemory", fact_id)
    return {"status": "forgotten", "id": fact_id}


# --- Procedural Memory ---
@router.post(
    "/procedures",
    response_model=ProceduralMemoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a procedural skill draft",
)
def create_procedure(
    payload: ProceduralMemoryCreate,
    db: Session = Depends(get_db),
) -> ProceduralMemoryResponse:
    service = MemoryService(db)
    item = service.create_procedure(
        name=payload.name,
        description=payload.description,
        steps_json=payload.steps,
        version=payload.version,
        preconditions_json=payload.preconditions,
        required_capabilities_json=payload.required_capabilities,
        expected_effects_json=payload.expected_effects,
        verification_criteria_json=payload.verification_criteria,
        data_classification=payload.data_classification,
        source_provenance=payload.source_provenance,
        metadata_json=payload.metadata,
    )
    return ProceduralMemoryResponse.model_validate(item)


@router.get(
    "/procedures",
    response_model=ProceduralMemoryListResponse,
    summary="List procedural memories",
)
def list_procedures(
    lifecycle_status: str | None = Query(default=None),
    name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ProceduralMemoryListResponse:
    service = MemoryService(db)
    items = service.list_procedures(
        lifecycle_status=lifecycle_status,
        name=name,
        limit=limit,
        offset=offset,
    )
    total = service.count_procedures(active_only=False)
    return ProceduralMemoryListResponse(
        items=[ProceduralMemoryResponse.model_validate(it) for it in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/procedures/{procedure_id}/review",
    response_model=ProceduralMemoryResponse,
    summary="Review procedural draft (draft -> reviewed)",
)
def review_procedure(
    procedure_id: str,
    notes: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ProceduralMemoryResponse:
    service = MemoryService(db)
    item = service.review_procedure(procedure_id=procedure_id, notes=notes)
    return ProceduralMemoryResponse.model_validate(item)


@router.post(
    "/procedures/{procedure_id}/approve",
    response_model=ProceduralMemoryResponse,
    summary="Explicit human approval for procedural skill (reviewed -> approved)",
)
def approve_procedure(
    procedure_id: str,
    payload: ProceduralMemoryApprovalRequest,
    x_approval_token: str | None = Header(default=None, alias="X-Pico-Approval-Token"),
    db: Session = Depends(get_db),
) -> ProceduralMemoryResponse:
    if settings.APPROVAL_SECRET_TOKEN and x_approval_token != settings.APPROVAL_SECRET_TOKEN:
        raise AuthorizationError("Invalid or missing approval authorization token.")
    service = MemoryService(db)
    item = service.approve_procedure(
        procedure_id=procedure_id,
        approver=payload.approver,
        notes=payload.notes,
    )
    return ProceduralMemoryResponse.model_validate(item)


@router.post(
    "/procedures/{procedure_id}/activate",
    response_model=ProceduralMemoryResponse,
    summary="Activate approved procedural skill (approved -> active)",
)
def activate_procedure(
    procedure_id: str,
    db: Session = Depends(get_db),
) -> ProceduralMemoryResponse:
    service = MemoryService(db)
    item = service.activate_procedure(procedure_id=procedure_id)
    return ProceduralMemoryResponse.model_validate(item)


@router.post(
    "/procedures/{procedure_id}/retire",
    response_model=ProceduralMemoryResponse,
    summary="Retire a procedural skill (* -> retired)",
)
def retire_procedure(
    procedure_id: str,
    db: Session = Depends(get_db),
) -> ProceduralMemoryResponse:
    service = MemoryService(db)
    item = service.retire_procedure(procedure_id=procedure_id)
    return ProceduralMemoryResponse.model_validate(item)
