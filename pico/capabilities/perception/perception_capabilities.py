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
from pico.perception.pipeline import PerceptionPipeline


# 1. Perception Status Capability
class PerceptionStatusInput(BaseModel):
    check_adapters: bool = True


class PerceptionStatusOutput(BaseModel):
    overall_ready: bool
    audio_enabled: bool
    microphone_active: bool
    vision_enabled: bool
    motion_enabled: bool
    environmental_enabled: bool
    active_sensor_count: int


class PerceptionStatusCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="perception.status",
        version="1.0.0",
        name="Perception Subsystem Status",
        description="Inspect readiness and availability states across all four perception modalities.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="perception:read",
        required_classification="INTERNAL",
        timeout_seconds=3.0,
        max_output_bytes=4096,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = PerceptionStatusInput
    output_model = PerceptionStatusOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        pipeline = PerceptionPipeline(db=context.db)
        status = pipeline.get_status()
        return ExecutionResult(
            success=True,
            output_data={
                "overall_ready": status.overall_ready,
                "audio_enabled": status.audio.get("enabled", False),
                "microphone_active": status.audio.get("microphone_active", False),
                "vision_enabled": status.vision.get("enabled", False),
                "motion_enabled": status.motion.get("enabled", False),
                "environmental_enabled": status.environmental.get("enabled", False),
                "active_sensor_count": status.environmental.get("active_sensor_count", 0),
            },
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
        return VerificationResult(passed=True, reason="Perception status verified successfully.")


# 2. Perception Get Recent Events Capability
class PerceptionGetRecentEventsInput(BaseModel):
    seconds: float = Field(default=300.0, ge=1.0, le=86400.0)
    modality: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class PerceptionGetRecentEventsOutput(BaseModel):
    count: int
    events: list[dict[str, Any]]


class PerceptionGetRecentEventsCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="perception.get_recent_events",
        version="1.0.0",
        name="Get Recent Perception Events",
        description="Retrieve scrubbed, normalized perception events recorded within a recent time window.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="perception:read",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=16384,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = PerceptionGetRecentEventsInput
    output_model = PerceptionGetRecentEventsOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        pipeline = PerceptionPipeline(db=context.db)
        if not pipeline.repo:
            return ExecutionResult(success=True, output_data={"count": 0, "events": []})

        recent = pipeline.repo.get_recent(
            seconds=input_data.get("seconds", 300.0),
            modality=input_data.get("modality"),
            limit=input_data.get("limit", 10),
        )
        items = [
            {
                "event_id": r.id,
                "modality": r.modality,
                "event_type": r.event_type,
                "source_device_id": r.source_device_id,
                "data_classification": r.data_classification,
                "confidence": r.confidence,
                "created_at": r.created_at.isoformat(),
            }
            for r in recent
        ]
        return ExecutionResult(
            success=True,
            output_data={"count": len(items), "events": items},
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
        count = output_data.get("count", 0)
        events = output_data.get("events", [])
        if count != len(events):
            return VerificationResult(passed=False, reason="Count mismatch in event payload.")
        return VerificationResult(passed=True, reason="Recent events verified successfully.")


# 3. Perception Validate Sensor Payload Capability
class PerceptionValidateSensorPayloadInput(BaseModel):
    source_device: str
    protocol: str = "mqtt"
    temperature_c: float | None = None
    humidity_percent: float | None = None
    ambient_light_lux: float | None = None
    contact_state: str | None = None
    air_quality_aqi: int | None = None


class PerceptionValidateSensorPayloadOutput(BaseModel):
    is_valid: bool
    reason: str | None = None
    normalized_metrics: dict[str, Any]


class PerceptionValidateSensorPayloadCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="perception.validate_sensor_payload",
        version="1.0.0",
        name="Validate Environmental Sensor Payload",
        description="Validates sensor readings against physical unit ranges and schema constraints.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="perception:validate",
        required_classification="INTERNAL",
        timeout_seconds=2.0,
        max_output_bytes=4096,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = PerceptionValidateSensorPayloadInput
    output_model = PerceptionValidateSensorPayloadOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        metrics = {
            "temperature_c": input_data.get("temperature_c"),
            "humidity_percent": input_data.get("humidity_percent"),
            "ambient_light_lux": input_data.get("ambient_light_lux"),
            "contact_state": input_data.get("contact_state"),
            "air_quality_aqi": input_data.get("air_quality_aqi"),
        }
        active = {k: v for k, v in metrics.items() if v is not None}
        if not active:
            return ExecutionResult(
                success=True,
                output_data={
                    "is_valid": False,
                    "reason": "Payload contains no metric values.",
                    "normalized_metrics": {},
                },
            )

        # Range checks
        if "temperature_c" in active and not (-50.0 <= active["temperature_c"] <= 100.0):
            return ExecutionResult(
                success=True,
                output_data={
                    "is_valid": False,
                    "reason": "Temperature out of realistic bounds (-50 to 100 C).",
                    "normalized_metrics": active,
                },
            )

        if "humidity_percent" in active and not (0.0 <= active["humidity_percent"] <= 100.0):
            return ExecutionResult(
                success=True,
                output_data={
                    "is_valid": False,
                    "reason": "Humidity out of bounds (0 to 100%).",
                    "normalized_metrics": active,
                },
            )

        if "contact_state" in active and active["contact_state"].lower() not in ("open", "closed"):
            return ExecutionResult(
                success=True,
                output_data={
                    "is_valid": False,
                    "reason": "Contact state must be 'open' or 'closed'.",
                    "normalized_metrics": active,
                },
            )

        return ExecutionResult(
            success=True,
            output_data={
                "is_valid": True,
                "reason": None,
                "normalized_metrics": active,
            },
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
        return VerificationResult(passed=True, reason="Sensor validation verified.")


# 4. Perception Analyze Motion Capability
class PerceptionAnalyzeMotionInput(BaseModel):
    source_device: str = "cam:motion_eval"
    frame_a_base64: str
    frame_b_base64: str
    sensitivity_threshold: float = Field(default=0.05, ge=0.01, le=0.5)


class PerceptionAnalyzeMotionOutput(BaseModel):
    motion_detected: bool
    direction: str
    magnitude: str
    delta_pixel_ratio: float


class PerceptionAnalyzeMotionCapability(BaseCapability):
    metadata = CapabilityMetadata(
        capability_id="perception.analyze_motion",
        version="1.0.0",
        name="Analyze Motion Differencing",
        description="Performs deterministic frame differencing between two frames to determine movement characteristics.",
        risk_tier=RiskTier.READ_ONLY,
        required_permission="perception:analyze",
        required_classification="INTERNAL",
        timeout_seconds=5.0,
        max_output_bytes=4096,
        idempotent=True,
        max_retries=1,
        consequential=False,
    )
    input_model = PerceptionAnalyzeMotionInput
    output_model = PerceptionAnalyzeMotionOutput

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        from pico.perception.contracts import MovementInterpretationRequest
        pipeline = PerceptionPipeline(db=context.db)
        req = MovementInterpretationRequest(
            source_device=input_data.get("source_device", "cam:motion_eval"),
            frame_a_base64=input_data["frame_a_base64"],
            frame_b_base64=input_data["frame_b_base64"],
            sensitivity_threshold=input_data.get("sensitivity_threshold", 0.05),
        )
        res = pipeline.interpret_movement(req)
        return ExecutionResult(
            success=True,
            output_data={
                "motion_detected": res.motion_detected,
                "direction": res.direction.value,
                "magnitude": res.magnitude.value,
                "delta_pixel_ratio": res.delta_pixel_ratio,
            },
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
        delta = output_data.get("delta_pixel_ratio", 0.0)
        motion = output_data.get("motion_detected", False)
        threshold = input_data.get("sensitivity_threshold", 0.05)
        if motion != (delta >= threshold):
            return VerificationResult(passed=False, reason="Inconsistent motion detection vs delta ratio.")
        return VerificationResult(passed=True, reason="Motion analysis verified independently.")
