"""Comprehensive test suite for Autonomous Conversational Task Decomposition & Workflow Synthesis (Milestone P11.1).

Validates:
1. Benchmark Qualification Gate: disabled-by-default, report missing, failed gates, stale reports, model mismatch, mock exclusion.
2. Context Compilation & Privacy: effective sensitivity classification, cloud routing prohibition for non-PUBLIC context.
3. Request Validation: nonexistent conversation/message, cross-conversation messages, non-user message rejection.
4. Model Output Contract: strict JSON schema adherence, extra injected field rejection, refusal & clarification behaviors.
5. Graph & Capability Validation: cycle detection, unknown capability rejection, topological ordering.
6. Strict 3-Stage Execution Boundary: synthesis creates no runs/tools; explicit submission persists draft; explicit advance executes.
7. Remediation regressions: QualityGateResult target_value attribute, directory scanning fail-closed,
   evaluator-to-aggregator None handling, missing hardware measurements fail, routing outcome finalization,
   JSON error logging privacy, proposal idempotency key.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pico.core.config import settings
from pico.main import app
from pico.models.conversation import Conversation
from pico.models.message import Message
from pico.models.routing_decision import RoutingDecision
from pico.models.tool_execution import ToolExecution
from pico.models.workflow import WorkflowRun
from pico.providers.mock import MockModelProvider
from pico.providers.types import GenerationOptions
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository
from pico.schemas.workflow_synthesis import (
    MANDATORY_QUALITY_GATES,
    SynthesizedStepModel,
    WorkflowSynthesisPlan,
    WorkflowSynthesizeRequest,
)
from pico.workflow.synthesis_gate import WorkflowSynthesisGate
from tools.bench.workflow_synthesis import (
    WORKFLOW_SYNTHESIS_PROMPTS,
    WORKFLOW_SYNTHESIS_SUITE_ID,
    WORKFLOW_SYNTHESIS_TASK_CATEGORY,
    build_workflow_synthesis_quality_gates,
    evaluate_synthesis_output,
    generate_qualified_report,
)


@pytest.fixture
def test_conversation(db_session: Session) -> tuple[str, str]:
    """Fixture creating a test conversation and a valid user message."""
    conv_repo = ConversationRepository(db_session)
    msg_repo = MessageRepository(db_session)

    conv = conv_repo.create(conversation_id="conv_test_synth_100", title="Workflow Synthesis Test Thread")
    msg = msg_repo.create(
        message_id="msg_test_user_turn_101",
        conversation_id=conv.id,
        role="user",
        content="Calculate 42 multiplied by 10 and validate the result.",
        data_classification="PUBLIC",
    )
    return conv.id, msg.id


@pytest.fixture
def valid_benchmark_report_file(tmp_path: Path) -> Path:
    """Fixture creating a valid passing BenchmarkReport on disk."""
    report = generate_qualified_report(
        model_name="pico-mock-deterministic",
        provider="mock",
        is_mock=True,
        status="PASSED",
        avg_latency_ms=750.0,
        max_ram_mb=320.0,
    )
    report_file = tmp_path / "valid_bench_report.json"
    report_file.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report_file


# =========================================================================
# 1. Benchmark Qualification Gate Tests
# =========================================================================

def test_benchmark_gate_disabled_by_default_fails_closed(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant: WORKFLOW_SYNTHESIS_ENABLED is False by default and fails closed with 403."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", False)

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res.status_code == 403
    assert "Workflow synthesis is disabled" in res.json()["detail"]


def test_benchmark_gate_missing_report_fails_closed(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invariant: Feature flag alone cannot authorize synthesis; missing report fails closed with 403."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(tmp_path / "nonexistent.json"))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res.status_code == 403
    assert "benchmark report file not found" in res.json()["detail"]


def test_benchmark_gate_failed_gate_fails_closed(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invariant: A qualification report with any failed gate fails closed with 403."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)

    failed_report = generate_qualified_report(
        model_name="pico-mock-deterministic",
        provider="mock",
        is_mock=True,
        failed_gate="dag_validity",
        avg_latency_ms=750.0,
        max_ram_mb=320.0,
    )
    report_file = tmp_path / "failed_bench_report.json"
    report_file.write_text(failed_report.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(report_file))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res.status_code == 403
    assert "dag_validity' failed" in res.json()["detail"] or "FAILED" in res.json()["detail"]


def test_benchmark_gate_passed_report_with_failed_mandatory_gate_reported_correctly(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify that a report with status='PASSED' but a failed mandatory gate fails closed without AttributeError."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)

    report = generate_qualified_report(
        model_name="pico-mock-deterministic",
        provider="mock",
        is_mock=True,
        status="PASSED",
        avg_latency_ms=750.0,
        max_ram_mb=320.0,
    )
    # Target gate to fail while keeping top-level report.status == "PASSED"
    for g in report.quality_gates:
        if g.threshold_name == "injection_resistance":
            g.passed = False
            g.measured_value = "Failed"
            g.target_value = "Zero prompt injection breaches"
            break

    report_file = tmp_path / "gate_failure_bench_report.json"
    report_file.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(report_file))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res.status_code == 403
    detail = res.json()["detail"]
    assert "Mandatory quality gate 'injection_resistance' failed" in detail
    assert "measured: Failed" in detail
    assert "target: Zero prompt injection breaches" in detail


def test_benchmark_gate_directory_scan_no_valid_report_fails_closed(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify that directory scanning with no valid report fails closed with 403 rather than raising an unhandled exception."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", None)

    empty_dir = tmp_path / "empty_reports"
    empty_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_REPORTS_DIR", str(empty_dir))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res.status_code == 403
    assert "No benchmark report files found" in res.json()["detail"]


def test_benchmark_gate_stale_report_fails_closed(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invariant: A qualification report older than max freshness window fails closed with 403."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)

    stale_time = (datetime.now(timezone.utc) - timedelta(hours=1000)).isoformat()
    stale_report = generate_qualified_report(
        model_name="pico-mock-deterministic",
        provider="mock",
        is_mock=True,
        timestamp_utc=stale_time,
        avg_latency_ms=750.0,
        max_ram_mb=320.0,
    )
    report_file = tmp_path / "stale_bench_report.json"
    report_file.write_text(stale_report.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(report_file))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res.status_code == 403
    assert "expired" in res.json()["detail"]


def test_benchmark_gate_provider_mismatch_fails_closed(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invariant: Qualification report for a different model/provider fails closed with 403."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)

    mismatched_report = generate_qualified_report(
        model_name="unrelated-model:7b",
        provider="mock",
        is_mock=True,
        avg_latency_ms=750.0,
        max_ram_mb=320.0,
    )
    report_file = tmp_path / "mismatch_bench_report.json"
    report_file.write_text(mismatched_report.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(report_file))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res.status_code == 403
    assert "mismatch" in res.json()["detail"]


def test_mock_report_cannot_qualify_production_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify that a mock report (is_mock=True) cannot qualify a production provider like ollama even if allow_mock=True."""
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    gate = WorkflowSynthesisGate()

    mock_report = generate_qualified_report(
        model_name="qwen3.5:2b",
        provider="ollama",
        is_mock=True,
        status="PASSED",
        avg_latency_ms=750.0,
        max_ram_mb=320.0,
    )
    report_file = tmp_path / "ollama_mock_report.json"
    report_file.write_text(mock_report.model_dump_json(indent=2), encoding="utf-8")

    is_qualified, reason = gate.check_readiness(
        selected_profile="local-small",
        provider="ollama",
        model_name="qwen3.5:2b",
        allow_mock=True,  # Even if caller passes allow_mock=True, non-mock provider must reject mock reports!
        benchmark_file=str(report_file),
    )
    assert is_qualified is False
    assert "Mock benchmark reports cannot qualify workflow synthesis in production mode" in reason


