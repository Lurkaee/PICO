"""Tests for durable workflow coordination and subtask execution (Milestone P9.1)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pico.capabilities import registry as default_registry
from pico.core.errors import ValidationError
from pico.models.workflow import WorkflowRun, WorkflowStep
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.workflow_repo import WorkflowRepository
from pico.schemas.workflow import StepDefinition, WorkflowCreate
from pico.services.execution_engine import ToolExecutionEngine
from pico.workflow.service import WorkflowService


def test_multi_step_workflow_execution_order_and_data_flow(db_session: Session) -> None:
    """Verify sequential execution of ready steps and dependency-aware output mapping."""
    service = WorkflowService(db_session)

    # Step 1: Idempotent counter increments by 5
    # Step 2: Idempotent counter takes increment from Step 1's output
    payload = WorkflowCreate(
        name="Chained Calculation Workflow",
        data_classification="INTERNAL",
        steps=[
            StepDefinition(
                step_key="counter_1",
                capability_id="test.idempotent_counter",
                capability_version="1.0.0",
                dependencies=[],
                input_template={"increment": 5},
            ),
            StepDefinition(
                step_key="counter_2",
                capability_id="test.idempotent_counter",
                capability_version="1.0.0",
                dependencies=["counter_1"],
                input_template={
                    "increment": {"$from_step": "counter_1", "$path": "incremented_by"}
                },
            ),
        ],
    )

    run_resp = service.submit_workflow(payload)
    assert run_resp.status == "queued"
    assert run_resp.total_steps == 2
    assert run_resp.completed_steps == 0

    # Advance workflow
    advanced = pytest.importorskip("asyncio").run(
        service.advance_workflow(run_resp.id, caller_permissions={"test:execute"})
    )

    assert advanced.status == "succeeded"
    assert advanced.completed_steps == 2
    assert advanced.failed_steps == 0

    # Inspect step outputs
    step1 = next(s for s in advanced.steps if s.step_key == "counter_1")
    step2 = next(s for s in advanced.steps if s.step_key == "counter_2")

    assert step1.status == "succeeded"
    assert step1.output["incremented_by"] == 5

    assert step2.status == "succeeded"
    assert step2.resolved_input == {"increment": 5}
    assert step2.output["incremented_by"] == 5


def test_rejection_of_cyclic_graphs(db_session: Session) -> None:
    """Acyclic dependency check: cycle must be rejected before dispatch."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Cyclic Workflow",
        steps=[
            StepDefinition(
                step_key="step_a",
                capability_id="test.idempotent_counter",
                dependencies=["step_b"],
                input_template={"increment": 1},
            ),
            StepDefinition(
                step_key="step_b",
                capability_id="test.idempotent_counter",
                dependencies=["step_a"],
                input_template={"increment": 1},
            ),
        ],
    )

    with pytest.raises(ValidationError, match="Cyclic dependency detected"):
        service.submit_workflow(payload)


def test_rejection_of_self_dependencies(db_session: Session) -> None:
    """Step depending on itself must be rejected immediately."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Self-dependent Workflow",
        steps=[
            StepDefinition(
                step_key="step_loop",
                capability_id="test.idempotent_counter",
                dependencies=["step_loop"],
                input_template={"increment": 1},
            ),
        ],
    )

    with pytest.raises(ValidationError, match="cannot depend on itself"):
        service.submit_workflow(payload)


def test_rejection_of_missing_dependencies(db_session: Session) -> None:
    """Step depending on a non-existent step must be rejected."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Missing Dep Workflow",
        steps=[
            StepDefinition(
                step_key="step_valid",
                capability_id="test.idempotent_counter",
                dependencies=["nonexistent_step"],
                input_template={"increment": 1},
            ),
        ],
    )

    with pytest.raises(ValidationError, match="depends on non-existent step"):
        service.submit_workflow(payload)


