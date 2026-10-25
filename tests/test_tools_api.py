import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pico.main import app
from pico.services.memory_service import MemoryService


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_api_list_and_get_capabilities(client: TestClient) -> None:
    # 1. List capabilities
    res = client.get("/api/tools/capabilities")
    assert res.status_code == 200
    caps = res.json()
    cap_ids = [c["capability_id"] for c in caps]
    assert "core.calculator" in cap_ids
    assert "core.validate_json" in cap_ids
    assert "db.read_project" in cap_ids

    # 2. Get specific capability
    calc_res = client.get("/api/tools/capabilities/core.calculator")
    assert calc_res.status_code == 200
    calc = calc_res.json()
    assert calc["name"] == "Safe Bounded Calculator"
    assert calc["risk_tier"] == "READ_ONLY"
    assert calc["idempotent"] is True


def test_api_submit_and_execute_tool(client: TestClient) -> None:
    # Submit calculator job with execute_now=True
    res = client.post(
        "/api/tools/executions?execute_now=true",
        json={
            "capability_id": "core.calculator",
            "input_data": {"expression": "25 * 4"},
            "idempotency_key": "api_test_calc_key_1",
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "succeeded"
    assert data["verification_status"] == "passed"
    assert data["output_data"]["result"] == 100.0

    # Retrieve execution by ID
    exec_id = data["id"]
    get_res = client.get(f"/api/tools/executions/{exec_id}")
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "succeeded"

    # Retrieve event trace for execution
    events_res = client.get(f"/api/tools/executions/{exec_id}/events")
    assert events_res.status_code == 200
    events = events_res.json()["items"]
    event_types = [e["event_type"] for e in events]
    assert "task_submitted" in event_types
    assert "execution_succeeded" in event_types


def test_unauthenticated_consequential_submission_fails_closed(client: TestClient) -> None:
    """
    Adversarial regression test for Finding A:
    Submitting test.consequential_action without an operator session or valid
    server-side approval token MUST be rejected with HTTP 403 Forbidden due to
    missing 'admin:consequential' permission, create NO database execution record,
    and produce NO consequential side effect.
    """
    # 1. Unauthenticated submission without credentials
    before_res = client.get("/api/tools/executions?capability_id=test.consequential_action")
    assert before_res.status_code == 200
    initial_count = len(before_res.json()["items"])

    res = client.post(
        "/api/tools/executions?execute_now=true",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "sys_disk_unique_probe", "operation": "wipe"},
        },
    )
    assert res.status_code == 403
    assert "admin:consequential" in res.json()["detail"]

    # 2. Verify no execution record was created
    after_res = client.get("/api/tools/executions?capability_id=test.consequential_action")
    assert after_res.status_code == 200
    assert len(after_res.json()["items"]) == initial_count


def test_api_consequential_approval_and_execution(client: TestClient) -> None:
    # 1. Pair operator session via trusted CLI challenge
    from pico.services.operator_session import OperatorSessionService
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code()
    pair_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert pair_res.status_code == 200
    csrf_token = pair_res.json()["csrf_token"]
    assert pair_res.json()["approver"] == "local_operator"

    # 2. Authenticated operator submits consequential capability -> enters awaiting_approval
    res = client.post(
        "/api/tools/executions?execute_now=true",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "sys_disk_0", "operation": "wipe"},
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "awaiting_approval"
    exec_id = data["id"]

    # 3. Reject unauthenticated approver even if claiming a trusted identity string
    # Use clean unauthenticated client
    unauth_client = TestClient(app)
    naked_res = unauth_client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"approver": "local_operator"},
    )
    assert naked_res.status_code == 403

    # 4. Approve with authenticated operator session, attempting to pass a spoofed approver in JSON
    approve_res = client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"approver": "forged_operator_override", "notes": "Approved for testing via operator session"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert approve_res.status_code == 200
    assert approve_res.json()["status"] == "queued"
    # Identity MUST be bound to local_operator, ignoring the JSON payload attempt
    assert approve_res.json()["approved_by"] == "local_operator"

    # 5. Trigger execution (with active operator session)
    run_res = client.post(
        f"/api/tools/executions/{exec_id}/execute",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert run_res.status_code == 200
    assert run_res.json()["status"] == "succeeded"
    assert run_res.json()["output_data"]["performed"] is True


def test_api_cancel_execution(client: TestClient) -> None:
    from pico.services.operator_session import OperatorSessionService
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code()
    pair_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert pair_res.status_code == 200
    csrf_token = pair_res.json()["csrf_token"]

    # Submit consequential capability with authenticated operator which stays in awaiting_approval
    res = client.post(
        "/api/tools/executions?execute_now=false",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "res_1", "operation": "action"},
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert res.status_code == 201
    exec_id = res.json()["id"]

    cancel_res = client.post(
        f"/api/tools/executions/{exec_id}/cancel?reason=Aborted",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"


def test_api_validate_procedural_memory_against_capabilities(
    client: TestClient,
) -> None:
    # 1. Create a procedure referencing valid capabilities
    create_res = client.post(
        "/api/memory/procedures",
        json={
            "name": "diagnostic_and_calculation_routine",
            "description": "Routine description",
            "steps": [
                {"action": "system.diagnostics", "expected": "system is healthy"},
                {"action": "core.calculator", "expected": "calculation done"},
            ],
            "data_classification": "INTERNAL",
        },
    )
    assert create_res.status_code == 201
    proc = create_res.json()
    proc_id = proc["id"]

    val_res = client.post(f"/api/tools/procedures/{proc_id}/validate")
    assert val_res.status_code == 200
    val_data = val_res.json()
    assert val_data["is_valid"] is True
    assert val_data["total_steps"] == 2
    assert "system.diagnostics" in val_data["resolved_capabilities"]
    assert "core.calculator" in val_data["resolved_capabilities"]
    assert len(val_data["missing_capabilities"]) == 0

    # 2. Create a procedure with an unapproved/unregistered action
    create_bad_res = client.post(
        "/api/memory/procedures",
        json={
            "name": "unregistered_arbitrary_script",
            "description": "Unregistered script description",
            "steps": [{"action": "arbitrary.shell.exec", "expected": "unauthorized"}],
            "data_classification": "INTERNAL",
        },
    )
    assert create_bad_res.status_code == 201
    proc_bad = create_bad_res.json()
    proc_bad_id = proc_bad["id"]

    val_bad_res = client.post(f"/api/tools/procedures/{proc_bad_id}/validate")
    assert val_bad_res.status_code == 200
    val_bad_data = val_bad_res.json()
    assert val_bad_data["is_valid"] is False
    assert "arbitrary.shell.exec" in val_bad_data["missing_capabilities"]
