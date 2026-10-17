from datetime import datetime, timezone
from sqlalchemy.orm import Session

from pico.core.errors import EntityNotFoundError
from pico.core.id_gen import generate_id
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.project_repo import ProjectRepository
from pico.repositories.task_repo import TaskRepository
from pico.schemas.task import (
    TaskCreate,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
)


class TaskService:
    def __init__(self, db: Session) -> None:
        self.task_repo = TaskRepository(db)
        self.project_repo = ProjectRepository(db)
        self.conversation_repo = ConversationRepository(db)

    def create_task(self, payload: TaskCreate) -> TaskResponse:
        # Validate foreign references
        if payload.project_id:
            project = self.project_repo.get_by_id(payload.project_id)
            if not project:
                raise EntityNotFoundError("Project", payload.project_id)

        if payload.conversation_id:
            conversation = self.conversation_repo.get_by_id(payload.conversation_id)
            if not conversation:
                raise EntityNotFoundError("Conversation", payload.conversation_id)

        task_id = generate_id("task")
        completed_at = (
            datetime.now(timezone.utc) if payload.status == "completed" else None
        )

        task = self.task_repo.create(
            task_id=task_id,
            title=payload.title,
            description=payload.description,
            project_id=payload.project_id,
            conversation_id=payload.conversation_id,
            status=payload.status,
            priority=payload.priority,
            due_date=payload.due_date,
            completed_at=completed_at,
            metadata_json=payload.metadata,
        )

        return TaskResponse(
            id=task.id,
            project_id=task.project_id,
            conversation_id=task.conversation_id,
            title=task.title,
            description=task.description,
            status=task.status,  # type: ignore[arg-type]
            priority=task.priority,  # type: ignore[arg-type]
            due_date=task.due_date,
            completed_at=task.completed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            metadata=task.metadata_json,
        )

    def get_task(self, task_id: str) -> TaskResponse:
        task = self.task_repo.get_by_id(task_id)
        if not task:
            raise EntityNotFoundError("Task", task_id)

        return TaskResponse(
            id=task.id,
            project_id=task.project_id,
            conversation_id=task.conversation_id,
            title=task.title,
            description=task.description,
            status=task.status,  # type: ignore[arg-type]
            priority=task.priority,  # type: ignore[arg-type]
            due_date=task.due_date,
            completed_at=task.completed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            metadata=task.metadata_json,
        )

    def list_tasks(
        self,
        project_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> TaskListResponse:
        items = self.task_repo.list(
            project_id=project_id,
            conversation_id=conversation_id,
            status=status,
            priority=priority,
            limit=limit,
            offset=offset,
        )
        total = self.task_repo.count(
            project_id=project_id,
            conversation_id=conversation_id,
            status=status,
            priority=priority,
        )

        return TaskListResponse(
            items=[
                TaskResponse(
                    id=item.id,
                    project_id=item.project_id,
                    conversation_id=item.conversation_id,
                    title=item.title,
                    description=item.description,
                    status=item.status,  # type: ignore[arg-type]
                    priority=item.priority,  # type: ignore[arg-type]
                    due_date=item.due_date,
                    completed_at=item.completed_at,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    metadata=item.metadata_json,
                )
                for item in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    def update_task(self, task_id: str, payload: TaskUpdate) -> TaskResponse:
        task = self.task_repo.get_by_id(task_id)
        if not task:
            raise EntityNotFoundError("Task", task_id)

        # Validate references if updated
        if payload.project_id is not None and payload.project_id != "":
            project = self.project_repo.get_by_id(payload.project_id)
            if not project:
                raise EntityNotFoundError("Project", payload.project_id)

        if payload.conversation_id is not None and payload.conversation_id != "":
            conversation = self.conversation_repo.get_by_id(payload.conversation_id)
            if not conversation:
                raise EntityNotFoundError("Conversation", payload.conversation_id)

        # Invariant: auto-timestamp task completion
        update_completed_at = False
        new_completed_at = task.completed_at
        if payload.status is not None:
            if payload.status == "completed" and task.status != "completed":
                update_completed_at = True
                new_completed_at = datetime.now(timezone.utc)
            elif payload.status != "completed" and task.status == "completed":
                update_completed_at = True
                new_completed_at = None

        updated = self.task_repo.update(
            task=task,
            title=payload.title,
            description=payload.description,
            project_id=payload.project_id,
            conversation_id=payload.conversation_id,
            status=payload.status,
            priority=payload.priority,
            due_date=payload.due_date,
            completed_at=new_completed_at,
            metadata_json=payload.metadata,
            update_completed_at=update_completed_at,
        )

        return TaskResponse(
            id=updated.id,
            project_id=updated.project_id,
            conversation_id=updated.conversation_id,
            title=updated.title,
            description=updated.description,
            status=updated.status,  # type: ignore[arg-type]
            priority=updated.priority,  # type: ignore[arg-type]
            due_date=updated.due_date,
            completed_at=updated.completed_at,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
            metadata=updated.metadata_json,
        )
