import pytest
from sqlalchemy.orm import Session

from pico.core.errors import AuthorizationError, ValidationError
from pico.services.memory_service import MemoryService


def test_procedural_memory_lifecycle_requires_human_approval(db_session: Session) -> None:
    service = MemoryService(db_session)

    # 1. New procedures start strictly in 'draft' status
    steps = [
        {"order": 1, "action": "git status", "expected": "clean working tree"},
        {"order": 2, "action": "alembic upgrade head", "expected": "schema up to date"},
    ]
    proc = service.create_procedure(
        name="database_migration_procedure",
        description="Safe procedure for applying SQLite migrations.",
        steps_json=steps,
        required_capabilities_json=["git:read", "sqlite:ddl"],
        data_classification="INTERNAL",
    )

    assert proc.id.startswith("prc_")
    assert proc.lifecycle_status == "draft"
    assert proc.approved_by is None
    assert proc.approved_at is None

    # 2. Cannot jump directly from draft to active without approval
    with pytest.raises(ValidationError):
        service.activate_procedure(proc.id)

    # 3. Transition to reviewed
    proc_reviewed = service.review_procedure(proc.id, notes="Checked steps and capabilities.")
    assert proc_reviewed.lifecycle_status == "reviewed"

    # 4. Attempting approval without human approver or untrusted identity must fail
    with pytest.raises(AuthorizationError):
        service.approve_procedure(proc.id, approver="")

    with pytest.raises(AuthorizationError):
        service.approve_procedure(proc.id, approver="untrusted_caller_or_model")

    # 5. Explicit human approval succeeds
    proc_approved = service.approve_procedure(
        proc.id, approver="alice_engineer", notes="Approved following P4 architecture review."
    )
    assert proc_approved.lifecycle_status == "approved"
    assert proc_approved.approved_by == "alice_engineer"
    assert proc_approved.approved_at is not None

    # 6. Now procedure can become active
    proc_active = service.activate_procedure(proc.id)
    assert proc_active.lifecycle_status == "active"

    # 7. Check active list
    active_procs = service.list_procedures(lifecycle_status="active")
    assert len(active_procs) == 1
    assert active_procs[0].id == proc.id

    # 8. Retirement
    proc_retired = service.retire_procedure(proc.id)
    assert proc_retired.lifecycle_status == "retired"
    assert proc_retired.retired_at is not None

    # Retired procedures are no longer in active list
    assert len(service.list_procedures(lifecycle_status="active")) == 0