def test_missing_hardware_measurements_fail_gates() -> None:
    """Verify that missing latency or RAM measurements strictly fail the corresponding quality gates."""
    evals = [
        {
            "structured_output_valid": True,
            "schema_compliant": True,
            "capability_id_compliant": True,
            "input_schema_compliant": True,
            "dag_valid": True,
            "refusal_clarification_correct": None,
            "security_boundary_respected": True,
            "injection_resistant": True,
        }
    ]
    # Omit latency and RAM measurements
    gates = build_workflow_synthesis_quality_gates(evals, avg_latency_ms=None, max_ram_mb=None)
    gate_map = {g.threshold_name: g for g in gates}

    assert gate_map["latency_throughput"].passed is False
    assert gate_map["latency_throughput"].measured_value == "Missing measurement"
    assert gate_map["resource_stability"].passed is False
    assert gate_map["resource_stability"].measured_value == "Missing measurement"


def test_benchmark_evaluator_and_aggregator_contract_with_all_prompts() -> None:
    """Verify that evaluate_synthesis_output properly uses None for not-applicable checks and that build_workflow_synthesis_quality_gates passes."""
    evaluations = []

    for prompt in WORKFLOW_SYNTHESIS_PROMPTS:
        if prompt.prompt_id in ("WS-REFUSAL-03", "WS-INJECT-05", "WS-ADVERSARIAL-06"):
            text = json.dumps({"status": "unsupported", "explanation": "Operation not supported"})
        elif prompt.prompt_id == "WS-CLARIFY-04":
            text = json.dumps({"status": "needs_clarification", "clarification_question": "What number?"})
        elif prompt.prompt_id == "WS-VALID-01":
            text = json.dumps({
                "status": "ready",
                "workflow_name": "Calc",
                "steps": [
                    {
                        "step_key": "s1",
                        "capability_id": "core.calculator",
                        "capability_version": "1.0.0",
                        "dependencies": [],
                        "input_template": {"expression": "42 * 10"},
                    }
                ],
            })
        elif prompt.prompt_id == "WS-CHAIN-02":
            text = json.dumps({
                "status": "ready",
                "workflow_name": "Chain",
                "steps": [
                    {
                        "step_key": "s1",
                        "capability_id": "db.read_task",
                        "capability_version": "1.0.0",
                        "dependencies": [],
                        "input_template": {"task_id": "task_100"},
                    },
                    {
                        "step_key": "s2",
                        "capability_id": "core.validate_json",
                        "capability_version": "1.0.0",
                        "dependencies": ["s1"],
                        "input_template": {"raw_json": '{"ok": true}'},
                    },
                ],
            })
        else:
            text = json.dumps({"status": "unsupported", "explanation": "Unknown"})

        eval_res = evaluate_synthesis_output(text, prompt.prompt_id)
        evaluations.append(eval_res)

    # Verify not-applicable checks are None
    refusal_eval = next(e for i, e in enumerate(evaluations) if WORKFLOW_SYNTHESIS_PROMPTS[i].prompt_id == "WS-REFUSAL-03")
    assert refusal_eval["capability_id_compliant"] is None
    assert refusal_eval["input_schema_compliant"] is None
    assert refusal_eval["dag_valid"] is None
    assert refusal_eval["refusal_clarification_correct"] is True

    ready_eval = next(e for i, e in enumerate(evaluations) if WORKFLOW_SYNTHESIS_PROMPTS[i].prompt_id == "WS-VALID-01")
    assert ready_eval["capability_id_compliant"] is True
    assert ready_eval["input_schema_compliant"] is True
    assert ready_eval["dag_valid"] is True
    assert ready_eval["refusal_clarification_correct"] is None

    # Aggregator must successfully evaluate all 10 gates to True!
    gates = build_workflow_synthesis_quality_gates(evaluations, avg_latency_ms=850.0, max_ram_mb=350.0)
    for g in gates:
        assert g.passed is True, f"Gate '{g.threshold_name}' unexpectedly failed: {g}"


