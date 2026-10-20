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


def test_api_consequential_approval_and_execution(client: TestClient) -> None:
    # 1. Submit consequential capability -> awaiting_approval
    res = client.post(
        "/api/tools/executions?execute_now=true",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "sys_disk_0", "operation": "wipe"},
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "awaiting_approval"
    exec_id = data["id"]

    # 2. Reject untrusted approver
    untrusted_res = client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"approver": "untrusted_robot_1"},
    )
    assert untrusted_res.status_code == 403

    # 3. Approve with trusted human operator
    approve_res = client.post(
        f"/api/tools/executions/{exec_id}/approve",
        json={"approver": "lead_engineer", "notes": "Approved for testing"},
    )
    assert approve_res.status_code == 200
    assert approve_res.json()["status"] == "queued"

    # 4. Trigger execution
    run_res = client.post(f"/api/tools/executions/{exec_id}/execute")
    assert run_res.status_code == 200
    assert run_res.json()["status"] == "succeeded"
    assert run_res.json()["output_data"]["performed"] is True


def test_api_cancel_execution(client: TestClient) -> None:
    # Submit consequential capability which stays in awaiting_approval
    res = client.post(
        "/api/tools/executions?execute_now=false",
        json={
            "capability_id": "test.consequential_action",
            "input_data": {"resource_id": "res_1", "operation": "action"},
        },
    )
    assert res.status_code == 201
    exec_id = res.json()["id"]

    cancel_res = client.post(f"/api/tools/executions/{exec_id}/cancel?reason=Aborted")
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
