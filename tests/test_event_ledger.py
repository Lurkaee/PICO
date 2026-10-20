import pytest
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.services.execution_engine import ToolExecutionEngine


@pytest.mark.anyio
async def test_event_ledger_lifecycle_audit_trail(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"tools:calculate"}

    item = engine.submit(
        capability_id="core.calculator",
        input_data={"expression": "10 * 10"},
        caller_permissions=perms,
        correlation_id="corr_abc_1",
    )
    await engine.execute(item.id, caller_permissions=perms, correlation_id="corr_abc_1")

    # Inspect event ledger audit trace
    ledger = EventLedgerRepository(db_session)
    events = ledger.list_events(execution_id=item.id)
    event_types = [ev.event_type for ev in events]

    assert "task_submitted" in event_types
    assert "execution_started" in event_types
    assert "verification_started" in event_types
    assert "verification_passed" in event_types
    assert "execution_succeeded" in event_types


def test_event_ledger_automatic_credential_redaction(db_session: Session) -> None:
    ledger = EventLedgerRepository(db_session)

    # Append event with secret and credentials
    ev = ledger.append(
        event_type="test_sensitive_event",
        payload_json={
            "user": "alice",
            "api_key": "sk-123456789secret",
            "auth_token": "bearer-xyz",
            "nested": {
                "db_password": "super-secret-password",
                "normal_field": "ok",
            },
        },
    )

    stored = ledger.get_by_id(ev.id)
    assert stored is not None
    payload = stored.payload_json
    assert payload["api_key"] == "[REDACTED]"
    assert payload["auth_token"] == "[REDACTED]"
    assert payload["nested"]["db_password"] == "[REDACTED]"
    assert payload["nested"]["normal_field"] == "ok"
    assert payload["user"] == "alice"


def test_event_ledger_persists_across_reopen(db_session: Session) -> None:
    ledger = EventLedgerRepository(db_session)
    ev = ledger.append(
        event_type="test_durable_event",
        payload_json={"test_marker": 42},
    )
    ev_id = ev.id

    # Simulate close and reopen
    db_session.expire_all()
    reloaded = ledger.get_by_id(ev_id)
    assert reloaded is not None
    assert reloaded.payload_json["test_marker"] == 42
