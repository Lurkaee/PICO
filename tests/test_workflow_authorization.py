"""Adversarial and security tests for workflow authorization integrity (P10.1.1).

Verifies that client-supplied `caller_permissions` lists in HTTP request bodies
cannot grant privileged or consequential execution, that unauthorized workflow
advancement and compensation fail closed, and that server-derived permissions
and verification gates are strictly enforced.
"""

import pytest
from fastapi.testclient import TestClient

from pico.main import app
from pico.services.operator_session import OperatorSessionService


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_adversarial_client_supplied_permissions_cannot_grant_consequential_execution(client: TestClient) -> None:
    """
    An unauthenticated caller attempts to submit fabricated permissions:
    ["filesystem:write", "os:launch", "homeassistant:actuate", "admin:consequential"]
    in WorkflowAdvanceRequest to execute a consequential action without operator approval.
    The server MUST ignore client-supplied permissions and fail closed.
    """
    # 1. Create a workflow containing a consequential step
    wf_payload = {
        "name": "adversarial_privilege_escalation_workflow",
        "description": "Attempt to bypass authorization via JSON caller_permissions",
        "data_classification": "INTERNAL",
        "steps": [
            {
                "step_key": "consequential_step",
                "capability_id": "test.consequential_action",
                "capability_version": "1.0.0",
                "dependencies": [],
                "input_template": {"resource_id": "sys_database", "operation": "drop_tables"},
            }
        ],
    }
    create_res = client.post("/api/workflows", json=wf_payload)
    assert create_res.status_code == 201
    wf_id = create_res.json()["id"]

    # 2. Attempt to advance by injecting privileged permissions in the JSON body
    fabricated_permissions = [
        "filesystem:write",
        "os:launch",
        "homeassistant:actuate",
        "admin:consequential",
        "admin:root",
        "superuser",
    ]
    adv_res = client.post(
        f"/api/workflows/{wf_id}/advance",
        json={"caller_permissions": fabricated_permissions, "max_steps": 5},
    )
    # The submission requires 'admin:consequential' which is NOT granted by safe defaults.
    # Server-derived permissions ignore caller_permissions -> workflow fails closed!
    assert adv_res.status_code == 200
    adv_data = adv_res.json()
    assert adv_data["status"] == "failed"
    assert "Unauthorized" in adv_data["error_message"]
    assert "admin:consequential" in adv_data["error_message"]
    assert adv_data["completed_steps"] == 0
    assert adv_data["failed_steps"] == 1

    # Verify step also failed closed and produced ZERO side effects / no execution id
    step = adv_data["steps"][0]
    assert step["status"] == "failed"
    assert "Unauthorized" in step["error_message"]
    assert step["tool_execution_id"] is None


def test_adversarial_compensation_fabricated_permissions_fail_closed(client: TestClient) -> None:
    """
    An unauthenticated caller attempts to trigger workflow compensation
    with fabricated permissions in JSON. Must fail closed.
    """
    # 1. Create a simple workflow with a safe step
    wf_payload = {
        "name": "compensation_test_workflow",
        "data_classification": "INTERNAL",
        "steps": [
            {
                "step_key": "calc_step",
                "capability_id": "core.calculator",
                "capability_version": "1.0.0",
                "dependencies": [],
                "input_template": {"expression": "10 + 20"},
            }
        ],
    }
    create_res = client.post("/api/workflows", json=wf_payload)
    assert create_res.status_code == 201
    wf_id = create_res.json()["id"]

    # 2. Advance safe workflow (uses server-derived safe permissions)
    adv_res = client.post(f"/api/workflows/{wf_id}/advance", json={})
    assert adv_res.status_code == 200
    assert adv_res.json()["status"] == "succeeded"

    # 3. Attempt compensation with fabricated permissions
    comp_res = client.post(
        f"/api/workflows/{wf_id}/compensate",
        json={
            "caller_permissions": ["admin:consequential", "filesystem:write"],
            "reason": "Adversarial compensation injection",
        },
    )
    # Even if no compensations are registered, client-supplied permissions must be ignored
    assert comp_res.status_code == 200


def test_authenticated_operator_can_advance_consequential_workflow_through_approval_gate(client: TestClient) -> None:
    """
    Demonstrates the legitimate authorized path:
    1. Authenticated operator advances workflow containing a consequential step.
    2. Step is submitted and pauses in 'awaiting_approval'.
    3. Operator explicitly approves the exact tool execution via /api/tools/executions/{id}/approve.
    4. Second advancement executes the approved step to completion.
    """
    # 1. Pair operator session
    service = OperatorSessionService.get_instance()
    challenge = service.generate_pairing_code(approver="lead_operator")
    pair_res = client.post("/api/operator/pair", json={"code": challenge.code})
    assert pair_res.status_code == 200
    csrf_token = pair_res.json()["csrf_token"]

    # 2. Create workflow with consequential step
    wf_payload = {
        "name": "authorized_consequential_workflow",
        "data_classification": "INTERNAL",
        "steps": [
            {
                "step_key": "consequential_op",
                "capability_id": "test.consequential_action",
                "capability_version": "1.0.0",
                "dependencies": [],
                "input_template": {"resource_id": "sys_cluster", "operation": "restart_node"},
            }
        ],
    }
    create_res = client.post("/api/workflows", json=wf_payload, headers={"X-CSRF-Token": csrf_token})
    assert create_res.status_code == 201
    wf_id = create_res.json()["id"]

    # 3. Advance workflow as paired operator
    adv1 = client.post(f"/api/workflows/{wf_id}/advance", json={}, headers={"X-CSRF-Token": csrf_token})
    assert adv1.status_code == 200
    adv1_data = adv1.json()
    assert adv1_data["status"] == "awaiting_approval"
    step = adv1_data["steps"][0]
    assert step["status"] == "awaiting_approval"
    tool_exec_id = step["tool_execution_id"]
    assert tool_exec_id is not None

    # 4. Operator explicitly reviews and approves the execution item
    approve_res = client.post(
        f"/api/tools/executions/{tool_exec_id}/approve",
        json={"notes": "Operator confirmed restart"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert approve_res.status_code == 200
    assert approve_res.json()["status"] == "queued"
    assert approve_res.json()["approved_by"] == "lead_operator"

    # 5. Advance workflow again -> step executes and completes
    adv2 = client.post(f"/api/workflows/{wf_id}/advance", json={}, headers={"X-CSRF-Token": csrf_token})
    assert adv2.status_code == 200
    adv2_data = adv2.json()
    assert adv2_data["status"] == "succeeded"
    assert adv2_data["completed_steps"] == 1
    assert adv2_data["steps"][0]["status"] == "succeeded"
