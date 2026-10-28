from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pico.capabilities.base import (
    BaseCapability,
    CapabilityMetadata,
    ExecutionContext,
    ExecutionResult,
    RiskTier,
    VerificationResult,
)
from pico.homeassistant.actuation import (
    HomeAssistantActuationService,
    HomeAssistantClimateActuationService,
    HomeAssistantSwitchActuationService,
    validate_actuation_preconditions,
    validate_climate_actuation_preconditions,
    validate_switch_actuation_preconditions,
)
from pico.core.errors import ValidationError
from pico.homeassistant.climate import (
    validate_climate_allowlist,
    validate_climate_grid_origins,
)
from pico.homeassistant.contracts import (
    ClimateActuationResult,
    SwitchActuationResult,
    TestLightActuationResult,
)


class HomeAssistantTestLightActuationInput(BaseModel):
    target_state: Literal["on", "off"] = Field(
        description="Explicit target state: 'on' or 'off'. Toggle is strictly prohibited."
    )
    target_entity_id: str | None = Field(
        default=None,
        description="Optional target entity ID. If supplied, must strictly match configured test light.",
    )
    reason: str | None = Field(
        default=None,
        description="Optional human-readable reason for actuation.",
    )

    @model_validator(mode="after")
    def validate_target_entity(self) -> "HomeAssistantTestLightActuationInput":
        if self.target_entity_id is not None:
            validate_actuation_preconditions(target_entity_id=self.target_entity_id, operation=self.target_state)
        return self


class HomeAssistantTestLightActuationOutput(BaseModel):
    success: bool
    entity_id: str
    requested_state: str
    reported_state: str
    dispatch_status: str
    verification_status: str
    verification_details: dict[str, Any] = Field(default_factory=dict)
    message: str
    physical_device_connectivity_claimed: bool = False