def test_rejection_of_duplicate_step_keys(db_session: Session) -> None:
    """Workflow with duplicate step keys must be rejected."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Duplicate Step Keys",
        steps=[
            StepDefinition(
                step_key="step_same",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": 1},
            ),
            StepDefinition(
                step_key="step_same",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": 2},
            ),
        ],
    )

    with pytest.raises(ValidationError, match="Duplicate step key"):
        service.submit_workflow(payload)


def test_rejection_of_unknown_capability(db_session: Session) -> None:
    """Workflow referencing an unregistered capability must be rejected."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Unknown Capability Workflow",
        steps=[
            StepDefinition(
                step_key="step_unknown",
                capability_id="nonexistent.arbitrary.tool",
                dependencies=[],
                input_template={"foo": "bar"},
            ),
        ],
    )

    with pytest.raises(ValidationError, match="unregistered capability"):
        service.submit_workflow(payload)


def test_rejection_of_invalid_capability_version(db_session: Session) -> None:
    """Workflow referencing an unsupported capability version must be rejected."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Invalid Version Workflow",
        steps=[
            StepDefinition(
                step_key="step_bad_ver",
                capability_id="test.idempotent_counter",
                capability_version="9.9.9",
                dependencies=[],
                input_template={"increment": 1},
            ),
        ],
    )

    with pytest.raises(ValidationError, match="version mismatch"):
        service.submit_workflow(payload)


def test_rejection_of_undeclared_output_dependency(db_session: Session) -> None:
    """Output reference requires step to be explicitly declared in dependencies."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Undeclared Reference Workflow",
        steps=[
            StepDefinition(
                step_key="step_first",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": 2},
            ),
            StepDefinition(
                step_key="step_second",
                capability_id="test.idempotent_counter",
                dependencies=[],  # Omitted step_first from dependencies!
                input_template={
                    "increment": {"$from_step": "step_first", "$path": "incremented_by"}
                },
            ),
        ],
    )

    with pytest.raises(ValidationError, match="not listed in dependencies"):
        service.submit_workflow(payload)


def test_rejection_of_invalid_static_input_schema(db_session: Session) -> None:
    """Static input violating capability schema must be rejected before dispatch."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Invalid Schema Workflow",
        steps=[
            StepDefinition(
                step_key="step_bad_input",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": -5},  # ge=1 violated
            ),
        ],
    )

    with pytest.raises(ValidationError, match="static input fails schema validation"):
        service.submit_workflow(payload)


def test_failure_propagation_blocks_downstream_steps(db_session: Session) -> None:
    """When a prerequisite step fails, dependent steps are blocked, not executed."""
    import asyncio
    service = WorkflowService(db_session)

    payload = WorkflowCreate(
        name="Failing Prerequisite Workflow",
        steps=[
            StepDefinition(
                step_key="step_fail",
                capability_id="test.controlled_failure",
                dependencies=[],
                input_template={"should_fail": True, "error_message": "Hard failure in step 1"},
            ),
            StepDefinition(
                step_key="step_dependent",
                capability_id="test.idempotent_counter",
                dependencies=["step_fail"],
                input_template={"increment": 1},
            ),
        ],
    )

    run = service.submit_workflow(payload)
    advanced = asyncio.run(service.advance_workflow(run.id, caller_permissions={"test:execute"}))

    assert advanced.status == "failed"
    step1 = next(s for s in advanced.steps if s.step_key == "step_fail")
    step2 = next(s for s in advanced.steps if s.step_key == "step_dependent")

    assert step1.status == "failed"
    assert "Hard failure in step 1" in (step1.error_message or "")

    assert step2.status == "blocked"
    assert step2.tool_execution_id is None  # Never dispatched


def test_consequential_action_halts_for_human_approval(db_session: Session) -> None:
    """Consequential capability halts workflow at awaiting_approval until approved."""
    import asyncio
    service = WorkflowService(db_session)

    payload = WorkflowCreate(
        name="Consequential Action Workflow",
        steps=[
            StepDefinition(
                step_key="consequential_step",
                capability_id="test.consequential_action",
                dependencies=[],
                input_template={"resource_id": "srv-01", "operation": "hard_reboot"},
            ),
        ],
    )

    run = service.submit_workflow(payload)
    # First advance halts at awaiting_approval
    advanced = asyncio.run(
        service.advance_workflow(run.id, caller_permissions={"admin:consequential"})
    )

    assert advanced.status == "awaiting_approval"
    step = advanced.steps[0]
    assert step.status == "awaiting_approval"
    assert step.tool_execution_id is not None

    # Verify tool execution record
    exec_record = service.engine.exec_repo.get_by_id(step.tool_execution_id)
    assert exec_record is not None
    assert exec_record.status == "awaiting_approval"

    # Human operator approves the tool execution
    service.engine.approve(exec_record.id, approver="lead_engineer")
    db_session.commit()

    # Second advance executes approved execution and completes workflow
    resumed = asyncio.run(
        service.advance_workflow(run.id, caller_permissions={"admin:consequential"})
    )

    assert resumed.status == "succeeded"
    assert resumed.completed_steps == 1
    assert resumed.steps[0].status == "succeeded"
    assert resumed.steps[0].output["performed"] is True


def test_idempotent_workflow_submission(db_session: Session) -> None:
    """Workflow submitted with existing idempotency_key returns identical run without duplication."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Idempotent Workflow",
        idempotency_key="wf-unique-key-42",
        steps=[
            StepDefinition(
                step_key="step_1",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": 1},
            ),
        ],
    )

    run1 = service.submit_workflow(payload)
    run2 = service.submit_workflow(payload)

    assert run1.id == run2.id
    assert run1.created_at == run2.created_at


