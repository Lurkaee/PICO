from sqlalchemy.orm import Session

from pico.core.errors import ConflictError, EntityNotFoundError
from pico.core.id_gen import generate_id
from pico.repositories.project_repo import ProjectRepository
from pico.schemas.project import (
    ProjectCreate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
)


class ProjectService:
    def __init__(self, db: Session) -> None:
        self.project_repo = ProjectRepository(db)

    def create_project(self, payload: ProjectCreate) -> ProjectResponse:
        existing = self.project_repo.get_by_name(payload.name)
        if existing:
            raise ConflictError(f"Project with name '{payload.name}' already exists.")

        project_id = generate_id("proj")
        project = self.project_repo.create(
            project_id=project_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            metadata_json=payload.metadata,
        )

        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description,
            status=project.status,  # type: ignore[arg-type]
            created_at=project.created_at,
            updated_at=project.updated_at,
            metadata=project.metadata_json,
        )

    def get_project(self, project_id: str) -> ProjectResponse:
        project = self.project_repo.get_by_id(project_id)
        if not project:
            raise EntityNotFoundError("Project", project_id)

        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description,
            status=project.status,  # type: ignore[arg-type]
            created_at=project.created_at,
            updated_at=project.updated_at,
            metadata=project.metadata_json,
        )

    def list_projects(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ProjectListResponse:
        items = self.project_repo.list(status=status, limit=limit, offset=offset)
        total = self.project_repo.count(status=status)

        return ProjectListResponse(
            items=[
                ProjectResponse(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    status=item.status,  # type: ignore[arg-type]
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

    def update_project(self, project_id: str, payload: ProjectUpdate) -> ProjectResponse:
        project = self.project_repo.get_by_id(project_id)
        if not project:
            raise EntityNotFoundError("Project", project_id)

        if payload.name is not None and payload.name != project.name:
            existing = self.project_repo.get_by_name(payload.name)
            if existing:
                raise ConflictError(f"Project with name '{payload.name}' already exists.")

        updated = self.project_repo.update(
            project=project,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            metadata_json=payload.metadata,
        )

        return ProjectResponse(
            id=updated.id,
            name=updated.name,
            description=updated.description,
            status=updated.status,  # type: ignore[arg-type]
            created_at=updated.created_at,
            updated_at=updated.updated_at,
            metadata=updated.metadata_json,
        )
