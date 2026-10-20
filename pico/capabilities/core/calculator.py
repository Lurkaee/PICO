import ast
import operator
from typing import Any
from pydantic import BaseModel, Field

from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)


class CalculatorInput(BaseModel):
    expression: str = Field(min_length=1, max_length=256, description="Arithmetic expression to evaluate")


class CalculatorOutput(BaseModel):
    expression: str
    result: float


_SAFE_OPERATORS = {
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


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_type == ast.Pow and (abs(right) > 100 or abs(left) > 1000):
            raise ValueError("Exponentiation arguments exceeded safe bounds")
        if op_type in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
            raise ZeroDivisionError("Division by zero")
        return float(_SAFE_OPERATORS[op_type](left, right))
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = _eval_node(node.operand)
        return float(_SAFE_OPERATORS[op_type](operand))
    raise ValueError(f"Unsupported AST node in expression: {type(node).__name__}")


def evaluate_arithmetic(expression: str) -> float:
    parsed = ast.parse(expression.strip(), mode="eval")
    return _eval_node(parsed.body)


class CalculatorCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="core.calculator",
        version="1.0.0",
        name="Safe Bounded Calculator",
        description="Deterministically evaluates safe mathematical expressions without eval() or code execution.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="tools:calculate",
        required_classification="PUBLIC",
        timeout_seconds=2.0,
        max_output_bytes=4096,
        idempotent=True,
        max_retries=1,
        consequential=False,
        preconditions=["Valid arithmetic expression within character limit"],
        expected_effects=["Returns numerical evaluation result"],
    )
    input_model = CalculatorInput
    output_model = CalculatorOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        try:
            expr = input_data["expression"]
            res = evaluate_arithmetic(expr)
            return ExecutionResult(
                success=True,
                output_data={"expression": expr, "result": res},
                evidence={"evaluated_at": "ast_evaluator", "raw_result": res},
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Arithmetic evaluation failed: {str(exc)}",
            )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        base_ver = await super().verify(input_data, output_data, evidence, context)
        if not base_ver.passed:
            return base_ver

        try:
            # Independent recalculation proof
            recalc = evaluate_arithmetic(input_data["expression"])
            reported = output_data.get("result")
            if abs(recalc - reported) > 1e-9:
                return VerificationResult(
                    passed=False,
                    reason=f"Calculated result {reported} does not match independent verification {recalc}",
                )
            return VerificationResult(passed=True, details={"recalculated": recalc})
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Independent verification calculation failed: {str(exc)}",
            )