def test_workflow_cancellation(db_session: Session) -> None:
    """Cancelling a workflow stops pending and ready steps."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Cancellable Workflow",
        steps=[
            StepDefinition(
                step_key="step_1",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": 1},
            ),
            StepDefinition(
                step_key="step_2",
                capability_id="test.idempotent_counter",
                dependencies=["step_1"],
                input_template={"increment": 2},
            ),
        ],
    )

    run = service.submit_workflow(payload)
    cancelled = service.cancel_workflow(run.id, reason="Operator requested immediate stop")

    assert cancelled.status == "cancelled"
    for s in cancelled.steps:
        assert s.status == "cancelled"
        assert s.error_message == "Operator requested immediate stop"


def test_interrupted_workflow_recovery(db_session: Session) -> None:
    """Interrupted workflows on restart are reconciled without blind replay."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Interrupted Workflow",
        steps=[
            StepDefinition(
                step_key="step_running",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": 1},
            ),
            StepDefinition(
                step_key="step_pending",
                capability_id="test.idempotent_counter",
                dependencies=["step_running"],
                input_template={"increment": 2},
            ),
        ],
    )

    run = service.submit_workflow(payload)
    # Simulate a crash where step_running was left in 'running' status
    step_running = next(s for s in run.steps if s.step_key == "step_running")
    service.repo.update_step_status(step_running.id, status="running")
    service.repo.update_run_status(run.id, status="running")
    db_session.commit()

    # Recover
    reconciled = service.recover_interrupted_workflows()
    assert run.id in reconciled

    refreshed = service.get_workflow(run.id)
    assert refreshed.status == "failed"
    s1 = next(s for s in refreshed.steps if s.step_key == "step_running")
    s2 = next(s for s in refreshed.steps if s.step_key == "step_pending")
    assert s1.status == "failed"
    assert s2.status == "blocked"


