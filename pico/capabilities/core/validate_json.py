import json
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


class ValidateJsonInput(BaseModel):
    raw_json: str = Field(min_length=1, max_length=65536, description="JSON string to validate")
    expected_keys: list[str] = Field(default_factory=list, description="Keys required in top-level JSON object")


class ValidateJsonOutput(BaseModel):
    is_valid: bool
    key_count: int
    top_level_keys: list[str]
    missing_keys: list[str]


class ValidateJsonCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="core.validate_json",
        version="1.0.0",
        name="Structured JSON Validator",
        description="Validates structured JSON text against syntax and key requirements without executing code.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="tools:validate",
        required_classification="PUBLIC",
        timeout_seconds=3.0,
        max_output_bytes=16384,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = ValidateJsonInput
    output_model = ValidateJsonOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        raw = input_data["raw_json"]
        expected = input_data.get("expected_keys", [])
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return ExecutionResult(
                    success=True,
                    output_data={
                        "is_valid": False,
                        "key_count": 0,
                        "top_level_keys": [],
                        "missing_keys": expected,
                    },
                    evidence={"parsed_type": type(parsed).__name__},
                )
            keys = sorted(list(parsed.keys()))
            missing = [k for k in expected if k not in parsed]
            is_valid = len(missing) == 0
            return ExecutionResult(
                success=True,
                output_data={
                    "is_valid": is_valid,
                    "key_count": len(keys),
                    "top_level_keys": keys,
                    "missing_keys": missing,
                },
                evidence={"keys_detected": keys},
            )
        except json.JSONDecodeError as err:
            return ExecutionResult(
                success=True,
                output_data={
                    "is_valid": False,
                    "key_count": 0,
                    "top_level_keys": [],
                    "missing_keys": expected,
                },
                evidence={"json_error": str(err)},
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"JSON validation failed: {str(exc)}",
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

        raw = input_data["raw_json"]
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                actual_keys = sorted(list(parsed.keys()))
                if output_data.get("top_level_keys") != actual_keys:
                    return VerificationResult(
                        passed=False,
                        reason="Reported top-level keys do not match actual parsed keys.",
                    )
            return VerificationResult(passed=True)
        except Exception:
            if output_data.get("is_valid") is True:
                return VerificationResult(
                    passed=False,
                    reason="Invalid JSON was incorrectly reported as valid.",
                )
            return VerificationResult(passed=True)
