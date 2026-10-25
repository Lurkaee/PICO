import pytest
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.core.config import settings
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
async def test_consequential_action_requires_authenticated_approval(db_session: Session) -> None:
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

    # 3. Self-asserted trusted identity string WITHOUT authenticated session or token is REJECTED
    with pytest.raises(AuthorizationError) as auth_err:
        engine.approve(item.id, approver="lead_engineer")
    assert "fails closed without valid server-side" in str(auth_err.value)

    # 4. Untrusted caller identity without credentials is also rejected
    with pytest.raises(AuthorizationError) as auth_err2:
        engine.approve(item.id, approver="malicious_untrusted_caller")
    assert "fails closed without valid server-side" in str(auth_err2.value)

    # 5. Verified local operator session allows approval and records authenticated approver
    approved = engine.approve(
        item.id,
        approver="untrusted_caller_override_attempt",
        notes="Confirmed deletion request via local operator session.",
        authenticated_principal="local_operator",
    )
    assert approved.status == "queued"
    # Approver MUST be bound to local_operator, ignoring any caller-supplied override attempt
    assert approved.approved_by == "local_operator"
    assert approved.approved_at is not None
    assert approved.approval_input_digest == item.input_digest

    # 6. Now execution succeeds
    res = await engine.execute(item.id, caller_permissions=perms)
    assert res.status == "succeeded"
    assert res.output_json["performed"] is True


@pytest.mark.anyio
async def test_consequential_action_approval_with_server_secret_token(
    db_session: Session, monkeypatch
) -> None:
    token = "secret_test_token_789"
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", token)
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"admin:consequential"}

    item = engine.submit(
        capability_id="test.consequential_action",
        input_data={"resource_id": "res_token", "operation": "restart"},
        caller_permissions=perms,
    )

    # Approval with invalid token rejected
    with pytest.raises(AuthorizationError):
        engine.approve(item.id, approval_token="wrong_token")

    # Approval with valid server token succeeds
    approved = engine.approve(item.id, approval_token=token)
    assert approved.status == "queued"
    assert approved.approved_by == "authenticated_token_holder"


@pytest.mark.anyio
async def test_tampered_input_invalidates_approval(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"admin:consequential"}

    item = engine.submit(
        capability_id="test.consequential_action",
        input_data={"resource_id": "res_safe", "operation": "restart"},
        caller_permissions=perms,
    )
    # Approve via verified local operator session
    approved = engine.approve(
        item.id, approver="lead_engineer", authenticated_principal="local_operator"
    )
    assert approved.status == "queued"

    # Simulate tampering with the input payload in the DB
    approved.input_digest = "tampered_digest_value"
    db_session.commit()

    # Attempt execution: Must fail closed because digest does not match approved digest
    with pytest.raises(AuthorizationError) as exc_info:
        await engine.execute(item.id, caller_permissions=perms)
    assert "Approval invalidated" in str(exc_info.value)
