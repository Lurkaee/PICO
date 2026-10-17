from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.project import Project
from pico.repositories.base import BaseRepository


class ProjectRepository(BaseRepository[Project]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, project_id: str) -> Project | None:
        stmt = select(Project).where(Project.id == project_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_name(self, name: str) -> Project | None:
        stmt = select(Project).where(Project.name == name)
        return self.db.execute(stmt).scalar_one_or_none()

    def list(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Project]:
        stmt = select(Project)
        if status is not None:
            stmt = stmt.where(Project.status == status)
        stmt = stmt.order_by(Project.updated_at.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def count(self, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(Project)
        if status is not None:
            stmt = stmt.where(Project.status == status)
        return self.db.execute(stmt).scalar_one()

    def create(
        self,
        project_id: str,
        name: str,
        description: str | None = None,
        status: str = "active",
        metadata_json: dict[str, Any] | None = None,
    ) -> Project:
        project = Project(
            id=project_id,
            name=name,
            description=description,
            status=status,
            metadata_json=metadata_json,
        )
        self.db.add(project)
        self.db.commit()
        self.db.refresh(project)
        return project

    def update(
        self,
        project: Project,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> Project:
        if name is not None:
            project.name = name
        if description is not None:
            project.description = description
        if status is not None:
            project.status = status
        if metadata_json is not None:
            project.metadata_json = metadata_json
        self.db.commit()
        self.db.refresh(project)
        return project
