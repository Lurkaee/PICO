"""Read-only desktop window inspection capability (Milestone P8.1)."""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)
from pico.os_automation.contracts import WindowMetadata
from pico.os_automation.manager import get_os_adapter


class InspectWindowsInput(BaseModel):
    max_windows: int = Field(default=50, ge=1, le=100, description="Maximum number of windows to enumerate")
    max_title_length: int = Field(default=256, ge=1, le=512, description="Maximum length per window title")

    model_config = ConfigDict(extra="forbid")


class InspectWindowsOutput(BaseModel):
    status: str = Field(description="Inspection status: 'success', 'unavailable', or 'unsupported'")
    platform: str = Field(description="Host platform identifier")
    windows: list[WindowMetadata] = Field(default_factory=list, description="Enumerated window metadata items")
    total_windows_found: int = Field(default=0, description="Total matching windows discovered before bounding")
    truncated: bool = Field(default=False, description="Whether result was truncated to max_windows")
    message: str | None = Field(default=None, description="Diagnostic reason if unavailable or unsupported")

    model_config = ConfigDict(extra="ignore")


class InspectWindowsCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="os.inspect_windows",
        version="1.0.0",
        name="Inspect Desktop Windows",
        description="Enumerates visible top-level desktop windows and bounded metadata read-only without window interaction.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="os:inspect",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=65536,
        idempotent=True,
        max_retries=0,
        consequential=False,
        preconditions=["Host platform supports window inspection or returns honest unsupported status"],
        expected_effects=["Returns bounded list of visible window titles and process IDs; zero desktop mutation"],
    )
    input_model = InspectWindowsInput
    output_model = InspectWindowsOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        try:
            validated_input = InspectWindowsInput.model_validate(input_data)
            adapter = get_os_adapter()
            result = await adapter.inspect_windows(
                max_windows=validated_input.max_windows,
                max_title_length=validated_input.max_title_length,
            )
            out_dict = result.model_dump()
            return ExecutionResult(
                success=True,
                output_data=out_dict,
                evidence={
                    "platform": result.platform,
                    "status": result.status,
                    "window_count": len(result.windows),
                    "total_found": result.total_windows_found,
                    "truncated": result.truncated,
                },
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Window inspection failed: {str(exc)}",
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

        max_w = input_data.get("max_windows", 50)
        max_t = input_data.get("max_title_length", 256)
        windows = output_data.get("windows", [])

        if len(windows) > max_w:
            return VerificationResult(
                passed=False,
                reason=f"Returned window count {len(windows)} exceeds requested bound {max_w}",
            )

        for w in windows:
            title = w.get("title", "") if isinstance(w, dict) else getattr(w, "title", "")
            if len(title) > max_t:
                return VerificationResult(
                    passed=False,
                    reason=f"Window title length {len(title)} exceeds requested bound {max_t}",
                )

        return VerificationResult(passed=True, details={"verified_windows": len(windows)})
