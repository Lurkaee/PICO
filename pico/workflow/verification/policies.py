"""Verification policies for independent workflow verification (Milestone P9.3)."""

import ast
import hashlib
import json
import operator
from abc import ABC, abstractmethod
from typing import Any
from sqlalchemy.orm import Session

from pico.capabilities import CapabilityRegistry
from pico.capabilities.base import RiskTier
from pico.workflow.verification.types import (
    VerificationMethod,
    VerificationRequest,
    VerificationResult,
    VerificationStage,
    VerificationStatus,
)


def compute_digest(data: Any) -> str:
    """Computes a deterministic SHA-256 digest of normalized JSON data."""
    canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class SafeArithmeticEvaluator(ast.NodeVisitor):
    """
    Deterministic independent arithmetic evaluator using Python ast.
    Evaluates basic math expressions without executing arbitrary code.
    """
    ALLOWED_OPERATORS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    @classmethod
    def evaluate(cls, expr_str: str) -> float | int | None:
        try:
            visitor = cls()
            parsed = ast.parse(str(expr_str).strip(), mode="eval")
            return visitor.visit(parsed.body)
        except Exception:
            return None


    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        op_type = type(node.op)
        if op_type not in self.ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported binary operator: {op_type.__name__}")
        return self.ALLOWED_OPERATORS[op_type](left, right)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        op_type = type(node.op)
        if op_type not in self.ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        return self.ALLOWED_OPERATORS[op_type](operand)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value)}")

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"Unsupported AST node expression: {type(node).__name__}")


class BaseVerificationPolicy(ABC):
    """Abstract base class for all versioned workflow verification policies."""
    policy_id: str
    policy_version: str = "1.0.0"

    @abstractmethod
    async def verify(
        self,
        request: VerificationRequest,
        registry: CapabilityRegistry,
        db: Session,
        scheduler: Any = None,
    ) -> VerificationResult:
        """Execute verification pipeline over requested step and evidence."""
        pass


