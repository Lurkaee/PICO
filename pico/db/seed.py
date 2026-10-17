from pico.core.id_gen import generate_id
from pico.db.session import SessionLocal
from pico.models.conversation import Conversation
from pico.models.message import Message
from pico.models.project import Project
from pico.models.task import Task


def seed_database() -> dict[str, str]:
    """Seed minimal deterministic development data if database is empty."""
    db = SessionLocal()
    try:
        # Check if project already exists
        existing_proj = db.query(Project).filter(Project.name == "PICO Core Platform").first()
        if existing_proj:
            return {
                "project_id": existing_proj.id,
                "status": "already_seeded",
            }

        proj_id = generate_id("proj")
        project = Project(
            id=proj_id,
            name="PICO Core Platform",
            description="Foundational personal intelligence architecture and local services.",
            status="active",
            metadata_json={"seed": True},
        )
        db.add(project)

        conv_id = generate_id("conv")
        conversation = Conversation(
            id=conv_id,
            title="System Onboarding & Architecture Review",
            metadata_json={"seed": True},
        )
        db.add(conversation)

        msg_id = generate_id("msg")
        message = Message(
            id=msg_id,
            conversation_id=conv_id,
            role="system",
            content="PICO local-first storage initialized with SQLite WAL mode and foreign keys enabled.",
            data_classification="INTERNAL",
            token_count=18,
            metadata_json={"seed": True},
        )
        db.add(message)

        task_id = generate_id("task")
        task = Task(
            id=task_id,
            project_id=proj_id,
            conversation_id=conv_id,
            title="Review Phase 0 Foundation Artifacts",
            description="Examine AGENTS.md, architecture.md, data-model.md, and api-contract.md.",
            status="pending",
            priority="high",
            metadata_json={"seed": True},
        )
        db.add(task)

        db.commit()
        return {
            "project_id": proj_id,
            "conversation_id": conv_id,
            "message_id": msg_id,
            "task_id": task_id,
            "status": "seeded",
        }
    finally:
        db.close()


if __name__ == "__main__":
    result = seed_database()
    print("Seed result:", result)
