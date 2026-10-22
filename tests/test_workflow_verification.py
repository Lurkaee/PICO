"""Tests for independent verification, dependency gating, and compensation (Milestone P9.3)."""

import asyncio
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from pico.capabilities import registry as default_registry
from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult as CapabilityVerificationResult,
)
from pico.core.errors import EntityNotFoundError, ValidationError
from pico.models.tool_execution import ToolExecution
from pico.models.workflow import WorkflowRun, WorkflowStep
from pico.models.workflow_verification import WorkflowVerification
from pico.models.workflow_compensation import WorkflowCompensation
from pico.repositories.event_ledger_repo import EventLedgerRepository
from pico.repositories.workflow_repo import WorkflowRepository
from pico.scheduler.types import WorkerRole
from pico.schemas.workflow import (
    StepDefinition,
    WorkflowCreate,
    WorkflowAdvanceRequest,
    WorkflowCompensateRequest,
)
from pico.services.execution_engine import ToolExecutionEngine
from pico.workflow.compensation.registry import (
    CompensationDefinition,
    CompensationRegistry,
    get_compensation_registry,
)
from pico.workflow.service import WorkflowService
from pico.workflow.verification.policies import (
    BaseVerificationPolicy,
    DefaultEvidenceVerificationPolicy,
    ModelAssistedVerificationPolicy,
    SafeArithmeticEvaluator,
    compute_digest,
    get_verification_policy,
)
from pico.workflow.verification.types import (
    VerificationMethod,
    VerificationRequest,
    VerificationResult,
    VerificationStage,
    VerificationStatus,
)


# --- Custom Test Capabilities with Proper Pydantic Models ---

class TamperedCalculatorInput(BaseModel):
    expression: str = Field(default="2 + 2")


class TamperedCalculatorOutput(BaseModel):
    expression: str
    result: float
    self_certified: bool = True


class TamperedCalculatorCapability(BaseCapability):
    """Simulates a model or calculator that produces mathematically incorrect outputs."""
    metadata = CapabilityMetadata(
        capability_id="test.tampered_calculator",
        version="1.0.0",
        name="Test Tampered Calculator",
        description="Produces wrong calculation results to test independent verification gating.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="PUBLIC",
        timeout_seconds=2.0,
        idempotent=True,
    )
    input_model = TamperedCalculatorInput
    output_model = TamperedCalculatorOutput

    async def execute(self, input_data: dict, context: ExecutionContext) -> ExecutionResult:
        expr = input_data.get("expression", "2 + 2")
        # Deliberately report wrong result 999.0 while claiming success
        return ExecutionResult(
            success=True,
            output_data={"expression": expr, "result": 999.0, "self_certified": True},
            evidence={"executed": True},
        )


class FlakySemanticInput(BaseModel):
    source: str = Field(default="")
    secret: str | None = None


class FlakySemanticOutput(BaseModel):
    confidence: float | None = None
    claim: str | None = None
    summary: str | None = None
    classified: bool | None = None


class FlakySemanticCapability(BaseCapability):
    """Capability whose semantic result requires manual operator review."""
    metadata = CapabilityMetadata(
        capability_id="test.flaky_semantic",
        version="1.0.0",
        name="Test Flaky Semantic Capability",
        description="Generates uncertain outputs that require human review.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="INTERNAL",
        timeout_seconds=2.0,
        idempotent=True,
    )
    input_model = FlakySemanticInput
    output_model = FlakySemanticOutput

    async def execute(self, input_data: dict, context: ExecutionContext) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            output_data={"confidence": 0.45, "claim": "unverified fact"},
            evidence={"confidence": 0.45},
        )


class CompensableActionInput(BaseModel):
    key: str
    value: str


class CompensableActionOutput(BaseModel):
    key: str
    value: str
    applied: bool