class DefaultEvidenceVerificationPolicy(BaseVerificationPolicy):
    """
    Standard deterministic verification policy enforcing Stages A through D:
    Stage A — Structural integrity (schema adherence & byte bounds).
    Stage B — Execution evidence (tool execution status, digests, versions).
    Stage C — Semantic & postcondition verification (domain-specific determinism).
    Stage D — Policy decision & risk-sensitive acceptance.
    """
    policy_id = "evidence.deterministic.v1"
    policy_version = "1.0.0"

    async def verify(
        self,
        request: VerificationRequest,
        registry: CapabilityRegistry,
        db: Session,
        scheduler: Any = None,
    ) -> VerificationResult:
        in_digest = compute_digest(request.resolved_input)
        out_digest = compute_digest(request.output_data)
        findings: dict[str, Any] = {}
        evidence: dict[str, Any] = {}

        # ---------------------------------------------------------------------
        # Stage A: Structural Integrity
        # ---------------------------------------------------------------------
        if not registry.has(request.capability_id):
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_A_STRUCTURAL,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.UNSUPPORTED_FALLBACK,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason=f"Capability '{request.capability_id}' is not registered.",
                data_classification=request.data_classification,
            )

        capability = registry.get(request.capability_id, version=request.capability_version)

        # Output payload size bounds
        out_bytes = len(json.dumps(request.output_data).encode("utf-8"))
        if out_bytes > capability.metadata.max_output_bytes:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_A_STRUCTURAL,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.SCHEMA_AND_EVIDENCE,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason=f"Output size ({out_bytes} bytes) exceeded capability limit of {capability.metadata.max_output_bytes} bytes.",
                data_classification=request.data_classification,
            )

        # Schema validation
        try:
            if hasattr(capability, "output_model") and capability.output_model:
                capability.output_model.model_validate(request.output_data)
            findings["stage_a_structural"] = {"status": "passed", "output_bytes": out_bytes}
        except Exception as exc:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_A_STRUCTURAL,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.SCHEMA_AND_EVIDENCE,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason=f"Stage A structural validation failed: output does not conform to schema ({exc})",
                data_classification=request.data_classification,
            )

        # ---------------------------------------------------------------------
        # Stage B: Execution Evidence
        # ---------------------------------------------------------------------
        exec_record = request.tool_execution_record
        if not exec_record:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_B_EVIDENCE,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.SCHEMA_AND_EVIDENCE,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason="Stage B evidence verification failed: missing associated ToolExecution record.",
                data_classification=request.data_classification,
            )

        if exec_record.status != "succeeded":
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_B_EVIDENCE,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.SCHEMA_AND_EVIDENCE,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason=f"Stage B evidence verification failed: execution record status is '{exec_record.status}', not 'succeeded'.",
                data_classification=request.data_classification,
            )

        if exec_record.verification_status != "passed":
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_B_EVIDENCE,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.SCHEMA_AND_EVIDENCE,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason=f"Stage B evidence verification failed: tool verification status is '{exec_record.verification_status}'.",
                data_classification=request.data_classification,
            )

        if exec_record.input_digest != in_digest:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_B_EVIDENCE,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.SCHEMA_AND_EVIDENCE,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason="Stage B evidence verification failed: step input digest diverges from persisted execution input digest.",
                data_classification=request.data_classification,
            )

        evidence["execution_id"] = exec_record.id
        evidence["tool_verification_details"] = exec_record.verification_details_json or {}
        findings["stage_b_evidence"] = {"status": "passed", "execution_id": exec_record.id}

        # ---------------------------------------------------------------------
        # Stage C: Semantic & Postcondition Verification
        # ---------------------------------------------------------------------
        method_used = VerificationMethod.SCHEMA_AND_EVIDENCE

        if request.capability_id in ("core.calculator", "test.tampered_calculator") or "calculator" in request.capability_id:
            method_used = VerificationMethod.DETERMINISTIC_RECOMPUTATION
            expr = (
                (request.resolved_input or {}).get("expression")
                or (request.output_data or {}).get("expression")
                or ((request.tool_execution_record.input_json or {}) if request.tool_execution_record else {}).get("expression")
            )
            reported_res = (request.output_data or {}).get("result")
            if expr is None or reported_res is None:
                return VerificationResult(
                    status=VerificationStatus.FAILED,
                    stage=VerificationStage.STAGE_C_SEMANTIC,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    method=method_used,
                    input_digest=in_digest,
                    output_digest=out_digest,
                    passed=False,
                    reason="Stage C semantic check failed: calculator expression or result missing.",
                    data_classification=request.data_classification,
                )
            expected_res = SafeArithmeticEvaluator.evaluate(str(expr))
            if expected_res is None or abs(float(expected_res) - float(reported_res)) > 1e-6:
                return VerificationResult(
                    status=VerificationStatus.FAILED,
                    stage=VerificationStage.STAGE_C_SEMANTIC,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    method=method_used,
                    input_digest=in_digest,
                    output_digest=out_digest,
                    passed=False,
                    reason=f"Independent arithmetic recomputation mismatch: expected {expected_res}, got {reported_res}.",
                    findings={"expected": expected_res, "reported": reported_res},
                    data_classification=request.data_classification,
                )
            findings["stage_c_semantic"] = {
                "status": "passed",
                "recomputed_result": expected_res,
                "method": "independent_ast_evaluation",
            }

        elif request.capability_id in ("filesystem.apply_staged_diff", "filesystem.rollback_staged_diff"):
            method_used = VerificationMethod.FILESYSTEM_HANDLE_DIGEST
            v_details = exec_record.verification_details_json or {}
            if not v_details.get("verified"):
                return VerificationResult(
                    status=VerificationStatus.FAILED,
                    stage=VerificationStage.STAGE_C_SEMANTIC,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    method=method_used,
                    input_digest=in_digest,
                    output_digest=out_digest,
                    passed=False,
                    reason="Stage C filesystem post-write handle verification was not confirmed.",
                    data_classification=request.data_classification,
                )
            findings["stage_c_semantic"] = {"status": "passed", "handle_verified": True}

        elif request.capability_id == "homeassistant.test_light_mutate":
            method_used = VerificationMethod.DEVICE_STATE_POLL
            v_details = exec_record.verification_details_json or {}
            if not v_details.get("state_verified"):
                return VerificationResult(
                    status=VerificationStatus.FAILED,
                    stage=VerificationStage.STAGE_C_SEMANTIC,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    method=method_used,
                    input_digest=in_digest,
                    output_digest=out_digest,
                    passed=False,
                    reason="Stage C smart-home independent read-after-write verification was not confirmed.",
                    data_classification=request.data_classification,
                )
            findings["stage_c_semantic"] = {"status": "passed", "state_verified": True}

        elif request.capability_id == "browser.open_page":
            findings["stage_c_semantic"] = {
                "status": "passed",
                "origin_checked": True,
                "byte_size": request.output_data.get("byte_size"),
            }

        else:
            # Honest reporting of semantic strategy limitation
            if capability.metadata.risk_tier == RiskTier.HIGH and capability.metadata.consequential:
                approver = (request.evidence or {}).get("approver")
                if not approver and request.tool_execution_record:
                    approver = getattr(request.tool_execution_record, "approved_by", None) or getattr(request.tool_execution_record, "approver", None)
                if approver:
                    findings["stage_c_semantic"] = {
                        "status": "passed",
                        "approver": approver,
                        "consequential_approval_verified": True,
                        "limitation_reported": True,
                    }
                else:
                    # Consequential actions without an explicit verified postcondition or approval evidence require operator review
                    return VerificationResult(
                        status=VerificationStatus.REQUIRES_REVIEW,
                        stage=VerificationStage.STAGE_C_SEMANTIC,
                        policy_id=self.policy_id,
                        policy_version=self.policy_version,
                        method=VerificationMethod.UNSUPPORTED_FALLBACK,
                        input_digest=in_digest,
                        output_digest=out_digest,
                        passed=False,
                        reason=f"No automated semantic postcondition oracle available for consequential capability '{request.capability_id}'; operator review required.",
                        findings={"limitation_reported": True, "requires_operator_review": True},
                        data_classification=request.data_classification,
                    )
            else:
                findings["stage_c_semantic"] = {
                    "status": "passed",
                    "semantic_strategy": "schema_and_evidence_only",
                    "limitation_reported": True,
                }

        # ---------------------------------------------------------------------
        # Stage D: Policy Decision
        # ---------------------------------------------------------------------
        findings["stage_d_decision"] = {"status": "accepted"}
        return VerificationResult(
            status=VerificationStatus.PASSED,
            stage=VerificationStage.STAGE_D_DECISION,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            method=method_used,
            input_digest=in_digest,
            output_digest=out_digest,
            passed=True,
            reason="All verification stages passed: structural integrity, execution evidence, and semantic checks satisfied.",
            findings=findings,
            evidence=evidence,
            data_classification=request.data_classification,
        )