class HomeAssistantTestLightActuationCapability(BaseCapability):
    """Milestone P7.3: Actuate the single configured Home Assistant test light.

    Consequential action: Requires explicit human operator approval bound to SHA-256 digest.
    Risk Tier: HIGH.
    Retries: Strictly 0.
    Target: Strictly the single configured test light entity.
    """

    metadata = CapabilityMetadata(
        capability_id="homeassistant.test_light_actuate",
        version="1.0.0",
        name="Home Assistant Test Light Actuation",
        description=(
            "Actuate the single configured Home Assistant test light to 'on' or 'off'. "
            "Requires explicit human approval, strictly bounds target entity to configuration, "
            "and requires independent read-after-write state verification."
        ),
        risk_tier=RiskTier.HIGH,
        required_permission="homeassistant:actuate",
        required_classification="INTERNAL",
        timeout_seconds=15.0,
        max_output_bytes=4096,
        idempotent=False,
        max_retries=0,
        consequential=True,
        preconditions=[
            "Home Assistant integration and physical actuation are explicitly enabled",
            "Target entity strictly equals HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID",
            "Human operator approval ticket is valid and matches input digest",
        ],
        expected_effects=[
            "Dispatches exactly one service call (turn_on or turn_off)",
            "Independent read-only state check verifies target state",
        ],
    )
    input_model = HomeAssistantTestLightActuationInput
    output_model = HomeAssistantTestLightActuationOutput

    def __init__(self, service: HomeAssistantActuationService | None = None) -> None:
        self._service = service

    def _get_service(self) -> HomeAssistantActuationService:
        if self._service:
            return self._service
        return HomeAssistantActuationService()

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        # Precondition check before any network mutation
        target_state: Literal["on", "off"] = input_data.get("target_state")  # type: ignore[assignment]
        target_entity_id = input_data.get("target_entity_id")

        service = self._get_service()
        configured_id = validate_actuation_preconditions(
            target_entity_id=target_entity_id,
            operation=target_state,
        )

        # Single auditable dispatch point
        try:
            dispatch_res = await service.dispatch_actuation(
                target_state=target_state,
                target_entity_id=target_entity_id,
                execution_id=context.execution_id,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Actuation dispatch rejected or failed: {str(exc)}",
                evidence={"dispatch_attempted": True, "error": str(exc)},
            )

        dispatch_status = dispatch_res.get("dispatch_status", "unknown")

        output = HomeAssistantTestLightActuationOutput(
            success=True,
            entity_id=configured_id,
            requested_state=target_state,
            reported_state="pending_verification",
            dispatch_status=dispatch_status,
            verification_status="pending",
            verification_details={},
            message=f"Test light '{configured_id}' service dispatch '{dispatch_status}'. Pending independent verification.",
            physical_device_connectivity_claimed=False,
        )

        return ExecutionResult(
            success=True,
            output_data=output.model_dump(),
            evidence={
                "dispatch": dispatch_res,
            },
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        try:
            self.output_model.model_validate(output_data)

            # Invariant: Never claim physical device connectivity
            if output_data.get("physical_device_connectivity_claimed") is True:
                return VerificationResult(
                    passed=False,
                    reason="Capability erroneously claimed physical device connectivity.",
                )

            service = self._get_service()
            target_state = input_data.get("target_state")
            target_entity_id = input_data.get("target_entity_id")

            # Independent read-after-write state verification using read-only path
            ver_res = await service.verify_state(
                expected_state=target_state,
                target_entity_id=target_entity_id,
            )

            # Update output_data with verification result
            output_data["verification_status"] = "verified" if ver_res.passed else "failed"
            output_data["reported_state"] = ver_res.details.get("reported_state", "unknown")
            output_data["verification_details"] = ver_res.details
            output_data["success"] = ver_res.passed
            output_data["message"] = (
                f"Test light '{output_data['entity_id']}' verified in state '{target_state}'."
                if ver_res.passed
                else f"Test light '{output_data['entity_id']}' actuation verification failed: {ver_res.reason}"
            )

            if not ver_res.passed:
                return VerificationResult(
                    passed=False,
                    reason=ver_res.reason,
                    details=ver_res.details,
                )

            return VerificationResult(
                passed=True,
                details=ver_res.details,
            )
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Independent verifier failed schema or semantic check: {str(exc)}",
                details={"error": str(exc)},
            )


class HomeAssistantSwitchActuationInput(BaseModel):
    target_entity_id: str = Field(
        description="Exact allowlisted switch entity ID (e.g. 'switch.living_room_plug')."
    )
    target_state: Literal["on", "off"] = Field(
        description="Explicit target state: 'on' or 'off'. Toggle is strictly prohibited."
    )
    reason: str | None = Field(
        default=None,
        description="Optional human-readable reason for actuation.",
    )

    @model_validator(mode="after")
    def validate_target_entity(self) -> "HomeAssistantSwitchActuationInput":
        validate_switch_actuation_preconditions(
            target_entity_id=self.target_entity_id,
            operation=self.target_state,
        )
        return self


class HomeAssistantSwitchActuationOutput(BaseModel):
    success: bool
    entity_id: str
    requested_state: str
    reported_state: str
    dispatch_status: str
    verification_status: str
    verification_details: dict[str, Any] = Field(default_factory=dict)
    message: str
    physical_device_connectivity_claimed: bool = False