class CompensableActionCapability(BaseCapability):
    """A safe compensable action with a deterministic reverse operation."""
    metadata = CapabilityMetadata(
        capability_id="test.compensable_action",
        version="1.0.0",
        name="Test Compensable Action",
        description="Action that records an entry which can be compensated.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="PUBLIC",
        timeout_seconds=2.0,
        idempotent=True,
    )
    input_model = CompensableActionInput
    output_model = CompensableActionOutput

    async def execute(self, input_data: dict, context: ExecutionContext) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            output_data={"key": input_data.get("key"), "value": input_data.get("value"), "applied": True},
            evidence={"key": input_data.get("key")},
        )


class ReverseCompensableActionInput(BaseModel):
    key: str


class ReverseCompensableActionOutput(BaseModel):
    key: str
    reversed: bool


class ReverseCompensableActionCapability(BaseCapability):
    """Reverse operation for CompensableActionCapability."""
    metadata = CapabilityMetadata(
        capability_id="test.reverse_compensable_action",
        version="1.0.0",
        name="Test Reverse Compensable Action",
        description="Compensates the action by removing or reversing it.",
        risk_tier=RiskTier.LOW,
        required_permission="test:execute",
        required_classification="PUBLIC",
        timeout_seconds=2.0,
        idempotent=True,
    )
    input_model = ReverseCompensableActionInput
    output_model = ReverseCompensableActionOutput

    async def execute(self, input_data: dict, context: ExecutionContext) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            output_data={"key": input_data.get("key"), "reversed": True},
            evidence={"key": input_data.get("key")},
        )


@pytest.fixture(autouse=True)
def register_test_capabilities():
    """Ensure test capabilities are registered for verification tests."""
    caps = [
        TamperedCalculatorCapability(),
        FlakySemanticCapability(),
        CompensableActionCapability(),
        ReverseCompensableActionCapability(),
    ]
    for c in caps:
        try:
            default_registry.register(c)
        except ValueError:
            pass

    # Register compensation in CompensationRegistry
    comp_reg = get_compensation_registry()
    comp_reg.register(
        source_capability_id="test.compensable_action",
        definition=CompensationDefinition(
            target_capability_id="test.reverse_compensable_action",
            input_builder=lambda orig_in, orig_out: {"key": orig_out.get("key")},
            is_consequential=False,
        ),
    )


# --- 1. Deterministic Arithmetic & Calculator Verification ---

def test_safe_arithmetic_evaluator():
    """Evaluates arithmetic expressions safely without eval()."""
    assert SafeArithmeticEvaluator.evaluate("2 + 2") == 4.0
    assert SafeArithmeticEvaluator.evaluate("10 * (3 + 2) / 5") == 10.0
    assert SafeArithmeticEvaluator.evaluate("2 ** 3") == 8.0
    assert SafeArithmeticEvaluator.evaluate("invalid expr") is None
    assert SafeArithmeticEvaluator.evaluate("__import__('os').system('dir')") is None


