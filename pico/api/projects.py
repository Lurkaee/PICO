from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pico.db.session import get_db
from pico.schemas.project import (
    ProjectCreate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
)
from pico.services.project_service import ProjectService

router = APIRouter(prefix="/api/projects", tags=["Projects"])


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create project",
)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
) -> ProjectResponse:
    service = ProjectService(db)
    return service.create_project(payload)


@router.get(
    "",
    response_model=ProjectListResponse,
    summary="List projects",
)
def list_projects(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    service = ProjectService(db)
    return service.list_projects(status=status, limit=limit, offset=offset)


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Get project by ID",
)
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
) -> ProjectResponse:
    service = ProjectService(db)
    return service.get_project(project_id)


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Update project",
)
def update_project(
    project_id: str,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
) -> ProjectResponse:
    service = ProjectService(db)
    return service.update_project(project_id=project_id, payload=payload)
