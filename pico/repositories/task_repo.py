from datetime import datetime
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.task import Task
from pico.repositories.base import BaseRepository


class TaskRepository(BaseRepository[Task]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, task_id: str) -> Task | None:
        stmt = select(Task).where(Task.id == task_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def list(
        self,
        project_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        stmt = select(Task)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if conversation_id is not None:
            stmt = stmt.where(Task.conversation_id == conversation_id)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if priority is not None:
            stmt = stmt.where(Task.priority == priority)

        stmt = stmt.order_by(Task.updated_at.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count(
        self,
        project_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(Task)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if conversation_id is not None:
            stmt = stmt.where(Task.conversation_id == conversation_id)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if priority is not None:
            stmt = stmt.where(Task.priority == priority)
        return self.db.execute(stmt).scalar_one()

    def create(
        self,
        task_id: str,
        title: str,
        description: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        status: str = "pending",
        priority: str = "medium",
        due_date: datetime | None = None,
        completed_at: datetime | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> Task:
        task = Task(
            id=task_id,
            title=title,
            description=description,
            project_id=project_id,
            conversation_id=conversation_id,
            status=status,
            priority=priority,
            due_date=due_date,
            completed_at=completed_at,
            metadata_json=metadata_json,
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def update(
        self,
        task: Task,
        title: str | None = None,
        description: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        due_date: datetime | None = None,
        completed_at: datetime | None = None,
        metadata_json: dict[str, Any] | None = None,
        update_completed_at: bool = False,
    ) -> Task:
        if title is not None:
            task.title = title
        if description is not None:
            task.description = description
        if project_id is not None:
            task.project_id = project_id if project_id != "" else None
        if conversation_id is not None:
            task.conversation_id = conversation_id if conversation_id != "" else None
        if status is not None:
            task.status = status
        if priority is not None:
            task.priority = priority
        if due_date is not None:
            task.due_date = due_date
        if update_completed_at:
            task.completed_at = completed_at
        if metadata_json is not None:
            task.metadata_json = metadata_json

        self.db.commit()
        self.db.refresh(task)
        return task
