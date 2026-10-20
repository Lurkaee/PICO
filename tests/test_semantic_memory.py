import os
import tempfile
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session

from pico.db.base import Base
from pico.services.memory_service import MemoryService


def test_semantic_memory_save_and_fts5_search(db_session: Session) -> None:
    service = MemoryService(db_session)

    # Save facts
    f1, conflict1 = service.save_fact(
        subject="User",
        predicate="preferred_editor",
        content="User prefers Neovim with Lua configuration for Python coding.",
        entity_type="user_preference",
        confidence=1.0,
        is_verified=True,
        data_classification="INTERNAL",
        source_provenance="user:stated_preference",
    )
    assert f1.id.startswith("sem_")
    assert conflict1 is False
    assert f1.is_verified is True

    f2, conflict2 = service.save_fact(
        subject="PICO",
        predicate="database_engine",
        content="PICO uses SQLite in Write-Ahead Logging (WAL) mode with immediate foreign keys.",
        entity_type="system_fact",
        confidence=1.0,
        is_verified=True,
        data_classification="INTERNAL",
        source_provenance="doc:architecture.md",
    )
    assert conflict2 is False

    # Search using FTS5 keywords
    res_editor = service.search_facts("Neovim")
    assert len(res_editor) >= 1
    assert res_editor[0].subject == "User"
    assert "Neovim" in res_editor[0].content

    res_wal = service.search_facts("WAL")
    assert len(res_wal) >= 1
    assert res_wal[0].subject == "PICO"
    assert "WAL" in res_wal[0].content


def test_semantic_memory_conflict_handling_no_silent_overwrite(db_session: Session) -> None:
    service = MemoryService(db_session)

    # Stated initial verified preference
    f1, _ = service.save_fact(
        subject="User",
        predicate="primary_language",
        content="Python is the primary backend language for PICO.",
        confidence=1.0,
        is_verified=True,
    )

    # An unverified model inference claims Rust is the primary language
    f2, is_conflict = service.save_fact(
        subject="User",
        predicate="primary_language",
        content="Rust is the primary backend language for PICO.",
        confidence=0.6,
        is_verified=False,
    )

    # Invariant: Must NOT silently overwrite the verified fact!
    assert is_conflict is True
    assert f2.conflict_status == "conflicted"

    # Both facts are retained; verified fact remains clean and active
    retrieved = service.list_facts()
    assert len(retrieved) == 2
    assert any(f.is_verified and f.content.startswith("Python") for f in retrieved)
    assert any(f.conflict_status == "conflicted" and f.content.startswith("Rust") for f in retrieved)


def test_semantic_memory_user_confirmed_superseding(db_session: Session) -> None:
    service = MemoryService(db_session)

    # Initial preference
    f1, _ = service.save_fact(
        subject="User",
        predicate="theme",
        content="User prefers light theme.",
        is_verified=True,
    )
    assert f1.conflict_status == "clean"

    # User explicitly updates preference (is_verified=True)
    f2, _ = service.save_fact(
        subject="User",
        predicate="theme",
        content="User switched to dark theme.",
        is_verified=True,
    )
    assert f2.conflict_status == "clean"

    # Old fact is now superseded, new fact is active
    db_session.refresh(f1)
    assert f1.conflict_status == "superseded"


def test_semantic_memory_user_directed_forget(db_session: Session) -> None:
    service = MemoryService(db_session)

    fact, _ = service.save_fact(
        subject="User",
        predicate="home_city",
        content="User lives in San Francisco.",
        data_classification="SENSITIVE",
    )

    # Verify searchable
    search_before = service.search_facts("Francisco")
    assert len(search_before) == 1

    # User explicitly forgets this fact
    forgotten = service.forget_fact(fact.id)
    assert forgotten is True

    # Fact is purged from database and FTS
    search_after = service.search_facts("Francisco")
    assert len(search_after) == 0
    assert service.semantic_repo.get_by_id(fact.id) is None


def test_semantic_memory_persistence_across_reopen() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        # First session: create schema, FTS table, and save fact
        engine1 = create_engine(f"sqlite:///{db_path}")

        @event.listens_for(engine1, "connect")
        def set_pragmas(dbapi_conn, _):
            c = dbapi_conn.cursor()
            c.execute("PRAGMA foreign_keys = ON;")
            c.execute("PRAGMA journal_mode = WAL;")
            c.close()

        Base.metadata.create_all(bind=engine1)
        with engine1.connect() as conn:
            conn.execute(text("""
                CREATE VIRTUAL TABLE IF NOT EXISTS semantic_memories_fts USING fts5(
                    memory_id UNINDEXED,
                    subject,
                    predicate,
                    object,
                    content
                );
            """))
            conn.commit()

        Session1 = sessionmaker(bind=engine1)
        session1 = Session1()

        svc1 = MemoryService(session1)
        saved_fact, _ = svc1.save_fact(
            subject="Project",
            predicate="milestone_p4",
            content="Milestone P4 implements persistent four-part memory architecture.",
            confidence=1.0,
            is_verified=True,
        )
        fact_id = saved_fact.id
        session1.close()
        engine1.dispose()

        # Second session: reopen fresh engine from disk file and verify data persists
        engine2 = create_engine(f"sqlite:///{db_path}")
        Session2 = sessionmaker(bind=engine2)
        session2 = Session2()

        svc2 = MemoryService(session2)
        fact_reopened = svc2.semantic_repo.get_by_id(fact_id)
        assert fact_reopened is not None
        assert fact_reopened.content == "Milestone P4 implements persistent four-part memory architecture."

        # Verify FTS search persists across reopen
        fts_res = svc2.search_facts("four-part")
        assert len(fts_res) == 1
        assert fts_res[0].id == fact_id

        session2.close()
        engine2.dispose()

    finally:
        for ext in ["", "-wal", "-shm"]:
            p = db_path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
