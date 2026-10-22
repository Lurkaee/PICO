"""Sandboxed browser open_page capability (Milestone P8.3)."""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from pico.browser.contracts import NavigationStatus, PageObservation
from pico.browser.manager import get_browser_adapter
from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)


class OpenPageInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048, description="Target URL to navigate to and inspect")
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0, description="Navigation timeout in seconds")
    max_bytes: int = Field(default=65536, ge=1024, le=262144, description="Maximum extracted text bytes")
    max_links: int = Field(default=50, ge=0, le=200, description="Maximum links to extract")

    model_config = ConfigDict(extra="forbid")


class OpenPageCapability(BaseCapability):
    """
    Read-only sandboxed web page inspection capability.
    Enforces origin allowlisting, SSRF protection, ephemeral contexts,
    and returns bounded observations classified strictly as INTERNAL.
    """

    metadata = CapabilityMetadata(
        capability_id="browser.open_page",
        version="1.0.0",
        name="Open and Inspect Web Page",
        description="Navigates to an approved web origin, retrieves bounded read-only page observation, and sanitizes untrusted content.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="browser:open",
        required_classification="INTERNAL",
        timeout_seconds=15.0,
        max_output_bytes=65536,
        idempotent=False,
        max_retries=0,
        consequential=False,
        preconditions=[
            "Browser automation enabled in server settings",
            "Target origin allowlisted in server configuration",
            "Destination passes SSRF network boundary checks",
        ],
        expected_effects=[
            "Returns bounded read-only page observation with untrusted content classified as INTERNAL; zero browser side-effects",
        ],
    )

    input_model = OpenPageInput
    output_model = PageObservation

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        try:
            validated = self.input_model.model_validate(input_data)
            adapter = get_browser_adapter()
            obs = await adapter.open_page(
                url=validated.url,
                timeout_seconds=validated.timeout_seconds,
                max_bytes=validated.max_bytes,
                max_links=validated.max_links,
            )

            success = (obs.status == NavigationStatus.SUCCESS)
            return ExecutionResult(
                success=success,
                output_data=obs.model_dump(),
                evidence={
                    "status": obs.status.value,
                    "requested_url": obs.requested_url,
                    "final_url": obs.final_url,
                    "truncated": obs.truncated,
                    "classification": obs.classification,
                },
                error_message=obs.message if not success else None,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Browser execution error: {str(exc)}",
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
            obs = self.output_model.model_validate(output_data)

            # Security Invariant: External page observation classification MUST be INTERNAL
            if obs.classification != "INTERNAL":
                return VerificationResult(
                    passed=False,
                    reason=f"Page observation classification '{obs.classification}' is invalid; must be strictly 'INTERNAL'.",
                )

            # Verify bounding limits
            max_bytes = input_data.get("max_bytes", 65536)
            if len(obs.text_content.encode("utf-8")) > max_bytes:
                return VerificationResult(
                    passed=False,
                    reason=f"Extracted text content ({len(obs.text_content.encode('utf-8'))} bytes) exceeds limit ({max_bytes} bytes).",
                )

            max_links = input_data.get("max_links", 50)
            if len(obs.links) > max_links:
                return VerificationResult(
                    passed=False,
                    reason=f"Extracted link count ({len(obs.links)}) exceeds limit ({max_links}).",
                )

            return VerificationResult(passed=True)
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Verification validation error: {str(exc)}",
            )
