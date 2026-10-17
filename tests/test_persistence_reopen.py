import os
import tempfile
from datetime import datetime, timezone
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from pico.core.id_gen import generate_id
from pico.db.base import Base
from pico.models.conversation import Conversation
from pico.models.message import Message
from pico.models.project import Project
from pico.models.task import Task


def configure_sqlite_engine(db_path: str):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.close()

    return engine


def test_records_survive_engine_disposal_and_reconnection():
    # Step 1: Create isolated temporary database file
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        # Step 2: Initialize engine 1 and create tables
        engine1 = configure_sqlite_engine(db_path)
        Base.metadata.create_all(bind=engine1)
        Session1 = sessionmaker(bind=engine1, autocommit=False, autoflush=False)

        session1 = Session1()
        proj_id = generate_id("proj")
        conv_id = generate_id("conv")
        msg_id = generate_id("msg")
        task_id = generate_id("task")

        project = Project(
            id=proj_id,
            name="Durable Test Project",
            description="Testing survival across process restarts",
            status="active",
        )
        session1.add(project)

        conversation = Conversation(
            id=conv_id,
            title="Durable Thread",
        )
        session1.add(conversation)

        message = Message(
            id=msg_id,
            conversation_id=conv_id,
            role="user",
            content="Critical persistable instructions.",
            data_classification="SECRET",
        )
        session1.add(message)

        task = Task(
            id=task_id,
            project_id=proj_id,
            conversation_id=conv_id,
            title="Survive shutdown",
            status="pending",
            priority="urgent",
        )
        session1.add(task)

        session1.commit()
        session1.close()

        # Step 3: Complete shutdown and disposal of engine 1
        engine1.dispose()

        # Step 4: Reconnect via entirely new engine 2
        engine2 = configure_sqlite_engine(db_path)
        with engine2.connect() as conn:
            wal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            fk_mode = conn.execute(text("PRAGMA foreign_keys")).scalar()
            assert str(wal_mode).lower() == "wal"
            assert fk_mode == 1

        Session2 = sessionmaker(bind=engine2, autocommit=False, autoflush=False)
        session2 = Session2()

        # Step 5: Verify all records exist with exact relationships
        fetched_proj = session2.query(Project).filter(Project.id == proj_id).first()
        assert fetched_proj is not None
        assert fetched_proj.name == "Durable Test Project"

        fetched_conv = session2.query(Conversation).filter(Conversation.id == conv_id).first()
        assert fetched_conv is not None
        assert fetched_conv.title == "Durable Thread"
        assert len(fetched_conv.messages) == 1
        assert fetched_conv.messages[0].id == msg_id
        assert fetched_conv.messages[0].data_classification == "SECRET"

        fetched_task = session2.query(Task).filter(Task.id == task_id).first()
        assert fetched_task is not None
        assert fetched_task.project_id == proj_id
        assert fetched_task.conversation_id == conv_id
        assert fetched_task.status == "pending"
        assert fetched_task.completed_at is None

        # Step 6: Mutate in session 2 (complete task) and close
        fetched_task.status = "completed"
        fetched_task.completed_at = datetime.now(timezone.utc)
        session2.commit()
        session2.close()
        engine2.dispose()

        # Step 7: Reconnect via engine 3 to verify mutation survived
        engine3 = configure_sqlite_engine(db_path)
        Session3 = sessionmaker(bind=engine3, autocommit=False, autoflush=False)
        session3 = Session3()

        verified_task = session3.query(Task).filter(Task.id == task_id).first()
        assert verified_task is not None
        assert verified_task.status == "completed"
        assert verified_task.completed_at is not None

        session3.close()
        engine3.dispose()

    finally:
        # Cleanup
        for ext in ["", "-wal", "-shm"]:
            p = db_path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