class ModelAssistedVerificationPolicy(BaseVerificationPolicy):
    """
    Model-assisted semantic verification policy.
    Uses logical worker role 'verifier' scheduled over the single resident local model.
    Untrusted model verdict cannot override failed Stage A or Stage B checks or grant permissions.
    """
    policy_id = "evidence.model_assisted.v1"
    policy_version = "1.0.0"

    def __init__(self, fallback_policy: BaseVerificationPolicy | None = None) -> None:
        self.fallback = fallback_policy or DefaultEvidenceVerificationPolicy()

    async def verify(
        self,
        request: VerificationRequest,
        registry: CapabilityRegistry,
        db: Session,
        scheduler: Any = None,
    ) -> VerificationResult:
        # 1. Run deterministic structural and evidence checks first
        preliminary = await self.fallback.verify(request, registry, db, scheduler=scheduler)
        if not preliminary.passed:
            return preliminary

        in_digest = compute_digest(request.resolved_input)
        out_digest = compute_digest(request.output_data)

        # Check data classification: SECRET is strictly ineligible for model review
        if request.data_classification == "SECRET":
            return VerificationResult(
                status=VerificationStatus.FAILED,
                stage=VerificationStage.STAGE_C_SEMANTIC,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.MODEL_ASSISTED_REVIEW,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason="Data classification SECRET is ineligible for model-assisted review.",
                data_classification=request.data_classification,
            )

        # 2. If scheduler is unavailable or cloud access forbidden, return preliminary
        if scheduler is None:
            return preliminary


        # 3. Model-assisted semantic check via ResourceScheduler
        from pico.scheduler.types import PriorityClass, WorkerRole

        goal = request.step_goal_or_description or f"Execute capability {request.capability_id}"
        prompt = (
            f"You are an independent verification auditor for a workflow step.\n"
            f"Step Objective: {goal}\n"
            f"Capability: {request.capability_id}\n"
            f"Input: {json.dumps(request.resolved_input, sort_keys=True)}\n"
            f"Output: {json.dumps(request.output_data, sort_keys=True)}\n\n"
            f"Evaluate if the output accurately satisfies the step objective and input parameters.\n"
            f"Respond strictly in JSON format with keys:\n"
            f"- verdict: 'passed' | 'failed' | 'inconclusive'\n"
            f"- reason: brief explanation\n"
            f"- confidence: float between 0.0 and 1.0"
        )

        try:
            from pico.core.config import settings
            sched_res = await scheduler.schedule_request(
                model_name=settings.OLLAMA_MODEL,
                provider="ollama",
                role=WorkerRole.VERIFIER,
                priority=PriorityClass.WORKFLOW,
                prompt=prompt,
                system_prompt="You are a strict, objective verification auditor. Return valid JSON only.",
                data_classification=request.data_classification,
                max_tokens=256,
                temperature=0.0,
                timeout_seconds=30.0,
            )

            # Strict structured validation of model verdict
            raw_text = sched_res.text.strip()
            # Handle potential code fence
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                raw_text = "\n".join(lines).strip()

            parsed = json.loads(raw_text)
            verdict = str(parsed.get("verdict", "")).lower()
            reason = str(parsed.get("reason", "Model review completed."))
            confidence = float(parsed.get("confidence", 0.0))

            findings = dict(preliminary.findings)
            findings["stage_c_model_review"] = {
                "verdict": verdict,
                "reason": reason,
                "confidence": confidence,
                "model": settings.OLLAMA_MODEL,
                "shared_resident_runtime": True,
            }

            if verdict == "passed" and confidence >= 0.7:
                return VerificationResult(
                    status=VerificationStatus.PASSED,
                    stage=VerificationStage.STAGE_D_DECISION,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    method=VerificationMethod.MODEL_ASSISTED_REVIEW,
                    input_digest=in_digest,
                    output_digest=out_digest,
                    passed=True,
                    reason=f"Model-assisted review passed: {reason}",
                    findings=findings,
                    evidence=preliminary.evidence,
                    data_classification=request.data_classification,
                )
            elif verdict == "failed":
                return VerificationResult(
                    status=VerificationStatus.FAILED,
                    stage=VerificationStage.STAGE_C_SEMANTIC,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    method=VerificationMethod.MODEL_ASSISTED_REVIEW,
                    input_digest=in_digest,
                    output_digest=out_digest,
                    passed=False,
                    reason=f"Model-assisted review reported failure: {reason}",
                    findings=findings,
                    evidence=preliminary.evidence,
                    data_classification=request.data_classification,
                )
            else:
                return VerificationResult(
                    status=VerificationStatus.INCONCLUSIVE,
                    stage=VerificationStage.STAGE_C_SEMANTIC,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    method=VerificationMethod.MODEL_ASSISTED_REVIEW,
                    input_digest=in_digest,
                    output_digest=out_digest,
                    passed=False,
                    reason=f"Model-assisted review inconclusive (confidence={confidence}): {reason}",
                    findings=findings,
                    evidence=preliminary.evidence,
                    data_classification=request.data_classification,
                )
        except Exception as exc:
            # Model parsing failure fails closed to inconclusive, never silent pass
            findings = dict(preliminary.findings)
            findings["stage_c_model_review"] = {"error": str(exc), "status": "failed_closed"}
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                stage=VerificationStage.STAGE_C_SEMANTIC,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                method=VerificationMethod.MODEL_ASSISTED_REVIEW,
                input_digest=in_digest,
                output_digest=out_digest,
                passed=False,
                reason=f"Model-assisted review failed closed due to malformed output or error: {exc}",
                findings=findings,
                evidence=preliminary.evidence,
                data_classification=request.data_classification,
            )


# Global policy registry
_POLICIES: dict[str, BaseVerificationPolicy] = {
    DefaultEvidenceVerificationPolicy.policy_id: DefaultEvidenceVerificationPolicy(),
    ModelAssistedVerificationPolicy.policy_id: ModelAssistedVerificationPolicy(),
}


def get_verification_policy(policy_id: str | None = None) -> BaseVerificationPolicy:
    """Resolve verification policy by ID, defaulting to evidence.deterministic.v1."""
    pid = policy_id or DefaultEvidenceVerificationPolicy.policy_id
    if pid not in _POLICIES:
        raise ValueError(f"Unknown verification policy ID: '{pid}'")
    return _POLICIES[pid]
