import pytest
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.services.execution_engine import ToolExecutionEngine


@pytest.mark.anyio
async def test_successful_execution_and_verification(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"tools:calculate"}

    # 1. Submit safe calculator job
    exec_item = engine.submit(
        capability_id="core.calculator",
        input_data={"expression": "(12 * 4) + 2"},
        caller_permissions=perms,
    )
    assert exec_item.status == "queued"
    assert exec_item.idempotency_key is None
    assert exec_item.verification_status == "pending"

    # 2. Execute through sandbox and independent verifier
    result = await engine.execute(exec_item.id, caller_permissions=perms)
    assert result.status == "succeeded"
    assert result.verification_status == "passed"
    assert result.output_json["result"] == 50.0
    assert result.completed_at is not None


@pytest.mark.anyio
async def test_handler_execution_failure(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"test:execute"}

    exec_item = engine.submit(
        capability_id="test.controlled_failure",
        input_data={"should_fail": True, "error_message": "Network peer terminated"},
        caller_permissions=perms,
    )
    assert exec_item.status == "queued"

    result = await engine.execute(exec_item.id, caller_permissions=perms)
    assert result.status == "failed"
    assert "Network peer terminated" in (result.error_message or "")


@pytest.mark.anyio
async def test_independent_verifier_failure(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"test:execute"}

    # Capability handler returns ok, but independent verification fails
    exec_item = engine.submit(
        capability_id="test.verifier_failure",
        input_data={"trigger_verifier_failure": True},
        caller_permissions=perms,
    )
    result = await engine.execute(exec_item.id, caller_permissions=perms)

    assert result.status == "verification_failed"
    assert result.verification_status == "failed"
    assert "Independent verifier detected invalid postcondition proof" in (result.error_message or "")


@pytest.mark.anyio
async def test_execution_timeout_deadline(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"test:execute"}

    # Capability has a 0.5s timeout, we ask it to sleep for 2.0s
    exec_item = engine.submit(
        capability_id="test.slow_timeout",
        input_data={"sleep_seconds": 2.0},
        caller_permissions=perms,
    )
    result = await engine.execute(exec_item.id, caller_permissions=perms)
    assert result.status == "timed_out"
    assert "deadline" in (result.error_message or "").lower()


@pytest.mark.anyio
async def test_idempotency_deduplication(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"test:execute"}
    idem_key = "key_unique_tx_123"

    # 1. First submission
    item1 = engine.submit(
        capability_id="test.idempotent_counter",
        input_data={"increment": 5},
        idempotency_key=idem_key,
        caller_permissions=perms,
    )
    res1 = await engine.execute(item1.id, caller_permissions=perms)
    assert res1.status == "succeeded"

    # 2. Second submission with identical key
    item2 = engine.submit(
        capability_id="test.idempotent_counter",
        input_data={"increment": 5},
        idempotency_key=idem_key,
        caller_permissions=perms,
    )
    # Must return existing execution record without creating a new one
    assert item2.id == item1.id
    assert item2.status == "succeeded"
    assert item2.output_json["incremented_by"] == 5


def test_execution_cancellation(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"tools:calculate"}

    item = engine.submit(
        capability_id="core.calculator",
        input_data={"expression": "100 + 1"},
        caller_permissions=perms,
    )
    assert item.status == "queued"

    cancelled = engine.cancel(item.id, reason="User clicked abort")
    assert cancelled.status == "cancelled"
    assert "User clicked abort" in (cancelled.error_message or "")


def test_crash_recovery_of_interrupted_executions(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"tools:calculate"}

    item = engine.submit(
        capability_id="core.calculator",
        input_data={"expression": "1 + 1"},
        caller_permissions=perms,
    )
    # Simulate an execution that was in 'running' when server crashed
    engine.exec_repo.update_status(item.id, status="running")

    # Run startup recovery
    recovered = engine.recover_interrupted_executions()
    assert item.id in recovered

    reloaded = engine.exec_repo.get_by_id(item.id)
    assert reloaded.status == "failed"
    assert "Interrupted" in (reloaded.error_message or "")
