from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from pico.models.task import Task
from pico.services.memory_service import MemoryService


def test_episodic_memory_recording_and_provenance(db_session: Session) -> None:
    service = MemoryService(db_session)

    # Create real task to satisfy foreign key constraint
    task = Task(id="task_compiler_01", title="Build Context Compiler")
    db_session.add(task)
    db_session.commit()

    now = datetime.now(timezone.utc)
    event = service.record_event(
        event_type="task_completed",
        description="Task 'Build Context Compiler' completed with 109 passing tests.",
        source_provenance="task:task_compiler_01",
        occurred_at=now,
        task_id="task_compiler_01",
        data_classification="INTERNAL",
        is_inferred=False,
        metadata_json={"tests_passed": 109},
    )

    assert event.id.startswith("epm_")
    assert event.event_type == "task_completed"
    assert event.data_classification == "INTERNAL"
    assert event.source_provenance == "task:task_compiler_01"
    assert event.is_inferred is False
    assert event.metadata_json == {"tests_passed": 109}


def test_episodic_memory_deduplication(db_session: Session) -> None:
    service = MemoryService(db_session)

    dedup_key = "task_failure:task_123:timeout"

    e1 = service.record_event(
        event_type="workflow_error",
        description="Ollama request timed out after 60s",
        source_provenance="provider:ollama",
        deduplication_key=dedup_key,
    )

    # Record the exact same event with duplicate key
    e2 = service.record_event(
        event_type="workflow_error",
        description="Ollama request timed out after 60s (retry)",
        source_provenance="provider:ollama",
        deduplication_key=dedup_key,
    )

    # Must update existing event instead of creating a second row
    assert e1.id == e2.id
    assert e2.description == "Ollama request timed out after 60s (retry)"

    events = service.list_events(event_type="workflow_error")
    assert len(events) == 1


def test_episodic_memory_filters(db_session: Session) -> None:
    service = MemoryService(db_session)

    # Create real tasks to satisfy foreign key constraint
    t_1 = Task(id="task_1", title="Task 1")
    t_2 = Task(id="task_2", title="Task 2")
    db_session.add_all([t_1, t_2])
    db_session.commit()

    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    t1 = datetime.now(timezone.utc) - timedelta(hours=1)
    t2 = datetime.now(timezone.utc)

    service.record_event("task_completed", "Event 1", "task:1", occurred_at=t0, task_id="task_1")
    service.record_event("decision_recorded", "Event 2", "user:explicit", occurred_at=t1, task_id="task_2")
    service.record_event("task_failed", "Event 3", "task:1", occurred_at=t2, task_id="task_1", is_inferred=True)

    # Filter by task_id
    task_1_events = service.list_events(task_id="task_1")
    assert len(task_1_events) == 2

    # Filter by event_type
    decisions = service.list_events(event_type="decision_recorded")
    assert len(decisions) == 1
    assert decisions[0].description == "Event 2"

    # Filter by inferred status
    inferred_events = service.list_events(is_inferred=True)
    assert len(inferred_events) == 1
    assert inferred_events[0].description == "Event 3"

    # Filter by date range
    recent_events = service.list_events(occurred_after=t0 + timedelta(minutes=30))
    assert len(recent_events) == 2