class HomeAssistantSwitchActuationCapability(BaseCapability):
    """Milestone P7.4.1: Actuate an explicitly allowlisted Home Assistant switch entity.

    Consequential action: Requires explicit human operator approval bound to SHA-256 digest.
    Risk Tier: HIGH.
    Retries: Strictly 0.
    Target: Strictly an exact entity ID in HOME_ASSISTANT_SWITCH_ALLOWLIST.
    """

    metadata = CapabilityMetadata(
        capability_id="homeassistant.allowlisted_switch_actuate",
        version="1.0.0",
        name="Home Assistant Allowlisted Switch Actuation",
        description=(
            "Actuate an explicitly configured and allowlisted Home Assistant switch to 'on' or 'off'. "
            "Requires explicit human approval, strictly bounds target entity to configuration, "
            "and requires independent read-after-write state verification."
        ),
        risk_tier=RiskTier.HIGH,
        required_permission="homeassistant:actuate",
        required_classification="INTERNAL",
        timeout_seconds=15.0,
        max_output_bytes=4096,
        idempotent=False,
        max_retries=0,
        consequential=True,
        preconditions=[
            "Home Assistant integration, physical actuation, and Stage C switch actuation are explicitly enabled",
            "Target entity is explicitly present in HOME_ASSISTANT_SWITCH_ALLOWLIST",
            "Human operator approval ticket is valid and matches input digest",
        ],
        expected_effects=[
            "Dispatches at most one service call (turn_on or turn_off)",
            "Independent read-only state check verifies target state",
        ],
    )
    input_model = HomeAssistantSwitchActuationInput
    output_model = HomeAssistantSwitchActuationOutput

    def __init__(self, service: HomeAssistantSwitchActuationService | None = None) -> None:
        self._service = service

    def _get_service(self) -> HomeAssistantSwitchActuationService:
        if self._service:
            return self._service
        return HomeAssistantSwitchActuationService()

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        target_state: Literal["on", "off"] = input_data.get("target_state")  # type: ignore[assignment]
        target_entity_id = input_data.get("target_entity_id", "")

        service = self._get_service()
        validated_id = validate_switch_actuation_preconditions(
            target_entity_id=target_entity_id,
            operation=target_state,
        )

        try:
            dispatch_res = await service.dispatch_actuation(
                target_entity_id=validated_id,
                target_state=target_state,
                execution_id=context.execution_id,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Actuation dispatch rejected or failed: {str(exc)}",
                evidence={"dispatch_attempted": True, "error": str(exc)},
            )

        dispatch_status = dispatch_res.get("dispatch_status", "unknown")

        if dispatch_status == "blocked_precondition":
            reason = dispatch_res.get("reason", "Precondition could not be established.")
            output = HomeAssistantSwitchActuationOutput(
                success=False,
                entity_id=validated_id,
                requested_state=target_state,
                reported_state=dispatch_res.get("reported_state", "unknown"),
                dispatch_status="blocked_precondition",
                verification_status="blocked",
                verification_details={"reason": reason, "dispatch": dispatch_res},
                message=f"Switch '{validated_id}' actuation blocked: {reason}",
                physical_device_connectivity_claimed=False,
            )
            return ExecutionResult(
                success=False,
                output_data=output.model_dump(),
                error_message=f"Actuation dispatch blocked: {reason}",
                evidence={"dispatch": dispatch_res},
            )

        if dispatch_status == "already_satisfied":
            output = HomeAssistantSwitchActuationOutput(
                success=True,
                entity_id=validated_id,
                requested_state=target_state,
                reported_state=target_state,
                dispatch_status="already_satisfied",
                verification_status="pending",
                verification_details={},
                message=f"Switch '{validated_id}' already in requested state '{target_state}'. Pending independent verification.",
                physical_device_connectivity_claimed=False,
            )
            return ExecutionResult(
                success=True,
                output_data=output.model_dump(),
                evidence={"dispatch": dispatch_res},
            )

        output = HomeAssistantSwitchActuationOutput(
            success=True,
            entity_id=validated_id,
            requested_state=target_state,
            reported_state="pending_verification",
            dispatch_status=dispatch_status,
            verification_status="pending",
            verification_details={},
            message=f"Switch '{validated_id}' service dispatch '{dispatch_status}'. Pending independent verification.",
            physical_device_connectivity_claimed=False,
        )

        return ExecutionResult(
            success=True,
            output_data=output.model_dump(),
            evidence={
                "dispatch": dispatch_res,
            },
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        try:
            self.output_model.model_validate(output_data)

            # Invariant: Never claim physical device connectivity
            if output_data.get("physical_device_connectivity_claimed") is True:
                return VerificationResult(
                    passed=False,
                    reason="Capability erroneously claimed physical device connectivity.",
                )

            if output_data.get("dispatch_status") == "blocked_precondition":
                return VerificationResult(
                    passed=False,
                    reason="Capability dispatch was blocked at precondition check.",
                    details=output_data.get("verification_details", {}),
                )

            service = self._get_service()
            target_state = input_data.get("target_state")
            target_entity_id = input_data.get("target_entity_id")

            ver_res = await service.verify_state(
                target_entity_id=target_entity_id,
                expected_state=target_state,
            )

            output_data["verification_status"] = "verified" if ver_res.passed else "failed"
            output_data["reported_state"] = ver_res.details.get("reported_state", "unknown")
            output_data["verification_details"] = ver_res.details
            output_data["success"] = ver_res.passed
            output_data["message"] = (
                f"Switch '{output_data['entity_id']}' verified in state '{target_state}'."
                if ver_res.passed
                else f"Switch '{output_data['entity_id']}' actuation verification failed: {ver_res.reason}"
            )

            if not ver_res.passed:
                return VerificationResult(
                    passed=False,
                    reason=ver_res.reason,
                    details=ver_res.details,
                )

            return VerificationResult(
                passed=True,
                details=ver_res.details,
            )
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Independent verifier failed schema or semantic check: {str(exc)}",
                details={"error": str(exc)},
            )


class HomeAssistantClimateActuationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_entity_id: str = Field(
        description="Exact climate entity ID in the Stage D climate allowlist."
    )
    target_temperature: float = Field(
        description="Explicit target temperature setpoint. Must be a finite number."
    )
    target_unit: str | None = Field(
        default=None,
        description="Optional explicit temperature unit (e.g. '°C', '°F').",
    )
    reason: str | None = Field(
        default=None,
        description="Optional human-readable reason for climate setpoint actuation.",
    )
    expected_grid_origin: float | None = Field(
        default=None,
        description="Trusted server-configured grid origin bound into the execution input and approval digest.",
    )
    expected_grid_origin_unit: str | None = Field(
        default=None,
        description="Trusted server-configured grid origin unit bound into the execution input and approval digest.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_untrusted_grid_origin(cls, data: Any) -> Any:
        if isinstance(data, dict) and "grid_origin" in data:
            raise ValueError(
                "Client-supplied 'grid_origin' is forbidden. Grid origins are server-side "
                "operator configurations and cannot be supplied or overridden in actuation requests."
            )
        return data

    @model_validator(mode="after")
    def validate_preconditions(self) -> "HomeAssistantClimateActuationInput":
        validate_climate_actuation_preconditions(
            target_entity_id=self.target_entity_id,
            target_temperature=self.target_temperature,
            target_unit=self.target_unit,
        )
        return self


class HomeAssistantClimateActuationOutput(BaseModel):
    success: bool
    entity_id: str
    requested_temperature: float
    requested_unit: str | None = None
    reported_temperature: float | None = None
    reported_mode: str | None = None
    dispatch_status: str
    verification_status: str
    verification_details: dict[str, Any] = Field(default_factory=dict)
    message: str
    physical_device_connectivity_claimed: bool = False


class HomeAssistantClimateActuationCapability(BaseCapability):
    """Milestone P7.4.2.2: Actuate an explicitly allowlisted Home Assistant climate entity setpoint.

    Consequential action: Requires explicit human operator approval bound to SHA-256 digest.
    Risk Tier: HIGH.
    Retries: Strictly 0.
    Target: Strictly an exact entity ID in HOME_ASSISTANT_CLIMATE_ALLOWLIST.
    """

    metadata = CapabilityMetadata(
        capability_id="homeassistant.allowlisted_climate_setpoint_actuate",
        version="1.0.0",
        name="Home Assistant Allowlisted Climate Setpoint Actuation",
        description=(
            "Actuate an explicitly configured and allowlisted Home Assistant climate entity to a verified single target temperature setpoint. "
            "Requires explicit human approval, strictly bounds target entity to configuration, verifies decimal-safe step alignment against "
            "trusted server grid origin, requires active compatible operating mode (heat or cool), and requires independent read-after-write state verification."
        ),
        risk_tier=RiskTier.HIGH,
        required_permission="homeassistant:actuate",
        required_classification="INTERNAL",
        timeout_seconds=15.0,
        max_output_bytes=4096,
        idempotent=False,
        max_retries=0,
        consequential=True,
        preconditions=[
            "Home Assistant integration, physical actuation, climate awareness, and Stage D climate actuation are explicitly enabled",
            "Target entity is explicitly present in HOME_ASSISTANT_CLIMATE_ALLOWLIST",
            "Target operating mode is compatible ('heat' or 'cool')",
            "Setpoint is within operating bounds and aligns with server-configured grid origin",
            "Human operator approval ticket is valid and matches input digest",
        ],
        expected_effects=[
            "Dispatches at most one service call (climate.set_temperature)",
            "Independent read-only state check verifies reported target temperature",
        ],
    )
    input_model = HomeAssistantClimateActuationInput
    output_model = HomeAssistantClimateActuationOutput

    def __init__(self, service: HomeAssistantClimateActuationService | None = None) -> None:
        self._service = service

    def _get_service(self) -> HomeAssistantClimateActuationService:
        if self._service:
            return self._service
        return HomeAssistantClimateActuationService()

    def prepare_input(self, input_data: dict[str, Any], db: Any = None) -> dict[str, Any]:
        """Enriches and binds the trusted server-configured climate grid origin and unit.

        Fails closed if caller attempts to forge, substitute, or tamper with grid origin.
        """
        data = dict(input_data)
        entity_id = data.get("target_entity_id")
        if not entity_id or not isinstance(entity_id, str):
            return data

        clean_id = entity_id.strip()

        # Resolve server-configured grid origin for this entity
        effective_origin: float | None = None
        effective_unit: str | None = None
        try:
            current_allowlist = validate_climate_allowlist()
            origins_map = validate_climate_grid_origins(allowlist=current_allowlist)
            if clean_id in origins_map:
                effective_origin = origins_map[clean_id]["origin"]
                effective_unit = origins_map[clean_id]["unit"]
        except Exception:
            # If invalid or cannot be loaded, keep as None; validation will fail downstream
            pass

        # Protect against client tampering
        if "expected_grid_origin" in data and data["expected_grid_origin"] is not None:
            if data["expected_grid_origin"] != effective_origin:
                raise ValidationError(
                    "Caller-supplied expected_grid_origin does not match trusted server-configured grid origin."
                )
        if "expected_grid_origin_unit" in data and data["expected_grid_origin_unit"] is not None:
            if data["expected_grid_origin_unit"] != effective_unit:
                raise ValidationError(
                    "Caller-supplied expected_grid_origin_unit does not match trusted server-configured grid origin unit."
                )

        data["expected_grid_origin"] = effective_origin
        data["expected_grid_origin_unit"] = effective_unit
        return data

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        target_entity_id = input_data.get("target_entity_id", "")
        target_temperature = input_data.get("target_temperature")
        target_unit = input_data.get("target_unit")

        service = self._get_service()
        validated_id = validate_climate_actuation_preconditions(
            target_entity_id=target_entity_id,
            target_temperature=target_temperature,
            target_unit=target_unit,
        )

        try:
            dispatch_res = await service.dispatch_actuation(
                target_entity_id=validated_id,
                target_temperature=float(target_temperature),
                target_unit=target_unit,
                execution_id=context.execution_id,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Climate actuation dispatch rejected or failed: {str(exc)}",
                evidence={"dispatch_attempted": True, "error": str(exc)},
            )

        dispatch_status = dispatch_res.get("dispatch_status", "unknown")

        if dispatch_status == "blocked_precondition":
            reason = dispatch_res.get("reason", "Precondition could not be established.")
            output = HomeAssistantClimateActuationOutput(
                success=False,
                entity_id=validated_id,
                requested_temperature=float(target_temperature),
                requested_unit=target_unit,
                reported_temperature=dispatch_res.get("reported_temperature"),
                reported_mode=dispatch_res.get("reported_mode"),
                dispatch_status="blocked_precondition",
                verification_status="blocked",
                verification_details={"reason": reason, "dispatch": dispatch_res},
                message=f"Climate entity '{validated_id}' actuation blocked: {reason}",
                physical_device_connectivity_claimed=False,
            )
            return ExecutionResult(
                success=False,
                output_data=output.model_dump(),
                error_message=f"Climate actuation dispatch blocked: {reason}",
                evidence={"dispatch": dispatch_res},
            )

        if dispatch_status == "already_satisfied":
            output = HomeAssistantClimateActuationOutput(
                success=True,
                entity_id=validated_id,
                requested_temperature=float(target_temperature),
                requested_unit=target_unit,
                reported_temperature=dispatch_res.get("reported_temperature"),
                reported_mode=dispatch_res.get("reported_mode"),
                dispatch_status="already_satisfied",
                verification_status="pending",
                verification_details={},
                message=f"Climate entity '{validated_id}' already at requested target temperature {target_temperature}. Pending independent verification.",
                physical_device_connectivity_claimed=False,
            )
            return ExecutionResult(
                success=True,
                output_data=output.model_dump(),
                evidence={"dispatch": dispatch_res},
            )

        output = HomeAssistantClimateActuationOutput(
            success=True,
            entity_id=validated_id,
            requested_temperature=float(target_temperature),
            requested_unit=target_unit,
            reported_temperature=dispatch_res.get("reported_temperature"),
            reported_mode=dispatch_res.get("reported_mode"),
            dispatch_status=dispatch_status,
            verification_status="pending",
            verification_details={},
            message=f"Climate entity '{validated_id}' service dispatch '{dispatch_status}'. Pending independent verification.",
            physical_device_connectivity_claimed=False,
        )

        return ExecutionResult(
            success=True,
            output_data=output.model_dump(),
            evidence={"dispatch": dispatch_res},
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        try:
            self.output_model.model_validate(output_data)

            # Invariant: Never claim physical device connectivity
            if output_data.get("physical_device_connectivity_claimed") is True:
                return VerificationResult(
                    passed=False,
                    reason="Capability erroneously claimed physical device connectivity.",
                )

            if output_data.get("dispatch_status") == "blocked_precondition":
                return VerificationResult(
                    passed=False,
                    reason="Capability dispatch was blocked at precondition check.",
                    details=output_data.get("verification_details", {}),
                )

            service = self._get_service()
            target_temperature = input_data.get("target_temperature")
            target_entity_id = input_data.get("target_entity_id")
            target_unit = input_data.get("target_unit")

            ver_res = await service.verify_state(
                target_entity_id=target_entity_id,
                expected_temperature=float(target_temperature),
                target_unit=target_unit,
            )

            output_data["verification_status"] = "verified" if ver_res.passed else "failed"
            output_data["reported_temperature"] = ver_res.details.get("reported_temperature")
            output_data["verification_details"] = ver_res.details
            output_data["success"] = ver_res.passed
            unit_str = f" {target_unit}" if target_unit else ""
            output_data["message"] = (
                f"Climate entity '{output_data['entity_id']}' verified at target temperature {target_temperature}{unit_str}."
                if ver_res.passed
                else f"Climate entity '{output_data['entity_id']}' actuation verification failed: {ver_res.reason}"
            )

            if not ver_res.passed:
                return VerificationResult(
                    passed=False,
                    reason=ver_res.reason,
                    details=ver_res.details,
                )

            return VerificationResult(
                passed=True,
                details=ver_res.details,
            )
        except Exception as exc:
            return VerificationResult(
                passed=False,
                reason=f"Independent verifier failed schema or semantic check: {str(exc)}",
                details={"error": str(exc)},
            )
