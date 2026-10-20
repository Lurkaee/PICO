import pytest
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.core.errors import AuthorizationError
from pico.services.execution_engine import ToolExecutionEngine


def test_unauthorized_permission_fails_closed(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)

    # Missing 'tools:calculate' permission
    with pytest.raises(AuthorizationError) as exc_info:
        engine.submit(
            capability_id="core.calculator",
            input_data={"expression": "1 + 1"},
            caller_permissions={"other:scope"},
        )
    assert "tools:calculate" in str(exc_info.value)


@pytest.mark.anyio
async def test_consequential_action_requires_human_approval(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"admin:consequential"}

    # 1. Submit consequential capability -> initializes in 'awaiting_approval'
    item = engine.submit(
        capability_id="test.consequential_action",
        input_data={"resource_id": "res_99", "operation": "delete_all"},
        caller_permissions=perms,
    )
    assert item.status == "awaiting_approval"
    assert item.approved_by is None

    # 2. Cannot execute directly while awaiting approval
    with pytest.raises(Exception):
        await engine.execute(item.id, caller_permissions=perms)

    # 3. Untrusted approver is rejected
    with pytest.raises(AuthorizationError) as auth_err:
        engine.approve(item.id, approver="malicious_untrusted_caller")
    assert "not authorized" in str(auth_err.value)

    # 4. Valid human approver succeeds
    approved = engine.approve(
        item.id, approver="lead_engineer", notes="Confirmed deletion request."
    )
    assert approved.status == "queued"
    assert approved.approved_by == "lead_engineer"
    assert approved.approved_at is not None
    assert approved.approval_input_digest == item.input_digest

    # 5. Now execution succeeds
    res = await engine.execute(item.id, caller_permissions=perms)
    assert res.status == "succeeded"
    assert res.output_json["performed"] is True


@pytest.mark.anyio
async def test_tampered_input_invalidates_approval(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"admin:consequential"}

    item = engine.submit(
        capability_id="test.consequential_action",
        input_data={"resource_id": "res_safe", "operation": "restart"},
        caller_permissions=perms,
    )
    # Approve the safe input
    approved = engine.approve(item.id, approver="lead_engineer")
    assert approved.status == "queued"

    # Simulate tampering with the input payload in the DB
    approved.input_digest = "tampered_digest_value"
    db_session.commit()

    # Attempt execution: Must fail closed because digest does not match approved digest
    with pytest.raises(AuthorizationError) as exc_info:
        await engine.execute(item.id, caller_permissions=perms)
    assert "Approval invalidated" in str(exc_info.value)