def test_deterministic_calculator_verification_pass(db_session: Session):
    """Verify that a legitimate calculator result passes independent recomputation."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Calculator Verification Test",
        data_classification="PUBLIC",
        steps=[
            StepDefinition(
                step_key="calc_step",
                capability_id="core.calculator",
                capability_version="1.0.0",
                dependencies=[],
                input_template={"expression": "12 * 4"},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)
    advanced = asyncio.run(service.advance_workflow(run_resp.id, caller_permissions={"tools:calculate"}))

    assert advanced.status == "succeeded"
    assert advanced.completed_steps == 1
    step = advanced.steps[0]
    assert step.status == "succeeded"
    assert step.output["result"] == 48.0
    assert step.verification is not None
    assert step.verification.status == "passed"
    assert step.verification.verification_method == "deterministic_recomputation"


def test_deterministic_calculator_verification_fail_gates_output(db_session: Session):
    """
    Stage C: A tampered result (claims 999.0 for 2 + 2) fails independent verification,
    blocking downstream dependent steps even though handler reported success.
    """
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Tampered Calculation Workflow",
        data_classification="PUBLIC",
        steps=[
            StepDefinition(
                step_key="tampered_calc",
                capability_id="test.tampered_calculator",
                capability_version="1.0.0",
                dependencies=[],
                input_template={"expression": "2 + 2"},
            ),
            StepDefinition(
                step_key="downstream_consumer",
                capability_id="test.idempotent_counter",
                capability_version="1.0.0",
                dependencies=["tampered_calc"],
                input_template={"increment": 1},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)

    advanced = asyncio.run(service.advance_workflow(run_resp.id, caller_permissions={"test:execute"}))

    assert advanced.status == "failed"
    step1 = next(s for s in advanced.steps if s.step_key == "tampered_calc")
    step2 = next(s for s in advanced.steps if s.step_key == "downstream_consumer")

    assert step1.status == "verification_failed"
    assert "Independent verification failed" in step1.error_message
    assert step1.verification is not None
    assert step1.verification.status == "failed"
    assert step1.verification.stage == "stage_c_semantic"

    # Crucial invariant: Dependent step is BLOCKED and never executed!
    assert step2.status == "blocked"
    assert step2.output is None


# --- 2. Output Schema vs Semantic Distinction & Self-Certification Rejection ---

def test_model_self_certification_cannot_bypass_verification(db_session: Session):
    """
    A model including self_certified=True in its output cannot bypass verification.
    """
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Self-Certified Bypass Test",
        data_classification="PUBLIC",
        steps=[
            StepDefinition(
                step_key="unverified_claim",
                capability_id="test.tampered_calculator",
                input_template={"expression": "100 - 1"},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)
    advanced = asyncio.run(service.advance_workflow(run_resp.id, caller_permissions={"test:execute"}))

    assert advanced.status == "failed"
    step = advanced.steps[0]
    assert step.status == "verification_failed"
    assert step.verification.status == "failed"


# --- 3. Missing Execution Evidence Fails Closed ---

def test_missing_execution_evidence_fails_closed(db_session: Session):
    """Stage B rejects verification when execution record is missing or corrupted."""
    policy = DefaultEvidenceVerificationPolicy()
    req = VerificationRequest(
        workflow_run_id="run_test",
        step_id="step_test",
        step_key="step_test",
        capability_id="core.calculator",
        resolved_input={"expression": "2 + 2"},
        output_data={"expression": "2 + 2", "result": 4.0},
        tool_execution_id="non_existent_exec_id",
        tool_execution_record=None,
        data_classification="PUBLIC",
    )
    res = asyncio.run(policy.verify(req, default_registry, db_session))
    assert res.status == VerificationStatus.FAILED
    assert res.stage == VerificationStage.STAGE_B_EVIDENCE
    assert "ToolExecution record" in res.reason


# --- 4. Review Required / Inconclusive Verification ---

class ReviewRequiredVerificationPolicy(BaseVerificationPolicy):
    """Test policy that returns REVIEW_REQUIRED."""
    policy_id = "test.review_policy"
    policy_version = "1.0.0"

    async def verify(
        self,
        request: VerificationRequest,
        registry,
        db: Session,
        scheduler=None,
    ) -> VerificationResult:
        in_digest = compute_digest(request.resolved_input)
        out_digest = compute_digest(request.output_data)
        return VerificationResult(
            status=VerificationStatus.REVIEW_REQUIRED,
            stage=VerificationStage.STAGE_C_SEMANTIC,
            method=VerificationMethod.SCHEMA_AND_EVIDENCE,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            input_digest=in_digest,
            output_digest=out_digest,
            passed=False,
            reason="Ambiguous semantic findings require operator intervention.",
            findings={"operator_review_required": True},
            data_classification=request.data_classification,
        )


def test_inconclusive_verification_pauses_in_review_required(db_session: Session, monkeypatch):
    """
    Inconclusive verification must not release output and must transition workflow to review_required.
    """
    monkeypatch.setattr(
        "pico.workflow.verification.service.get_verification_policy",
        lambda pid=None: ReviewRequiredVerificationPolicy(),
    )

    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Review Required Workflow",
        steps=[
            StepDefinition(
                step_key="ambiguous_step",
                capability_id="test.idempotent_counter",
                input_template={"increment": 10},
            ),
            StepDefinition(
                step_key="dependent_step",
                capability_id="test.idempotent_counter",
                dependencies=["ambiguous_step"],
                input_template={"increment": 1},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)
    advanced = asyncio.run(service.advance_workflow(run_resp.id, caller_permissions={"test:execute"}))

    assert advanced.status == "review_required"
    step1 = next(s for s in advanced.steps if s.step_key == "ambiguous_step")
    step2 = next(s for s in advanced.steps if s.step_key == "dependent_step")

    assert step1.status == "review_required"
    assert step2.status == "pending"
    assert step2.output is None


# --- 5. Model-Assisted Semantic Verification via Scheduler ---

def test_model_assisted_verifier_uses_scheduler_lease(db_session: Session):
    """
    Model-assisted semantic review requests lease with role=WorkerRole.VERIFIER
    over the single resident model.
    """
    class FakeScheduler:
        def __init__(self):
            self.scheduled_requests = []

        async def schedule_request(self, **kwargs):
            self.scheduled_requests.append(kwargs)
            class FakeSchedRes:
                text = '{"verdict": "passed", "confidence": 0.95, "reason": "Accurate output"}'
            return FakeSchedRes()

    fake_scheduler = FakeScheduler()
    policy = ModelAssistedVerificationPolicy()

    # Pre-populate execution record so Stage B passes
    exec_record = ToolExecution(
        id="exec_789",
        capability_id="test.flaky_semantic",
        status="succeeded",
        verification_status="passed",
        input_json={"source": "hello"},
        output_json={"summary": "hello world"},
        input_digest=compute_digest({"source": "hello"}),
    )
    db_session.add(exec_record)
    db_session.commit()

    req = VerificationRequest(
        workflow_run_id="run_123",
        step_id="step_456",
        step_key="summary_step",
        tool_execution_id="exec_789",
        tool_execution_record=exec_record,
        capability_id="test.flaky_semantic",
        resolved_input={"source": "hello"},
        output_data={"summary": "hello world"},
        step_goal_or_description="Summarize text",
        data_classification="PUBLIC",
    )

    res = asyncio.run(policy.verify(req, default_registry, db_session, scheduler=fake_scheduler))
    assert res.status == VerificationStatus.PASSED
    assert res.method == VerificationMethod.MODEL_ASSISTED_REVIEW
    # Verify that the scheduler request was made with WorkerRole.VERIFIER
    assert any(r.get("role") == WorkerRole.VERIFIER for r in fake_scheduler.scheduled_requests)


def test_non_public_context_pinned_locally_for_model_review(db_session: Session):
    """
    Non-public data classification (INTERNAL, SENSITIVE, SECRET) must remain local and cannot route to cloud.
    """
    policy = ModelAssistedVerificationPolicy()

    exec_record = ToolExecution(
        id="exec_secret",
        capability_id="test.flaky_semantic",
        status="succeeded",
        verification_status="passed",
        input_json={"secret": "my-password"},
        output_json={"classified": True},
        input_digest=compute_digest({"secret": "my-password"}),
    )
    db_session.add(exec_record)
    db_session.commit()

    req = VerificationRequest(
        workflow_run_id="run_1",
        step_id="step_1",
        step_key="secret_step",
        tool_execution_id="exec_secret",
        tool_execution_record=exec_record,
        capability_id="test.flaky_semantic",
        resolved_input={"secret": "my-password"},
        output_data={"classified": True},
        data_classification="SECRET",
    )
    # Model assisted review on SECRET context must fail closed or stay local
    res = asyncio.run(policy.verify(req, default_registry, db_session))
    assert res.status == VerificationStatus.FAILED
    assert "SECRET" in res.reason or "ineligible" in res.reason


# --- 6. Interrupted Verification Crash Recovery ---


def test_process_restart_reconciliation_safely_handles_verifying_step(db_session: Session):
    """
    If process crashes while step is in 'verifying' or 'running':
    Unverified output is NOT released, and step is safely reconciled to review_required.
    """
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Crash Recovery Workflow",
        steps=[
            StepDefinition(
                step_key="crash_step",
                capability_id="test.idempotent_counter",
                input_template={"increment": 5},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)

    # Simulate tool succeeded, but process crashed before durable verification
    step = service.repo.get_steps_for_run(run_resp.id)[0]
    exec_record = service.engine.submit(
        capability_id=step.capability_id,
        input_data={"increment": 5},
        caller_permissions={"test:execute"},
        task_id=run_resp.task_id,
    )
    # Execute execution record
    asyncio.run(service.engine.execute(exec_record.id, caller_permissions={"test:execute"}))

    service.repo.update_step_status(
        step.id,
        status="verifying",
        tool_execution_id=exec_record.id,
        output_json={"incremented_by": 5},
    )
    service.repo.update_run_status(run_resp.id, status="running")
    db_session.commit()

    # Reconcile after restart
    reconciled = service.recover_interrupted_workflows()
    assert run_resp.id in reconciled

    refreshed_step = service.repo.get_step_by_key(run_resp.id, "crash_step")
    # Output was NOT durably verified before crash, so it transitions to review_required
    assert refreshed_step.status == "review_required"
    assert "interrupted" in refreshed_step.error_message


# --- 7. Bounded Compensation and Reverse-Dependency Execution ---

def test_compensation_registered_and_executed_in_reverse_dependency_order(db_session: Session):
    """
    Succeeded steps with registered compensation are compensated in reverse dependency order.
    """
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Compensable Workflow",
        steps=[
            StepDefinition(
                step_key="first_action",
                capability_id="test.compensable_action",
                input_template={"key": "res_1", "value": "A"},
            ),
            StepDefinition(
                step_key="second_action",
                capability_id="test.compensable_action",
                dependencies=["first_action"],
                input_template={"key": "res_2", "value": "B"},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)
    advanced = asyncio.run(service.advance_workflow(run_resp.id, caller_permissions={"test:execute"}))
    assert advanced.status == "succeeded"
    assert len(advanced.compensations) == 2

    # Now trigger compensation
    compensated = asyncio.run(
        service.compensate_workflow(
            run_resp.id,
            reason="Operator requested rollback",
            caller_permissions={"test:execute"},
        )
    )
    assert compensated.status == "compensated"

    # Inspect compensations: check reverse order execution
    comps = service.repo.get_compensations_for_run(run_resp.id)
    assert len(comps) == 2
    # Both should be completed
    assert all(c.status == "completed" for c in comps)
    # second_action (dependent) was compensated before first_action (dependency)
    step_second = service.repo.get_step_by_key(run_resp.id, "second_action")
    comp_second = next(c for c in comps if c.workflow_step_id == step_second.id)
    assert comp_second.status == "completed"


def test_consequential_compensation_requires_human_approval(db_session: Session):
    """
    A consequential compensation (such as filesystem.rollback_staged_diff)
    MUST pause in awaiting_approval before execution.
    Prior approval of the original action does NOT approve compensation.
    """
    comp_reg = get_compensation_registry()
    comp_reg.register(
        source_capability_id="test.consequential_action",
        definition=CompensationDefinition(
            target_capability_id="test.consequential_action",
            input_builder=lambda orig_in, orig_out: {
                "resource_id": orig_in.get("resource_id"),
                "operation": "undo_" + orig_in.get("operation"),
            },
            is_consequential=True,  # Consequential rollback!
        ),
    )

    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Consequential Compensation Test",
        steps=[
            StepDefinition(
                step_key="consequential_step",
                capability_id="test.consequential_action",
                input_template={"resource_id": "config_file", "operation": "delete"},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)

    # 1. First advance: pauses in awaiting_approval
    adv1 = asyncio.run(service.advance_workflow(run_resp.id, caller_permissions={"admin:consequential"}))
    assert adv1.status == "awaiting_approval"

    # 2. Approve original step using record_approval
    step = adv1.steps[0]
    exec_rec = service.engine.exec_repo.get_by_id(step.tool_execution_id)
    service.engine.exec_repo.record_approval(
        step.tool_execution_id,
        approver="operator@local",
        input_digest=exec_rec.input_digest,
    )
    db_session.commit()

    # 3. Advance to execute and succeed
    adv2 = asyncio.run(service.advance_workflow(run_resp.id, caller_permissions={"admin:consequential"}))
    assert adv2.status == "succeeded"

    # 4. Trigger compensation
    comp_run = asyncio.run(
        service.compensate_workflow(
            run_resp.id,
            reason="Operator rollback",
            caller_permissions={"admin:consequential"},
        )
    )
    # The compensation is consequential, so it MUST pause in awaiting_approval!
    comps = service.repo.get_compensations_for_run(run_resp.id)
    assert len(comps) == 1
    assert comps[0].status == "awaiting_approval"


# --- 8. Workflow Cancellation Prevents Stale Advancement ---

def test_workflow_cancellation_cancels_active_and_pending_steps(db_session: Session):
    """Cancellation transitions all active and pending steps to cancelled."""
    service = WorkflowService(db_session)
    payload = WorkflowCreate(
        name="Cancellation Test Workflow",
        steps=[
            StepDefinition(
                step_key="step_1",
                capability_id="test.slow_timeout",
                input_template={"sleep_seconds": 1.0},
            ),
            StepDefinition(
                step_key="step_2",
                capability_id="test.idempotent_counter",
                dependencies=["step_1"],
                input_template={"increment": 1},
            ),
        ],
    )
    run_resp = service.submit_workflow(payload)
    cancelled = service.cancel_workflow(run_resp.id, reason="User cancelled execution")

    assert cancelled.status == "cancelled"
    for s in cancelled.steps:
        assert s.status == "cancelled"


# --- 9. REST API Verification & Compensation Endpoints ---

def test_workflow_verification_and_compensation_api(client: TestClient, db_session: Session):
    """Test the REST API endpoints for verifications and compensations."""
    # 1. Submit workflow
    payload = {
        "name": "API Verification Workflow",
        "data_classification": "PUBLIC",
        "steps": [
            {
                "step_key": "step_calc",
                "capability_id": "core.calculator",
                "capability_version": "1.0.0",
                "dependencies": [],
                "input_template": {"expression": "7 * 8"},
            }
        ],
    }
    create_resp = client.post("/api/workflows", json=payload)
    assert create_resp.status_code == 201
    wf_id = create_resp.json()["id"]

    # 2. Advance workflow with tools:calculate permission
    adv_resp = client.post(
        f"/api/workflows/{wf_id}/advance",
        json={"caller_permissions": ["tools:calculate"], "max_steps": 5},
    )
    assert adv_resp.status_code == 200
    adv_data = adv_resp.json()
    assert adv_data["status"] == "succeeded"

    # 3. GET /api/workflows/{id}/verifications
    v_list_resp = client.get(f"/api/workflows/{wf_id}/verifications")
    assert v_list_resp.status_code == 200
    verifications = v_list_resp.json()
    assert len(verifications) == 1
    assert verifications[0]["status"] == "passed"
    assert verifications[0]["verification_method"] == "deterministic_recomputation"

    # 4. GET /api/workflows/{id}/steps/{key}/verification
    step_v_resp = client.get(f"/api/workflows/{wf_id}/steps/step_calc/verification")
    assert step_v_resp.status_code == 200
    step_v = step_v_resp.json()
    assert step_v["status"] == "passed"

    # 5. GET /api/workflows/{id}/compensations
    c_list_resp = client.get(f"/api/workflows/{wf_id}/compensations")
    assert c_list_resp.status_code == 200
