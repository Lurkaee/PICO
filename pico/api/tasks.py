from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pico.db.session import get_db
from pico.schemas.task import (
    TaskCreate,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
)
from pico.services.task_service import TaskService

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create task",
)
def create_task(
    payload: TaskCreate,
    db: Session = Depends(get_db),
) -> TaskResponse:
    service = TaskService(db)
    return service.create_task(payload)


@router.get(
    "",
    response_model=TaskListResponse,
    summary="List tasks",
)
def list_tasks(
    project_id: str | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> TaskListResponse:
    service = TaskService(db)
    return service.list_tasks(
        project_id=project_id,
        conversation_id=conversation_id,
        status=status,
        priority=priority,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Get task by ID",
)
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> TaskResponse:
    service = TaskService(db)
    return service.get_task(task_id)


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Update task",
)
def update_task(
    task_id: str,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
) -> TaskResponse:
    service = TaskService(db)
    return service.update_task(task_id=task_id, payload=payload)
