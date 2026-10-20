from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from pico.repositories.working_memory_repo import WorkingMemoryRepository
from pico.services.memory_service import MemoryService


def test_working_memory_session_isolation(db_session: Session) -> None:
    service = MemoryService(db_session)

    # Session 1 puts working state
    w1 = service.put_working_state(
        session_id="session_alpha",
        key="active_goal",
        content="Refactor SQLite migrations",
        data_classification="INTERNAL",
        source_provenance="session:session_alpha",
    )
    assert w1.id.startswith("wmem_")

    # Session 2 puts working state
    w2 = service.put_working_state(
        session_id="session_beta",
        key="active_goal",
        content="Deploy web UI",
        data_classification="PUBLIC",
        source_provenance="session:session_beta",
    )
    assert w2.id.startswith("wmem_")

    # Verify session isolation: session_alpha items do not appear in session_beta
    alpha_items = service.list_working_state("session_alpha")
    beta_items = service.list_working_state("session_beta")

    assert len(alpha_items) == 1
    assert alpha_items[0].content == "Refactor SQLite migrations"
    assert alpha_items[0].session_id == "session_alpha"

    assert len(beta_items) == 1
    assert beta_items[0].content == "Deploy web UI"
    assert beta_items[0].session_id == "session_beta"


def test_working_memory_ttl_and_expiry(db_session: Session) -> None:
    service = MemoryService(db_session)

    # Item with TTL in the past (expired)
    repo = WorkingMemoryRepository(db_session)
    expired_item = repo.put(
        session_id="session_expiry",
        key="temp_state",
        content="This should expire",
        ttl_seconds=-10,  # Expired 10s ago
    )
    assert expired_item.expires_at is not None
    exp_tz = expired_item.expires_at if expired_item.expires_at.tzinfo else expired_item.expires_at.replace(tzinfo=timezone.utc)
    assert exp_tz < datetime.now(timezone.utc)

    # Non-expired item
    repo.put(
        session_id="session_expiry",
        key="valid_state",
        content="This is active",
        ttl_seconds=3600,
    )

    # list_active must filter out the expired item
    active_items = repo.list_active("session_expiry")
    keys = [item.key for item in active_items]
    assert "valid_state" in keys
    assert "temp_state" not in keys


def test_working_memory_bounded_size_policy(db_session: Session) -> None:
    # Set a tiny max_items_per_session limit of 3
    repo = WorkingMemoryRepository(db_session, max_items_per_session=3)

    # Insert 4 items into the same session
    repo.put("session_bounded", "key_1", "content 1")
    repo.put("session_bounded", "key_2", "content 2")
    repo.put("session_bounded", "key_3", "content 3")

    active_before = repo.list_active("session_bounded")
    assert len(active_before) == 3

    # Adding 4th item prunes oldest active item (key_1)
    repo.put("session_bounded", "key_4", "content 4")

    active_after = repo.list_active("session_bounded")
    assert len(active_after) == 3
    keys_after = [it.key for it in active_after]
    assert "key_4" in keys_after
    assert "key_3" in keys_after
    assert "key_2" in keys_after
    assert "key_1" not in keys_after


def test_working_memory_clear_session(db_session: Session) -> None:
    service = MemoryService(db_session)

    service.put_working_state("session_to_clear", "step_1", "first step")
    service.put_working_state("session_to_clear", "step_2", "second step")
    service.put_working_state("other_session", "step_1", "keep this")

    cleared = service.clear_working_memory("session_to_clear")
    assert cleared == 2

    assert len(service.list_working_state("session_to_clear")) == 0
    assert len(service.list_working_state("other_session")) == 1