def test_workflow_bounds_enforcement(db_session: Session) -> None:
    """Enforces graph step limit (max 50) and depth limit (max 20)."""
    from pydantic import ValidationError as PydanticValidationError
    service = WorkflowService(db_session)

    # Exceed step count limit (> 50)
    too_many_steps = [
        StepDefinition(
            step_key=f"step_{i}",
            capability_id="test.idempotent_counter",
            dependencies=[],
            input_template={"increment": 1},
        )
        for i in range(55)
    ]
    with pytest.raises((ValidationError, PydanticValidationError)):
        service.submit_workflow(WorkflowCreate(name="Too Many Steps", steps=too_many_steps))

    # Exceed depth limit (> 20)
    chain_steps = [
        StepDefinition(
            step_key=f"chain_{i}",
            capability_id="test.idempotent_counter",
            dependencies=[f"chain_{i-1}"] if i > 0 else [],
            input_template={"increment": 1},
        )
        for i in range(25)
    ]
    with pytest.raises(ValidationError, match="maximum limit of 20"):
        service.submit_workflow(WorkflowCreate(name="Too Deep Chain", steps=chain_steps))


def test_event_ledger_audit_entries(db_session: Session) -> None:
    """Workflow state transitions emit immutable event ledger records."""
    import asyncio
    service = WorkflowService(db_session)
    ledger_repo = EventLedgerRepository(db_session)

    payload = WorkflowCreate(
        name="Audited Workflow",
        correlation_id="corr-audit-99",
        steps=[
            StepDefinition(
                step_key="audit_step",
                capability_id="test.idempotent_counter",
                dependencies=[],
                input_template={"increment": 3},
            ),
        ],
    )

    run = service.submit_workflow(payload)
    asyncio.run(service.advance_workflow(run.id, caller_permissions={"test:execute"}))

    events = ledger_repo.list_events(correlation_id="corr-audit-99")
    event_types = [e.event_type for e in events]

    assert "workflow_submitted" in event_types
    assert "workflow_step_succeeded" in event_types
    assert "workflow_succeeded" in event_types


def test_workflow_api_endpoints(client: TestClient) -> None:
    """Test full HTTP API lifecycle: submit, inspect, advance, list, cancel."""
    # 1. Submit workflow
    submit_resp = client.post(
        "/api/workflows",
        json={
            "name": "API Test Workflow",
            "data_classification": "INTERNAL",
            "steps": [
                {
                    "step_key": "step_one",
                    "capability_id": "test.idempotent_counter",
                    "dependencies": [],
                    "input_template": {"increment": 10},
                },
            ],
        },
    )
    assert submit_resp.status_code == 201
    data = submit_resp.json()
    wf_id = data["id"]
    assert data["status"] == "queued"
    assert data["total_steps"] == 1

    # 2. Get workflow
    get_resp = client.get(f"/api/workflows/{wf_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "API Test Workflow"

    # 3. List workflows
    list_resp = client.get("/api/workflows?limit=10")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] >= 1

    # 4. Advance workflow
    adv_resp = client.post(
        f"/api/workflows/{wf_id}/advance",
        json={"caller_permissions": ["test:execute"], "max_steps": 5},
    )
    assert adv_resp.status_code == 200
    adv_data = adv_resp.json()
    assert adv_data["status"] == "succeeded"
    assert adv_data["completed_steps"] == 1
    assert adv_data["steps"][0]["status"] == "succeeded"

    # 5. Cancel workflow endpoint
    cancel_resp = client.post(
        f"/api/workflows/{wf_id}/cancel",
        json={"reason": "Test cancel after completion"},
    )
    assert cancel_resp.status_code == 200


def test_workflow_alembic_migration(tmp_path) -> None:
    """Verifies that Alembic upgrade head and downgrade cleanly apply workflow schema."""
    from alembic import command
    from alembic.config import Config

    test_db = tmp_path / "migration_test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{test_db}")

    # Upgrade to head
    command.upgrade(cfg, "head")

    # Downgrade back to previous head
    command.downgrade(cfg, "4e195bc031da")

    # Upgrade again
    command.upgrade(cfg, "head")

