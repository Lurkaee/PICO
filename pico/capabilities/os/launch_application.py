"""Consequential desktop application launch capability (Milestone P8.1)."""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from pico.core.errors import ValidationError
from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)
from pico.os_automation.allowlist import (
    compute_launch_profile_fingerprint,
    validate_launch_profile,
)
from pico.os_automation.contracts import (
    LaunchVerificationStatus,
)
from pico.os_automation.manager import get_allowlist_manager, get_os_adapter


class LaunchApplicationInput(BaseModel):
    alias: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        description="Configured server-side application alias",
    )
    launch_fingerprint: str | None = Field(
        default=None,
        description="Trusted server-computed launch profile fingerprint (SHA-256)",
    )

    model_config = ConfigDict(extra="forbid")


class LaunchApplicationOutput(BaseModel):
    alias: str
    status: str
    executable_path: str
    process_id: int | None = None
    verification_status: str
    verification_details: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None

    model_config = ConfigDict(extra="ignore")


class LaunchApplicationCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="os.launch_application",
        version="1.0.0",
        name="Launch Allowlisted Desktop Application",
        description="Launches an approved application alias using server-configured launch profiles without shell execution.",
        risk_tier=RiskTier.HIGH,
        required_permission="os:launch",
        required_classification="INTERNAL",
        timeout_seconds=10.0,
        max_output_bytes=16384,
        idempotent=False,
        max_retries=0,
        consequential=True,
        preconditions=[
            "Application alias must exist in server allowlist",
            "Launch profile must specify valid absolute executable with shell=False",
            "Single-use human operator approval bound to exact input digest must be verified",
        ],
        expected_effects=[
            "Spawns child process using direct argument list in sanitized environment",
            "Records honest process observation without equating spawn with full application usability",
        ],
    )
    input_model = LaunchApplicationInput
    output_model = LaunchApplicationOutput

    def prepare_input(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Enriches and binds the trusted server-computed launch profile fingerprint.
        Fails closed if caller attempts to forge, substitute, or tamper with fingerprint.
        """
        data = dict(input_data)
        alias = data.get("alias")
        if not alias or not isinstance(alias, str):
            return data

        manager = get_allowlist_manager()
        profile = manager.get_profile(alias)

        if profile is not None:
            validated_profile = validate_launch_profile(profile)
            server_fingerprint = compute_launch_profile_fingerprint(validated_profile)

            caller_fingerprint = data.get("launch_fingerprint")
            if caller_fingerprint is not None:
                if not isinstance(caller_fingerprint, str) or caller_fingerprint.strip() != server_fingerprint:
                    raise ValidationError(
                        "Caller-supplied launch_fingerprint does not match trusted server-computed profile fingerprint."
                    )

            data["launch_fingerprint"] = server_fingerprint
        else:
            if data.get("launch_fingerprint") is not None:
                raise ValidationError(
                    f"Cannot supply launch_fingerprint for unallowlisted application alias '{alias}'."
                )
            data["launch_fingerprint"] = None

        return data

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        try:
            validated_input = LaunchApplicationInput.model_validate(input_data)
            alias = validated_input.alias
            bound_fingerprint = validated_input.launch_fingerprint

            # 1. Resolve from server-side allowlist
            manager = get_allowlist_manager()
            profile = manager.get_profile(alias)

            if not profile:
                out = LaunchApplicationOutput(
                    alias=alias,
                    status=LaunchVerificationStatus.REQUEST_REJECTED.value,
                    executable_path="",
                    process_id=None,
                    verification_status="rejected",
                    verification_details={"reason": "alias_not_allowlisted"},
                    message=f"Application alias '{alias}' is not present in the server allowlist.",
                )
                return ExecutionResult(
                    success=False,
                    output_data=out.model_dump(),
                    error_message=f"Application alias '{alias}' is not allowlisted.",
                    evidence={"rejected": True, "reason": "alias_not_allowlisted"},
                )

            # 2. Re-validate profile at the actual execution boundary
            validated_profile = validate_launch_profile(profile)
            current_fingerprint = compute_launch_profile_fingerprint(validated_profile)

            # 3. Verify approval-bound fingerprint
            if not bound_fingerprint:
                out = LaunchApplicationOutput(
                    alias=alias,
                    status=LaunchVerificationStatus.REQUEST_REJECTED.value,
                    executable_path=validated_profile.executable_path,
                    process_id=None,
                    verification_status="rejected",
                    verification_details={"reason": "missing_approval_fingerprint"},
                    message="Application launch rejected: missing approval-bound launch profile fingerprint.",
                )
                return ExecutionResult(
                    success=False,
                    output_data=out.model_dump(),
                    error_message="Missing approval-bound launch profile fingerprint.",
                    evidence={"rejected": True, "reason": "missing_approval_fingerprint"},
                )

            if current_fingerprint != bound_fingerprint:
                out = LaunchApplicationOutput(
                    alias=alias,
                    status=LaunchVerificationStatus.REQUEST_REJECTED.value,
                    executable_path=validated_profile.executable_path,
                    process_id=None,
                    verification_status="rejected",
                    verification_details={
                        "reason": "profile_fingerprint_mismatch",
                        "approved_fingerprint": bound_fingerprint,
                        "current_fingerprint": current_fingerprint,
                    },
                    message=(
                        f"Application launch rejected: server-configured launch profile for '{alias}' "
                        f"changed after approval was granted."
                    ),
                )
                return ExecutionResult(
                    success=False,
                    output_data=out.model_dump(),
                    error_message=(
                        f"Launch profile for alias '{alias}' was modified after approval. "
                        f"Approved fingerprint {bound_fingerprint[:8]}... does not match current profile."
                    ),
                    evidence={"rejected": True, "reason": "profile_fingerprint_mismatch"},
                )

            # 4. Dispatch launch through adapter
            adapter = get_os_adapter()
            result = await adapter.launch_application(validated_profile)

            success = result.status in (
                LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED,
                LaunchVerificationStatus.INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED,
            )

            out_data = result.model_dump()
            return ExecutionResult(
                success=success,
                output_data=out_data,
                error_message=result.message if not success else None,
                evidence={
                    "alias": alias,
                    "status": result.status.value,
                    "process_id": result.process_id,
                    "executable_path": result.executable_path,
                    "verification_status": result.verification_status,
                },
            )

        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Application launch failed: {str(exc)}",
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

        alias = input_data.get("alias")
        reported_alias = output_data.get("alias")
        if reported_alias != alias:
            return VerificationResult(
                passed=False,
                reason=f"Reported alias '{reported_alias}' does not match input alias '{alias}'",
            )

        status_str = output_data.get("status")
        valid_statuses = {s.value for s in LaunchVerificationStatus}
        if status_str not in valid_statuses:
            return VerificationResult(
                passed=False,
                reason=f"Unrecognized launch verification status '{status_str}'",
            )

        # If reported success, verify executable path matches
        if status_str in (
            LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED.value,
            LaunchVerificationStatus.INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED.value,
        ):
            exe_path = output_data.get("executable_path")
            if not exe_path:
                return VerificationResult(
                    passed=False,
                    reason="Successful launch must report non-empty executable path",
                )

        return VerificationResult(
            passed=True,
            details={"status": status_str, "verification_status": output_data.get("verification_status")},
        )
