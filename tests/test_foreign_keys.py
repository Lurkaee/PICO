import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pico.core.id_gen import generate_id
from pico.models.message import Message
from pico.models.task import Task


def test_sqlite_foreign_key_enforcement_for_orphaned_message(db_session: Session) -> None:
    """Verifies that PRAGMA foreign_keys = ON prevents inserting a message with nonexistent conversation."""
    bad_msg = Message(
        id=generate_id("msg"),
        conversation_id="conv_does_not_exist_xyz",
        role="user",
        content="This should fail foreign key check.",
    )
    db_session.add(bad_msg)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_sqlite_foreign_key_enforcement_for_invalid_task_project(db_session: Session) -> None:
    """Verifies that PRAGMA foreign_keys = ON prevents inserting a task with nonexistent project."""
    bad_task = Task(
        id=generate_id("task"),
        project_id="proj_does_not_exist_xyz",
        title="Orphaned Task",
    )
    db_session.add(bad_task)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