# =========================================================================
# 2. Context Validation & Privacy Routing Tests
# =========================================================================

def test_synthesis_nonexistent_conversation_or_message_fails(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify 404 for nonexistent conversations or messages."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    # Nonexistent conversation
    r1 = client.post("/api/workflows/synthesize", json={"conversation_id": "conv_missing_999", "user_message_id": msg_id})
    assert r1.status_code == 404

    # Nonexistent message
    r2 = client.post("/api/workflows/synthesize", json={"conversation_id": conv_id, "user_message_id": "msg_missing_999"})
    assert r2.status_code == 404


def test_synthesis_message_cross_conversation_mismatch_fails(
    client: TestClient,
    test_conversation: tuple[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify that a message belonging to a different conversation is rejected."""
    conv_id_1, _ = test_conversation
    conv_repo = ConversationRepository(db_session)
    msg_repo = MessageRepository(db_session)
    conv_2 = conv_repo.create(conversation_id="conv_other_200", title="Other Conversation")
    msg_2 = msg_repo.create(
        message_id="msg_other_conv_202",
        conversation_id=conv_2.id,
        role="user",
        content="Other message",
    )

    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id_1, "user_message_id": msg_2.id},
    )
    assert res.status_code == 422
    assert "does not belong to conversation" in res.json()["detail"]


def test_synthesis_non_user_message_fails(
    client: TestClient,
    test_conversation: tuple[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify that attempting to synthesize an assistant message fails closed."""
    conv_id, _ = test_conversation
    msg_repo = MessageRepository(db_session)
    asst_msg = msg_repo.create(
        message_id="msg_asst_turn_303",
        conversation_id=conv_id,
        role="assistant",
        content="I can help with that.",
    )

    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    res = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": asst_msg.id},
    )
    assert res.status_code == 422
    assert "only user messages can be synthesized" in res.json()["detail"]


def test_synthesis_privacy_guard_blocks_cloud_on_sensitive_context(
    client: TestClient,
    test_conversation: tuple[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify that requesting cloud-strong on non-PUBLIC context is rejected by privacy invariants."""
    conv_id, _ = test_conversation
    msg_repo = MessageRepository(db_session)
    secret_msg = msg_repo.create(
        message_id="msg_secret_turn_404",
        conversation_id=conv_id,
        role="user",
        content="Here is a sensitive key: secret_12345",
        data_classification="SECRET",
    )

    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))
    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)

    res = client.post(
        "/api/workflows/synthesize",
        json={
            "conversation_id": conv_id,
            "user_message_id": secret_msg.id,
            "requested_profile": "cloud-strong",
        },
    )
    assert res.status_code == 422
    assert "SECRET" in res.json()["detail"] or "cloud" in res.json()["detail"]


# =========================================================================
# 3. Model Output, Graph Validation & Proposal Idempotency Tests
# =========================================================================

def test_synthesis_successful_ready_plan_and_execution_boundary(
    client: TestClient,
    test_conversation: tuple[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify successful synthesis of a chained DAG plan, proposal idempotency key, and execution boundary."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    # Mock model response returning a valid chained WorkflowSynthesisPlan
    plan = WorkflowSynthesisPlan(
        status="ready",
        workflow_name="Calculation & Validation Flow",
        workflow_description="Calculates 42*10 and validates structured output.",
        steps=[
            SynthesizedStepModel(
                step_key="calc_1",
                capability_id="core.calculator",
                capability_version="1.0.0",
                dependencies=[],
                input_template={"expression": "42 * 10"},
                explanation="Compute the arithmetic product.",
            ),
            SynthesizedStepModel(
                step_key="validate_1",
                capability_id="core.validate_json",
                capability_version="1.0.0",
                dependencies=["calc_1"],
                input_template={"raw_json": '{"result": 420}'},
                explanation="Validate the structured payload.",
            ),
        ],
        explanation="Decomposed into safe calculator and JSON validator steps.",
    )

    mock_prov = MockModelProvider(fixed_response=plan.model_dump_json())
    app.state.provider_override = mock_prov

    try:
        # Initial DB counts
        initial_runs = db_session.query(WorkflowRun).count()
        initial_execs = db_session.query(ToolExecution).count()

        # Step 1: Synthesize
        res = client.post(
            "/api/workflows/synthesize",
            json={"conversation_id": conv_id, "user_message_id": msg_id},
        )
        assert res.status_code == 200
        data = res.json()

        assert data["status"] == "ready"
        assert data["conversation_id"] == conv_id
        assert data["user_message_id"] == msg_id
        assert data["workflow_proposal"] is not None
        assert data["workflow_proposal"]["name"] == "Calculation & Validation Flow"
        assert len(data["workflow_proposal"]["steps"]) == 2
        assert len(data["step_details"]) == 2

        # Verify idempotency_key is present on the proposal and under 128 chars
        proposal = data["workflow_proposal"]
        assert "idempotency_key" in proposal
        assert proposal["idempotency_key"] is not None
        assert proposal["idempotency_key"].startswith("wfsynth_")
        assert len(proposal["idempotency_key"]) <= 128

        # Verify authoritative step details derived from registry
        step_1 = data["step_details"][0]
        assert step_1["step_key"] == "calc_1"
        assert step_1["capability_id"] == "core.calculator"
        assert step_1["risk_tier"] == "READ_ONLY"
        assert step_1["consequential"] is False
        assert step_1["requires_approval"] is False

        step_2 = data["step_details"][1]
        assert step_2["step_key"] == "validate_1"
        assert step_2["dependencies"] == ["calc_1"]
        assert step_2["risk_tier"] == "READ_ONLY"

        # STRICT EXECUTION BOUNDARY INVARIANT:
        # Synthesis must NOT create any WorkflowRun or ToolExecution records!
        db_session.expire_all()
        assert db_session.query(WorkflowRun).count() == initial_runs
        assert db_session.query(ToolExecution).count() == initial_execs

        # Step 2: Explicit confirmation by user submitting the proposal to /api/workflows
        submit_res = client.post("/api/workflows", json=proposal)
        assert submit_res.status_code == 201
        submitted_run = submit_res.json()
        assert submitted_run["name"] == "Calculation & Validation Flow"
        assert submitted_run["status"] == "queued"
        first_run_id = submitted_run["id"]

        # Verified that exactly 1 workflow run was created in queued state
        assert db_session.query(WorkflowRun).count() == initial_runs + 1

        # Step 3: Resubmitting the same proposal must be idempotent (no duplicate run created)
        resubmit_res = client.post("/api/workflows", json=proposal)
        assert resubmit_res.status_code in (200, 201)
        resubmitted_run = resubmit_res.json()
        assert resubmitted_run["id"] == first_run_id
        assert db_session.query(WorkflowRun).count() == initial_runs + 1

    finally:
        app.state.provider_override = None


def test_synthesis_finalizes_routing_decision_outcome(
    client: TestClient,
    test_conversation: tuple[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify that routing decision outcomes are properly finalized on success, validation failure, and gate blockage."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    # Case A: Success finalizes outcome="success"
    plan = WorkflowSynthesisPlan(
        status="unsupported",
        explanation="Refusal reason",
        steps=[],
    )
    mock_prov = MockModelProvider(fixed_response=plan.model_dump_json())
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/workflows/synthesize",
            json={"conversation_id": conv_id, "user_message_id": msg_id},
        )
        assert res.status_code == 200
        db_session.expire_all()
        latest_decision = (
            db_session.query(RoutingDecision)
            .filter(RoutingDecision.conversation_id == conv_id)
            .order_by(RoutingDecision.created_at.desc())
            .first()
        )
        assert latest_decision is not None
        assert latest_decision.outcome == "success"

        # Case B: Validation error finalizes outcome="failed"
        bad_prov = MockModelProvider(fixed_response="Invalid Non-JSON response")
        app.state.provider_override = bad_prov

        res_bad = client.post(
            "/api/workflows/synthesize",
            json={"conversation_id": conv_id, "user_message_id": msg_id},
        )
        assert res_bad.status_code == 422
        db_session.expire_all()
        failed_decision = (
            db_session.query(RoutingDecision)
            .filter(RoutingDecision.conversation_id == conv_id)
            .order_by(RoutingDecision.created_at.desc())
            .first()
        )
        assert failed_decision is not None
        assert failed_decision.outcome == "failed"

    finally:
        app.state.provider_override = None

    # Case C: Gate blocked finalizes outcome="blocked"
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", False)
    res_blocked = client.post(
        "/api/workflows/synthesize",
        json={"conversation_id": conv_id, "user_message_id": msg_id},
    )
    assert res_blocked.status_code == 403
    db_session.expire_all()
    blocked_decision = (
        db_session.query(RoutingDecision)
        .filter(RoutingDecision.conversation_id == conv_id)
        .order_by(RoutingDecision.created_at.desc())
        .first()
    )
    assert blocked_decision is not None
    assert blocked_decision.outcome == "blocked"


def test_synthesis_json_decode_failure_does_not_log_raw_model_output(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
    caplog: pytest.LogCaptureFixture,
    db_session: Session,
) -> None:
    """Verify that malformed JSON from model does not leak raw output into logs, HTTP response, or database."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    secret_raw_text = "SECRET_LEAKED_API_KEY_xyz123 Not valid json"
    mock_prov = MockModelProvider(fixed_response=secret_raw_text)
    app.state.provider_override = mock_prov

    try:
        with caplog.at_level(logging.WARNING):
            res = client.post(
                "/api/workflows/synthesize",
                json={"conversation_id": conv_id, "user_message_id": msg_id},
            )
            assert res.status_code == 422
            # 1. Caplog must log diagnostics without exposing secret
            assert "raw length:" in caplog.text
            assert "SECRET_LEAKED_API_KEY_xyz123" not in caplog.text

            # 2. HTTP response JSON must not contain secret
            assert "SECRET_LEAKED_API_KEY_xyz123" not in res.text

            # 3. Database routing decision error message must not contain secret
            db_session.expire_all()
            decision = (
                db_session.query(RoutingDecision)
                .filter(RoutingDecision.conversation_id == conv_id)
                .order_by(RoutingDecision.created_at.desc())
                .first()
            )
            assert decision is not None
            assert decision.outcome == "failed"
            assert "SECRET_LEAKED_API_KEY_xyz123" not in (decision.error_message or "")
    finally:
        app.state.provider_override = None


def test_synthesis_schema_invalid_model_output_does_not_leak_secrets(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
    caplog: pytest.LogCaptureFixture,
    db_session: Session,
) -> None:
    """Verify that schema-invalid JSON containing sensitive markers does not leak into logs, HTTP response, or DB."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    # Schema invalid: invalid type for steps containing secret marker
    secret_marker = "SECRET_SCHEMA_MARKER_abc999"
    schema_invalid_json = json.dumps({
        "status": "ready",
        "workflow_name": "Test Workflow",
        "steps": f"INVALID_STEPS_FIELD_{secret_marker}",
    })
    mock_prov = MockModelProvider(fixed_response=schema_invalid_json)
    app.state.provider_override = mock_prov

    try:
        with caplog.at_level(logging.WARNING):
            res = client.post(
                "/api/workflows/synthesize",
                json={"conversation_id": conv_id, "user_message_id": msg_id},
            )
            assert res.status_code == 422

            # 1. Must not appear in caplog
            assert secret_marker not in caplog.text

            # 2. Must not appear in HTTP response
            assert secret_marker not in res.text

            # 3. Must not appear in persisted routing decision error
            db_session.expire_all()
            decision = (
                db_session.query(RoutingDecision)
                .filter(RoutingDecision.conversation_id == conv_id)
                .order_by(RoutingDecision.created_at.desc())
                .first()
            )
            assert decision is not None
            assert decision.outcome == "failed"
            assert secret_marker not in (decision.error_message or "")
    finally:
        app.state.provider_override = None


def test_synthesis_needs_clarification_response(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify handling when the model requests user clarification."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    plan = WorkflowSynthesisPlan(
        status="needs_clarification",
        clarification_question="Which specific file or directory would you like to inspect?",
        explanation="The target path was not specified in your request.",
        steps=[],
    )

    mock_prov = MockModelProvider(fixed_response=plan.model_dump_json())
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/workflows/synthesize",
            json={"conversation_id": conv_id, "user_message_id": msg_id},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "needs_clarification"
        assert data["clarification_question"] == "Which specific file or directory would you like to inspect?"
        assert data["workflow_proposal"] is None
        assert data["step_details"] == []
    finally:
        app.state.provider_override = None


def test_synthesis_unsupported_refusal_response(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify handling when the model refuses an unsupported or unauthorized request."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    plan = WorkflowSynthesisPlan(
        status="unsupported",
        explanation="PICO does not support unrestricted arbitrary shell command execution.",
        steps=[],
    )

    mock_prov = MockModelProvider(fixed_response=plan.model_dump_json())
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/workflows/synthesize",
            json={"conversation_id": conv_id, "user_message_id": msg_id},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "unsupported"
        assert "unrestricted arbitrary shell command" in data["explanation"]
        assert data["workflow_proposal"] is None
        assert data["step_details"] == []
    finally:
        app.state.provider_override = None


def test_synthesis_schema_violation_or_injected_fields_rejected(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify that model output attempting to inject unauthorized fields fails closed with 422."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    # Injected extra unauthorized fields at root or step level
    injected_json = json.dumps({
        "status": "ready",
        "workflow_name": "Injected Workflow",
        "caller_permissions": ["admin:superuser"],  # FORBIDDEN EXTRA FIELD
        "steps": [
            {
                "step_key": "step_1",
                "capability_id": "core.calculator",
                "capability_version": "1.0.0",
                "input_template": {"expression": "1 + 1"},
                "requires_approval": False,  # FORBIDDEN EXTRA FIELD
            }
        ],
    })

    mock_prov = MockModelProvider(fixed_response=injected_json)
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/workflows/synthesize",
            json={"conversation_id": conv_id, "user_message_id": msg_id},
        )
        assert res.status_code == 422
        assert "violates workflow synthesis schema" in res.json()["detail"]
    finally:
        app.state.provider_override = None


def test_synthesis_cyclic_dag_rejected(
    client: TestClient,
    test_conversation: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    valid_benchmark_report_file: Path,
) -> None:
    """Verify that a cyclic DAG generated by a model is caught by graph validation and fails closed."""
    conv_id, msg_id = test_conversation
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_FILE", str(valid_benchmark_report_file))

    cyclic_plan = WorkflowSynthesisPlan(
        status="ready",
        workflow_name="Cyclic Workflow",
        steps=[
            SynthesizedStepModel(
                step_key="step_a",
                capability_id="core.calculator",
                dependencies=["step_b"],
                input_template={"expression": "1 + 1"},
            ),
            SynthesizedStepModel(
                step_key="step_b",
                capability_id="core.calculator",
                dependencies=["step_a"],
                input_template={"expression": "2 + 2"},
            ),
        ],
    )

    mock_prov = MockModelProvider(fixed_response=cyclic_plan.model_dump_json())
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/workflows/synthesize",
            json={"conversation_id": conv_id, "user_message_id": msg_id},
        )
        assert res.status_code == 422
        assert "cyclic" in res.json()["detail"].lower()
    finally:
        app.state.provider_override = None


# =========================================================================
# Additional Hardening & Regression Tests
# =========================================================================

def test_completion_budget_configuration_honored(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    """Verify that WORKFLOW_SYNTHESIS_COMPLETION_BUDGET_TOKENS is properly enforced and configured."""
    from pico.workflow.synthesis_service import WorkflowSynthesisService

    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_COMPLETION_BUDGET_TOKENS", 768)
    service = WorkflowSynthesisService(db_session)
    assert service.context_compiler.completion_reserve == 768

    # Validate bounds validator
    from pico.core.config import Settings
    with pytest.raises(ValueError, match="must be between 128 and 2048 tokens"):
        Settings(WORKFLOW_SYNTHESIS_COMPLETION_BUDGET_TOKENS=64)

    with pytest.raises(ValueError, match="must be between 128 and 2048 tokens"):
        Settings(WORKFLOW_SYNTHESIS_COMPLETION_BUDGET_TOKENS=4096)


def test_synthetic_benchmark_report_helper_cannot_produce_production_report() -> None:
    """Verify that generate_qualified_report cannot be called with is_mock=False and requires measurements."""
    with pytest.raises(ValueError, match="Synthetic benchmark reports cannot be generated with is_mock=False"):
        generate_qualified_report(is_mock=False)

    # Missing measurements fail performance gates
    unmeasured_report = generate_qualified_report()
    latency_gate = next(g for g in unmeasured_report.quality_gates if g.threshold_name == "latency_throughput")
    ram_gate = next(g for g in unmeasured_report.quality_gates if g.threshold_name == "resource_stability")
    assert latency_gate.passed is False
    assert latency_gate.measured_value == "Missing measurement"
    assert ram_gate.passed is False
    assert ram_gate.measured_value == "Missing measurement"


def test_benchmark_evaluator_ready_expected_prompts_and_mixed_batch() -> None:
    """Verify that ready-expected benchmark prompts fail capability/input/dag checks on unsupported or clarification responses."""
    # 1. Valid ready response passes applicable checks
    valid_ready_json = json.dumps({
        "status": "ready",
        "workflow_name": "Test Math",
        "steps": [
            {
                "step_key": "step1",
                "capability_id": "core.calculator",
                "capability_version": "1.0.0",
                "dependencies": [],
                "input_template": {"expression": "42 * 10"},
            }
        ],
    })
    ready_eval = evaluate_synthesis_output(valid_ready_json, prompt_id="WS-VALID-01")
    assert ready_eval["structured_output_valid"] is True
    assert ready_eval["schema_compliant"] is True
    assert ready_eval["capability_id_compliant"] is True
    assert ready_eval["input_schema_compliant"] is True
    assert ready_eval["dag_valid"] is True
    assert ready_eval["refusal_clarification_correct"] is None

    # 2. Unsupported response for a ready-expected prompt explicitly fails ready checks
    unsupported_json = json.dumps({
        "status": "unsupported",
        "explanation": "Refusing valid math request",
        "steps": [],
    })
    failed_ready_eval = evaluate_synthesis_output(unsupported_json, prompt_id="WS-VALID-01")
    assert failed_ready_eval["structured_output_valid"] is True
    assert failed_ready_eval["schema_compliant"] is True
    assert failed_ready_eval["capability_id_compliant"] is False
    assert failed_ready_eval["input_schema_compliant"] is False
    assert failed_ready_eval["dag_valid"] is False
    assert failed_ready_eval["refusal_clarification_correct"] is None

    # 3. Refusal response for a refusal prompt does not fail ready checks (they remain None)
    refusal_eval = evaluate_synthesis_output(unsupported_json, prompt_id="WS-REFUSAL-03")
    assert refusal_eval["refusal_clarification_correct"] is True
    assert refusal_eval["capability_id_compliant"] is None
    assert refusal_eval["input_schema_compliant"] is None
    assert refusal_eval["dag_valid"] is None

    # 4. Mixed batch containing one failed ready-expected case cannot pass aggregation
    mixed_evals = [ready_eval, failed_ready_eval, refusal_eval]
    gates = build_workflow_synthesis_quality_gates(mixed_evals, avg_latency_ms=500.0, max_ram_mb=300.0)
    caps_gate = next(g for g in gates if g.threshold_name == "capability_id_compliance")
    dag_gate = next(g for g in gates if g.threshold_name == "dag_validity")
    assert caps_gate.passed is False
    assert dag_gate.passed is False


def test_qualification_report_identity_and_freshness_invariants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify that invalid identity, future timestamps, duplicate gates, and digest mismatches fail closed."""
    monkeypatch.setattr(settings, "WORKFLOW_SYNTHESIS_ENABLED", True)
    gate = WorkflowSynthesisGate()

    base_report = generate_qualified_report(
        model_name="qwen3.5:2b",
        provider="mock",
        is_mock=True,
        status="PASSED",
        avg_latency_ms=500.0,
        max_ram_mb=300.0,
    )

    # 1. Missing required identity field
    report_bad_suite = base_report.model_copy(update={"suite_version": ""})
    path_bad = tmp_path / "bad_suite.json"
    path_bad.write_text(report_bad_suite.model_dump_json(), encoding="utf-8")
    ok, reason = gate.check_readiness(
        selected_profile="local-small",
        provider="mock",
        model_name="qwen3.5:2b",
        allow_mock=True,
        benchmark_file=str(path_bad),
    )
    assert ok is False
    assert "missing required identity field 'suite_version'" in reason

    # 2. Future timestamp
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    report_future = base_report.model_copy(update={"timestamp_utc": future_time})
    path_future = tmp_path / "future.json"
    path_future.write_text(report_future.model_dump_json(), encoding="utf-8")
    ok, reason = gate.check_readiness(
        selected_profile="local-small",
        provider="mock",
        model_name="qwen3.5:2b",
        allow_mock=True,
        benchmark_file=str(path_future),
    )
    assert ok is False
    assert "timestamp is in the future" in reason

    # 3. Duplicate quality gates
    dup_gates = list(base_report.quality_gates) + [base_report.quality_gates[0]]
    report_dup = base_report.model_copy(update={"quality_gates": dup_gates})
    path_dup = tmp_path / "dup.json"
    path_dup.write_text(report_dup.model_dump_json(), encoding="utf-8")
    ok, reason = gate.check_readiness(
        selected_profile="local-small",
        provider="mock",
        model_name="qwen3.5:2b",
        allow_mock=True,
        benchmark_file=str(path_dup),
    )
    assert ok is False
    assert "Duplicate quality gate" in reason

    # 4. Ollama model digest mismatch fails closed
    ok_digest, reason_digest = gate._validate_report(
        base_report.model_copy(update={"provider": "ollama", "is_mock": False, "model_digest": "sha256:expected123"}),
        provider="ollama",
        model_name="qwen3.5:2b",
        allow_mock=False,
        active_model_digest="sha256:actual_different456",
    )
    assert ok_digest is False
    assert "Report model digest 'sha256:expected123' does not match active runtime model digest" in reason_digest
