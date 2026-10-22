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
from pico.core.id_gen import generate_id
from pico.providers.types import ChatMessage, GenerationOptions
from pico.scheduler import (
    PriorityClass,
    ResourceScheduler,
    SchedulingRequest,
    WorkerRole,
    get_scheduler,
)
from pico.services.routing_service import RoutingService


class ModelInferInput(BaseModel):
    """Input parameters for bounded, scheduled model inference."""
    prompt: str = Field(..., min_length=1, max_length=10000, description="Prompt text")
    system_prompt: str | None = Field(default=None, max_length=5000, description="Optional system guidance")
    role: str = Field(
        default="executor",
        description="Logical worker role: planner, researcher, executor, summarizer, routine",
    )
    priority: str = Field(
        default="workflow",
        description="Priority class: workflow, background, interactive",
    )
    max_tokens: int | None = Field(default=512, ge=1, le=2048, description="Max generated tokens")
    temperature: float | None = Field(default=0.7, ge=0.0, le=2.0, description="Sampling temperature")
    timeout_seconds: float | None = Field(default=60.0, ge=1.0, le=300.0, description="Inference timeout")


class ModelInferOutput(BaseModel):
    """Output generated from scheduled model inference."""
    response: str
    role: str
    priority: str
    provider: str
    model_name: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float | None = None


class ModelInferCapability(BaseCapability):
    """Capability allowing workflow steps and logical workers to execute bounded, scheduled inference."""

    metadata = CapabilityMetadata(
        capability_id="model.infer",
        version="1.0.0",
        name="Model Inference",
        description="Execute bounded model inference through PICO's single-model resource scheduler.",
        risk_tier=RiskTier.READ_ONLY,
        consequential=False,
        idempotent=False,
        timeout_seconds=65.0,
        max_output_bytes=65536,
    )
    input_model = ModelInferInput
    output_model = ModelInferOutput

    def __init__(self, scheduler: ResourceScheduler | None = None) -> None:
        self._scheduler = scheduler

    def _get_scheduler(self) -> ResourceScheduler:
        return self._scheduler or get_scheduler()

    async def execute(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        parsed = ModelInferInput(**input_data)

        # Build ChatMessages preserving caller classification
        messages: list[ChatMessage] = []
        if parsed.system_prompt:
            messages.append(
                ChatMessage(
                    role="system",
                    content=parsed.system_prompt,
                    data_classification=context.context_classification,
                )
            )
        messages.append(
            ChatMessage(
                role="user",
                content=parsed.prompt,
                data_classification=context.context_classification,
            )
        )

        # Determine route using central policy
        router = RoutingService()
        decision = router.resolve_route(
            task_category="general_chat",
            data_classification=context.context_classification,
        )

        # Map role and priority safely to valid enums
        valid_roles = {r.value: r for r in WorkerRole}
        role_enum = valid_roles.get(parsed.role.lower(), WorkerRole.EXECUTOR)

        valid_priorities = {p.value: p for p in PriorityClass}
        prio_enum = valid_priorities.get(parsed.priority.lower(), PriorityClass.WORKFLOW)

        # Estimate prompt tokens conservatively
        est_tokens = max(1, len(parsed.prompt) // 4)
        if parsed.system_prompt:
            est_tokens += max(1, len(parsed.system_prompt) // 4)

        sched_req = SchedulingRequest(
            request_id=generate_id("sched"),
            role=role_enum,
            priority=prio_enum,
            provider=decision.provider,
            model_name=decision.model_name,
            task_category=decision.task_category,
            data_classification=decision.data_classification,
            prompt_tokens=est_tokens,
            max_tokens=parsed.max_tokens,
            timeout_seconds=parsed.timeout_seconds,
            workflow_run_id=context.task_id or context.correlation_id,
            correlation_id=context.correlation_id,
        )

        options = GenerationOptions(
            temperature=parsed.temperature,
            max_tokens=parsed.max_tokens,
            stream=False,
        )

        override_provider = None
        try:
            from pico.main import app
            override_provider = getattr(app.state, "provider_override", None)
        except Exception:
            pass

        scheduler = self._get_scheduler()
        try:
            response_text, done_event = await scheduler.schedule_complete(
                request=sched_req,
                messages=messages,
                options=options,
                override_provider=override_provider,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_message=f"Model inference failed: {exc}",
                evidence={"request_id": sched_req.request_id},
            )

        return ExecutionResult(
            success=True,
            output_data={
                "response": response_text,
                "role": role_enum.value,
                "priority": prio_enum.value,
                "provider": decision.provider,
                "model_name": decision.model_name,
                "prompt_tokens": done_event.prompt_tokens,
                "completion_tokens": done_event.completion_tokens,
                "latency_ms": done_event.latency_ms,
            },
            evidence={
                "decision_id": decision.decision_id,
                "request_id": sched_req.request_id,
            },
        )

    async def verify(
        self,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        evidence: dict[str, Any],
        context: ExecutionContext,
    ) -> VerificationResult:
        if not output_data or "response" not in output_data:
            return VerificationResult(
                passed=False,
                reason="Model inference output missing 'response' field.",
            )
        return VerificationResult(passed=True)
