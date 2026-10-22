import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
import time
from typing import Any

from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.core.id_gen import generate_id
from pico.providers.base import ModelProvider
from pico.providers.factory import ProviderFactory
from pico.providers.types import ChatMessage, DoneEvent, GenerationOptions, StreamEvent, TokenEvent
from pico.scheduler.admission import CloudAdmissionGate, LocalAdmissionGate
from pico.scheduler.errors import (
    ContextBudgetExceededError,
    QueueFullError,
    RequestCancelledError,
    SchedulingTimeoutError,
)
from pico.scheduler.queue import PriorityFairQueue, SchedulingTicket
from pico.scheduler.telemetry import capture_scheduler_telemetry
from pico.scheduler.types import (
    ExecutionLease,
    SchedulerStatus,
    SchedulingOutcomeSummary,
    SchedulingRequest,
    SchedulingStatus,
    WorkerRole,
)


class ResourceScheduler:
    """Provider-neutral model execution scheduler enforcing single-model local residency,

    fair bounded queueing, and strict privacy invariants.
    """

    def __init__(
        self,
        resident_model: str | None = None,
        max_queue_depth: int | None = None,
        max_active_local: int | None = None,
        max_active_cloud: int | None = None,
        aging_interval_seconds: float | None = None,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self._time_func = time_func or time.perf_counter
        self.max_queue_depth = max_queue_depth or getattr(settings, "SCHEDULER_MAX_QUEUE_DEPTH", 50)
        self.aging_interval_seconds = aging_interval_seconds or getattr(
            settings, "SCHEDULER_AGING_INTERVAL_SECONDS", 5.0
        )

        effective_resident = (
            resident_model
            or (settings.OLLAMA_MODEL if settings.DEFAULT_PROVIDER == "ollama" else "pico-mock-deterministic")
        )
        self.local_gate = LocalAdmissionGate(
            resident_model=effective_resident,
            max_active=max_active_local or getattr(settings, "SCHEDULER_MAX_ACTIVE_LOCAL", 1),
        )
        self.cloud_gate = CloudAdmissionGate(
            max_active=max_active_cloud or getattr(settings, "SCHEDULER_MAX_ACTIVE_CLOUD", 5),
        )
        self.queue = PriorityFairQueue(
            max_depth=self.max_queue_depth,
            aging_interval_seconds=self.aging_interval_seconds,
            time_func=self._time_func,
        )

        self._recent_outcomes: deque[SchedulingOutcomeSummary] = deque(maxlen=20)
        self._counts_by_status: dict[str, int] = {
            s.value: 0 for s in SchedulingStatus
        }
        self._dispatch_lock = asyncio.Lock()
        self.enabled = getattr(settings, "SCHEDULER_ENABLED", True)

    @property
    def resident_model(self) -> str:
        return self.local_gate.resident_model

    def validate_request(self, request: SchedulingRequest) -> None:
        """Validate request invariants before admission into queue."""
        # Context token budget check (4096 tokens total window, conservative prompt budget 3328)
        prompt_budget = getattr(settings, "SCHEDULER_PROMPT_BUDGET_TOKENS", 3328)
        if request.prompt_tokens is not None and request.prompt_tokens > prompt_budget:
            raise ContextBudgetExceededError(
                f"Prompt token count ({request.prompt_tokens}) exceeds maximum prompt budget ({prompt_budget}). "
                f"Active context limit is {settings.OLLAMA_CONTEXT_WINDOW} tokens.",
                details={"prompt_tokens": request.prompt_tokens, "prompt_budget": prompt_budget},
            )

        if (
            request.prompt_tokens is not None
            and request.max_tokens is not None
            and (request.prompt_tokens + request.max_tokens) > settings.OLLAMA_CONTEXT_WINDOW
        ):
            raise ContextBudgetExceededError(
                f"Total estimated tokens ({request.prompt_tokens} + {request.max_tokens}) "
                f"exceeds {settings.OLLAMA_CONTEXT_WINDOW} token context window.",
                details={
                    "prompt_tokens": request.prompt_tokens,
                    "max_tokens": request.max_tokens,
                    "context_window": settings.OLLAMA_CONTEXT_WINDOW,
                },
            )

        # Provider routing & residency check
        if request.provider == "cloud":
            self.cloud_gate.validate_policy(request)
        else:
            self.local_gate.validate_residency(request)

    async def _try_dispatch(self) -> None:
        """Try assigning available execution slots to eligible queued tickets."""
        async with self._dispatch_lock:
            # 1. Dispatch local tickets if local gate has capacity
            while await self.local_gate.can_admit():
                ticket = await self.queue.pop_next_eligible(
                    predicate=lambda r: r.provider != "cloud"
                )
                if not ticket:
                    break
                await self.local_gate.acquire_slot()
                lease = ExecutionLease(
                    lease_id=generate_id("lease"),
                    request_id=ticket.request.request_id,
                    provider=ticket.request.provider,
                    model_name=ticket.request.model_name,
                    is_local=True,
                )
                if not ticket.future.done():
                    ticket.future.set_result(lease)

            # 2. Dispatch cloud tickets if cloud gate has capacity
            while await self.cloud_gate.can_admit():
                ticket = await self.queue.pop_next_eligible(
                    predicate=lambda r: r.provider == "cloud"
                )
                if not ticket:
                    break
                await self.cloud_gate.acquire_slot()
                lease = ExecutionLease(
                    lease_id=generate_id("lease"),
                    request_id=ticket.request.request_id,
                    provider=ticket.request.provider,
                    model_name=ticket.request.model_name,
                    is_local=False,
                )
                if not ticket.future.done():
                    ticket.future.set_result(lease)

    async def _acquire_lease(self, request: SchedulingRequest) -> tuple[ExecutionLease, float]:
        """Validate, enqueue, and await an execution lease."""
        try:
            self.validate_request(request)
        except Exception as exc:
            self._record_outcome(
                request=request,
                status=SchedulingStatus.REJECTED,
                error_message=str(exc),
            )
            raise

        queue_start = self._time_func()
        ticket = SchedulingTicket(request, time_func=self._time_func)

        try:
            await self.queue.enqueue(ticket)
        except QueueFullError as exc:
            self._record_outcome(
                request=request,
                status=SchedulingStatus.REJECTED,
                error_message=str(exc),
            )
            raise

        self._counts_by_status[SchedulingStatus.QUEUED.value] += 1
        await self._try_dispatch()

        try:
            effective_timeout = request.queue_timeout_seconds or request.timeout_seconds
            if effective_timeout is not None:
                lease = await asyncio.wait_for(ticket.future, timeout=effective_timeout)
            else:
                lease = await ticket.future
            return lease, queue_start
        except asyncio.TimeoutError:
            ticket.cancelled = True
            await self.queue.remove(ticket)
            queue_wait_ms = (self._time_func() - queue_start) * 1000.0
            self._record_outcome(
                request=request,
                status=SchedulingStatus.TIMED_OUT,
                queue_wait_ms=queue_wait_ms,
                error_message=f"Request '{request.request_id}' timed out waiting in queue.",
            )
            raise SchedulingTimeoutError(
                f"Scheduling ticket '{request.request_id}' timed out waiting in queue."
            )
        except (asyncio.CancelledError, GeneratorExit):
            ticket.cancelled = True
            await self.queue.remove(ticket)
            if ticket.future.done() and not ticket.future.cancelled():
                try:
                    orphaned_lease = ticket.future.result()
                    await self._release_lease(
                        orphaned_lease,
                        status=SchedulingStatus.CANCELLED,
                        request=request,
                    )
                except Exception:
                    pass
            queue_wait_ms = (self._time_func() - queue_start) * 1000.0
            self._record_outcome(
                request=request,
                status=SchedulingStatus.CANCELLED,
                queue_wait_ms=queue_wait_ms,
                error_message="Cancelled by caller while waiting in queue.",
            )
            raise RequestCancelledError(
                f"Request '{request.request_id}' was cancelled while waiting in queue."
            )
        except Exception as exc:
            ticket.cancelled = True
            await self.queue.remove(ticket)
            queue_wait_ms = (self._time_func() - queue_start) * 1000.0
            self._record_outcome(
                request=request,
                status=SchedulingStatus.FAILED,
                queue_wait_ms=queue_wait_ms,
                error_message=str(exc),
            )
            raise

    async def _release_lease(
        self,
        lease: ExecutionLease,
        status: SchedulingStatus,
        queue_wait_ms: float = 0.0,
        infer_duration_ms: float = 0.0,
        completion_tokens: int | None = None,
        error_message: str | None = None,
        request: SchedulingRequest | None = None,
    ) -> None:
        """Release admission slot, record telemetry/outcome, and trigger next dispatch."""
        if lease.is_local:
            await self.local_gate.release_slot()
        else:
            await self.cloud_gate.release_slot()

        if request is not None:
            self._record_outcome(
                request=request,
                status=status,
                queue_wait_ms=queue_wait_ms,
                infer_duration_ms=infer_duration_ms,
                completion_tokens=completion_tokens,
                error_message=error_message,
            )

        # Wake next queued candidate
        await self._try_dispatch()

    def _record_outcome(
        self,
        request: SchedulingRequest,
        status: SchedulingStatus,
        queue_wait_ms: float = 0.0,
        infer_duration_ms: float = 0.0,
        completion_tokens: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Record sanitized diagnostic outcome (no prompts or secrets stored)."""
        self._counts_by_status[status.value] = self._counts_by_status.get(status.value, 0) + 1
        summary = SchedulingOutcomeSummary(
            request_id=request.request_id,
            role=request.role,
            priority=request.priority,
            provider=request.provider,
            model_name=request.model_name,
            data_classification=request.data_classification,
            status=status,
            queue_wait_ms=round(queue_wait_ms, 2),
            inference_duration_ms=round(infer_duration_ms, 2),
            completion_tokens=completion_tokens,
            error_message=error_message,
            workflow_run_id=request.workflow_run_id,
            step_key=request.step_key,
        )
        self._recent_outcomes.append(summary)

    async def schedule_stream(
        self,
        request: SchedulingRequest,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
        override_provider: ModelProvider | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Schedule and stream model inference while holding the admission lease throughout."""
        lease, queue_start = await self._acquire_lease(request)
        queue_wait_ms = max(0.0, (self._time_func() - queue_start) * 1000.0)
        infer_start = self._time_func()
        status = SchedulingStatus.COMPLETED
        completion_tokens: int | None = None
        error_msg: str | None = None

        try:
            provider = ProviderFactory.get_provider(
                provider_name=request.provider,
                model_name=request.model_name,
                override_instance=override_provider,
            )

            async for event in provider.stream_chat(messages, options):
                if isinstance(event, DoneEvent):
                    completion_tokens = event.completion_tokens
                yield event

        except (asyncio.CancelledError, GeneratorExit):
            status = SchedulingStatus.CANCELLED
            error_msg = "Inference cancelled during streaming."
            raise
        except Exception as exc:
            status = SchedulingStatus.FAILED
            error_msg = str(exc)
            raise
        finally:
            infer_duration_ms = max(0.0, (self._time_func() - infer_start) * 1000.0)
            await self._release_lease(
                lease=lease,
                status=status,
                queue_wait_ms=queue_wait_ms,
                infer_duration_ms=infer_duration_ms,
                completion_tokens=completion_tokens,
                error_message=error_msg,
                request=request,
            )

    async def schedule_complete(
        self,
        request: SchedulingRequest,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
        override_provider: ModelProvider | None = None,
    ) -> tuple[str, DoneEvent]:
        """Helper to collect a complete response through the scheduler."""
        collected: list[str] = []
        final_done = DoneEvent(finish_reason="stop")
        async for event in self.schedule_stream(request, messages, options, override_provider):
            if isinstance(event, TokenEvent):
                collected.append(event.delta)
            elif isinstance(event, DoneEvent):
                final_done = event
        return "".join(collected), final_done

    async def cancel_request(self, request_id: str) -> bool:
        """Cancel a queued request."""
        return await self.queue.cancel_ticket(request_id)

    async def cancel_workflow_requests(self, workflow_run_id: str) -> int:
        """Cancel all queued requests belonging to a workflow run."""
        return await self.queue.cancel_by_workflow(workflow_run_id)

    def cancel_workflow_requests_sync(self, workflow_run_id: str) -> int:
        """Cancel all queued requests belonging to a workflow run synchronously."""
        return self.queue.cancel_by_workflow_sync(workflow_run_id)

    async def get_status(self) -> SchedulerStatus:
        """Return operational diagnostics and truthful hardware telemetry."""
        queue_by_priority = await self.queue.get_counts_by_priority()
        telemetry = capture_scheduler_telemetry()

        return SchedulerStatus(
            enabled=self.enabled,
            active_local_profile=(
                "local-small" if settings.DEFAULT_PROVIDER != "mock" else "mock"
            ),
            active_local_model=self.local_gate.resident_model,
            max_active_local=self.local_gate.max_active,
            current_active_local=self.local_gate.active_count,
            max_active_cloud=self.cloud_gate.max_active,
            current_active_cloud=self.cloud_gate.active_count,
            queue_depth=self.queue.depth,
            max_queue_depth=self.max_queue_depth,
            queue_by_priority=queue_by_priority,
            counts_by_status=dict(self._counts_by_status),
            telemetry=telemetry,
            recent_outcomes=list(self._recent_outcomes),
        )


# Global singleton scheduler instance
_global_scheduler: ResourceScheduler | None = None


def get_scheduler() -> ResourceScheduler:
    """Retrieve or initialize the global ResourceScheduler singleton."""
    global _global_scheduler
    if _global_scheduler is None:
        _global_scheduler = ResourceScheduler()
    return _global_scheduler
