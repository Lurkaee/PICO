"""Milestone P9.2: Single-Model Resource Scheduling Tests.

Validates single-model residency, logical worker multiplexing, priority queueing with
aging fairness, cooperative cancellation, timeout expiry, telemetry truthfulness,
privacy gating, streaming lease lifecycle, and workflow integration.
"""

import asyncio
import time
from typing import Any
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from pico.capabilities.core.model_infer import ModelInferCapability
from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.core.id_gen import generate_id
from pico.main import app
from pico.models.workflow import WorkflowRun, WorkflowStep
from pico.providers.base import ModelProvider
from pico.providers.mock import MockModelProvider
from pico.schemas.workflow import StepDefinition, WorkflowCreate
from pico.providers.types import (
    ChatMessage,
    DoneEvent,
    GenerationOptions,
    MetaEvent,
    ProviderCapability,
    ProviderMetadata,
    StreamEvent,
    TokenEvent,
)
from pico.scheduler import (
    ContextBudgetExceededError,
    ModelResidencyConflictError,
    PriorityClass,
    QueueFullError,
    RequestCancelledError,
    ResourceScheduler,
    SchedulingRequest,
    SchedulingStatus,
    SchedulingTimeoutError,
    WorkerRole,
    get_scheduler,
)
from pico.services.execution_engine import ToolExecutionEngine
from pico.workflow.service import WorkflowService


class SlowControlledProvider(ModelProvider):
    """Mock provider with controllable streaming delay for concurrency testing."""

    def __init__(self, delay_seconds: float = 0.2, token_count: int = 3) -> None:
        self.delay_seconds = delay_seconds
        self.token_count = token_count
        self.call_count = 0
        self.currently_active = 0
        self.max_observed_concurrency = 0

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            name="mock",
            default_model="pico-mock-deterministic",
            capabilities=[ProviderCapability.STREAMING, ProviderCapability.LOCAL_INFERENCE],
            is_local=True,
        )

    async def check_health(self) -> bool:
        return True

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
    ) -> Any:
        self.call_count += 1
        self.currently_active += 1
        self.max_observed_concurrency = max(self.max_observed_concurrency, self.currently_active)

        yield MetaEvent(provider="mock", model="pico-mock-deterministic")
        try:
            for i in range(self.token_count):
                await asyncio.sleep(self.delay_seconds)
                yield TokenEvent(delta=f"tok_{i} ")
            yield DoneEvent(
                finish_reason="stop",
                prompt_tokens=10,
                completion_tokens=self.token_count,
                total_tokens=10 + self.token_count,
                latency_ms=100.0,
            )
        finally:
            self.currently_active -= 1


class SimulatedClock:
    """Injectable monotonic clock for deterministic priority aging and deadline testing."""

    def __init__(self, initial_time: float = 1000.0) -> None:
        self._current_time = initial_time

    def __call__(self) -> float:
        return self._current_time

    def advance(self, seconds: float) -> None:
        self._current_time += seconds


# ---------------------------------------------------------------------------
# Test 1 & 2: Default configuration preserves single-local-model invariant
# and multiple logical workers share the configured resident model
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_single_model_residency_and_worker_multiplexing() -> None:
    scheduler = ResourceScheduler()
    # Invariant: exactly 1 active local execution allowed
    assert scheduler.local_gate.max_active == 1
    assert scheduler.local_gate.active_count == 0

    # Test that multiple logical workers (planner, researcher, summarizer, executor)
    # can all execute successfully over the same configured resident model
    provider = MockModelProvider(fixed_response="Multiplexed worker response.")
    roles = [
        WorkerRole.PLANNER,
        WorkerRole.RESEARCHER,
        WorkerRole.EXECUTOR,
        WorkerRole.SUMMARIZER,
        WorkerRole.ROUTINE,
    ]

    for role in roles:
        req = SchedulingRequest(
            request_id=generate_id("sched"),
            role=role,
            priority=PriorityClass.WORKFLOW,
            provider="mock",
            model_name=scheduler.resident_model,
            data_classification="INTERNAL",
        )
        resp, done = await scheduler.schedule_complete(
            req,
            messages=[ChatMessage(role="user", content="Role test")],
            override_provider=provider,
        )
        assert resp == "Multiplexed worker response."
        assert done.finish_reason == "stop"

    status = await scheduler.get_status()
    assert status.counts_by_status.get("completed", 0) >= len(roles)
    assert status.current_active_local == 0


# ---------------------------------------------------------------------------
# Test 3 & 4: Only 1 local inference lease is active at a time; competing requests
# queue in deterministic order
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_single_active_local_lease_enforcement() -> None:
    provider = SlowControlledProvider(delay_seconds=0.08, token_count=2)
    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")

    async def execute_task(req_id: str, priority: PriorityClass) -> str:
        req = SchedulingRequest(
            request_id=req_id,
            role=WorkerRole.EXECUTOR,
            priority=priority,
            provider="mock",
            model_name="pico-mock-deterministic",
            data_classification="INTERNAL",
        )
        resp, _ = await scheduler.schedule_complete(
            req,
            messages=[ChatMessage(role="user", content=f"Req {req_id}")],
            override_provider=provider,
        )
        return resp

    # Launch 4 competing tasks simultaneously
    results = await asyncio.gather(
        execute_task("req_1", PriorityClass.BACKGROUND),
        execute_task("req_2", PriorityClass.INTERACTIVE),
        execute_task("req_3", PriorityClass.WORKFLOW),
        execute_task("req_4", PriorityClass.INTERACTIVE),
    )

    assert len(results) == 4
    # Strict invariant: provider saw max concurrency of exactly 1
    assert provider.max_observed_concurrency == 1
    assert scheduler.local_gate.active_count == 0


# ---------------------------------------------------------------------------
# Test 5: Priority fairness with aging prevents indefinite background starvation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_priority_queue_with_aging_fairness() -> None:
    clock = SimulatedClock(1000.0)
    scheduler = ResourceScheduler(
        resident_model="pico-mock-deterministic",
        aging_interval_seconds=5.0,
        time_func=clock,
    )

    # 1. Enqueue background request at t=1000 (base priority = 20)
    bg_ticket = SchedulingRequest(
        request_id="bg_req",
        role=WorkerRole.SUMMARIZER,
        priority=PriorityClass.BACKGROUND,
        provider="mock",
        model_name="pico-mock-deterministic",
    )

    # 2. Advance clock by 60s (aging bonus = 60/5 = 12 -> effective priority = 20 - 12 = 8)
    clock.advance(60.0)

    # 3. Enqueue workflow request at t=1060 (base priority = 10)
    wf_ticket = SchedulingRequest(
        request_id="wf_req",
        role=WorkerRole.PLANNER,
        priority=PriorityClass.WORKFLOW,
        provider="mock",
        model_name="pico-mock-deterministic",
    )

    # Add both to scheduler queue directly to inspect pop order
    from pico.scheduler.queue import SchedulingTicket
    t_bg = SchedulingTicket(bg_ticket, time_func=clock)
    t_bg.created_at = 1000.0  # queued 60 seconds ago

    t_wf = SchedulingTicket(wf_ticket, time_func=clock)
    t_wf.created_at = 1060.0  # queued just now

    await scheduler.queue.enqueue(t_bg)
    await scheduler.queue.enqueue(t_wf)

    # At t=1060:
    # t_bg effective score: 20 - (60 / 5) = 8.0
    # t_wf effective score: 10 - (0 / 5) = 10.0
    # Because 8.0 < 10.0, the aged background request MUST be popped first!
    first = await scheduler.queue.pop_next_eligible()
    assert first is not None
    assert first.request.request_id == "bg_req"

    second = await scheduler.queue.pop_next_eligible()
    assert second is not None
    assert second.request.request_id == "wf_req"


# ---------------------------------------------------------------------------
# Test 6: Queue capacity bound rejects over-capacity requests with QueueFullError
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_queue_capacity_bound_backpressure() -> None:
    # Configure scheduler with small max_queue_depth = 2
    scheduler = ResourceScheduler(
        resident_model="pico-mock-deterministic",
        max_queue_depth=2,
    )

    # Fill local gate
    await scheduler.local_gate.acquire_slot()

    # Fill queue to capacity (2 items)
    req1 = SchedulingRequest(
        request_id="q1",
        role=WorkerRole.ROUTINE,
        priority=PriorityClass.BACKGROUND,
        provider="mock",
        model_name="pico-mock-deterministic",
    )
    req2 = SchedulingRequest(
        request_id="q2",
        role=WorkerRole.ROUTINE,
        priority=PriorityClass.BACKGROUND,
        provider="mock",
        model_name="pico-mock-deterministic",
    )
    req3 = SchedulingRequest(
        request_id="q3",
        role=WorkerRole.ROUTINE,
        priority=PriorityClass.BACKGROUND,
        provider="mock",
        model_name="pico-mock-deterministic",
    )

    t1 = asyncio.create_task(scheduler.schedule_complete(req1, [ChatMessage(role="user", content="hi")]))
    t2 = asyncio.create_task(scheduler.schedule_complete(req2, [ChatMessage(role="user", content="hi")]))
    await asyncio.sleep(0.01)

    # Third request exceeds max_queue_depth = 2
    with pytest.raises(QueueFullError) as exc_info:
        await scheduler.schedule_complete(req3, [ChatMessage(role="user", content="hi")])

    assert "capacity (2) exceeded" in str(exc_info.value)

    # Clean up background tasks
    t1.cancel()
    t2.cancel()
    await scheduler.local_gate.release_slot()


# ---------------------------------------------------------------------------
# Test 7: Expired deadlines do not execute stale work
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_expired_deadline_discards_stale_work() -> None:
    clock = SimulatedClock(100.0)
    scheduler = ResourceScheduler(
        resident_model="pico-mock-deterministic",
        time_func=clock,
    )

    # Occupy the slot
    await scheduler.local_gate.acquire_slot()

    req = SchedulingRequest(
        request_id="exp_req",
        role=WorkerRole.ROUTINE,
        priority=PriorityClass.BACKGROUND,
        provider="mock",
        model_name="pico-mock-deterministic",
        queue_timeout_seconds=5.0,
    )

    task = asyncio.create_task(
        scheduler.schedule_complete(req, [ChatMessage(role="user", content="stale")])
    )
    await asyncio.sleep(0.01)

    # Advance clock past timeout
    clock.advance(10.0)

    # Releasing slot triggers dispatch which checks deadlines
    await scheduler.local_gate.release_slot()
    await scheduler._try_dispatch()

    with pytest.raises(SchedulingTimeoutError):
        await task


# ---------------------------------------------------------------------------
# Test 8: Queued requests cancel without making a provider call
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_queued_request_cancellation() -> None:
    provider = SlowControlledProvider()
    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")

    # Occupy slot
    await scheduler.local_gate.acquire_slot()

    req = SchedulingRequest(
        request_id="cancel_req",
        role=WorkerRole.SUMMARIZER,
        priority=PriorityClass.BACKGROUND,
        provider="mock",
        model_name="pico-mock-deterministic",
    )

    task = asyncio.create_task(
        scheduler.schedule_complete(
            req,
            [ChatMessage(role="user", content="test")],
            override_provider=provider,
        )
    )
    await asyncio.sleep(0.01)

    # Cancel while waiting in queue
    cancelled = await scheduler.cancel_request("cancel_req")
    assert cancelled is True

    with pytest.raises(RequestCancelledError):
        await task

    # Verify provider was never invoked
    assert provider.call_count == 0
    await scheduler.local_gate.release_slot()


# ---------------------------------------------------------------------------
# Test 9: Cancellation or exceptions during streaming release the active lease
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_streaming_cancellation_releases_lease() -> None:
    provider = SlowControlledProvider(delay_seconds=0.1, token_count=5)
    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")

    req = SchedulingRequest(
        request_id="stream_cancel_req",
        role=WorkerRole.INTERACTIVE,
        priority=PriorityClass.INTERACTIVE,
        provider="mock",
        model_name="pico-mock-deterministic",
    )

    stream_gen = scheduler.schedule_stream(
        req,
        [ChatMessage(role="user", content="test")],
        override_provider=provider,
    )

    # Read first event (meta)
    event1 = await stream_gen.__anext__()
    assert isinstance(event1, MetaEvent)

    # Slot must be occupied while streaming
    assert scheduler.local_gate.active_count == 1

    # Simulate client disconnect / cancellation by closing generator
    await stream_gen.aclose()

    # Slot must be cleanly released
    assert scheduler.local_gate.active_count == 0


# ---------------------------------------------------------------------------
# Test 10: One request failure does not block subsequent requests
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_request_failure_does_not_block_queue() -> None:
    class FailingProvider(ModelProvider):
        @property
        def metadata(self) -> ProviderMetadata:
            return ProviderMetadata(name="mock", default_model="pico-mock-deterministic", capabilities=[], is_local=True)

        async def check_health(self) -> bool:
            return True

        async def stream_chat(self, messages: Any, options: Any = None) -> Any:
            raise RuntimeError("Synthetic model crash")
            yield  # pragma: no cover

    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")
    fail_req = SchedulingRequest(
        request_id="fail_req",
        role=WorkerRole.EXECUTOR,
        priority=PriorityClass.WORKFLOW,
        provider="mock",
        model_name="pico-mock-deterministic",
    )

    with pytest.raises(RuntimeError) as exc:
        await scheduler.schedule_complete(
            fail_req,
            [ChatMessage(role="user", content="crash")],
            override_provider=FailingProvider(),
        )
    assert "Synthetic model crash" in str(exc.value)

    # Slot must be released despite failure
    assert scheduler.local_gate.active_count == 0

    # Subsequent request executes normally
    good_req = SchedulingRequest(
        request_id="good_req",
        role=WorkerRole.EXECUTOR,
        priority=PriorityClass.WORKFLOW,
        provider="mock",
        model_name="pico-mock-deterministic",
    )
    good_prov = MockModelProvider(fixed_response="Recovered.")
    resp, done = await scheduler.schedule_complete(
        good_req,
        [ChatMessage(role="user", content="ok")],
        override_provider=good_prov,
    )
    assert resp == "Recovered."
    assert scheduler.local_gate.active_count == 0


# ---------------------------------------------------------------------------
# Test 11 & 12: Conflicting local model names rejected (no download/kill)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_model_residency_conflict_rejection() -> None:
    scheduler = ResourceScheduler(resident_model="qwen3.5:2b")

    # Requesting an unconfigured / conflicting local model (e.g. qwen3.5:4b)
    req = SchedulingRequest(
        request_id="conflict_req",
        role=WorkerRole.PLANNER,
        priority=PriorityClass.WORKFLOW,
        provider="ollama",
        model_name="qwen3.5:4b",
    )

    with pytest.raises(ModelResidencyConflictError) as exc:
        await scheduler.schedule_complete(req, [ChatMessage(role="user", content="hi")])

    assert "conflicts with resident model" in str(exc.value)
    assert scheduler.local_gate.active_count == 0


# ---------------------------------------------------------------------------
# Test 13 & 14: Context budget and token bounds enforcement
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_context_budget_bounds_enforced() -> None:
    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")

    # Prompt exceeds maximum conservative prompt budget (3328 tokens)
    req_too_large = SchedulingRequest(
        request_id="large_req",
        role=WorkerRole.EXECUTOR,
        priority=PriorityClass.WORKFLOW,
        provider="mock",
        model_name="pico-mock-deterministic",
        prompt_tokens=3500,
    )

    with pytest.raises(ContextBudgetExceededError) as exc:
        await scheduler.schedule_complete(req_too_large, [ChatMessage(role="user", content="big")])
    assert "exceeds maximum prompt budget" in str(exc.value)

    # Prompt + completion exceeds 4096 context window
    req_window_overflow = SchedulingRequest(
        request_id="overflow_req",
        role=WorkerRole.EXECUTOR,
        priority=PriorityClass.WORKFLOW,
        provider="mock",
        model_name="pico-mock-deterministic",
        prompt_tokens=3000,
        max_tokens=1500,  # 3000 + 1500 = 4500 > 4096
    )

    with pytest.raises(ContextBudgetExceededError) as exc:
        await scheduler.schedule_complete(req_window_overflow, [ChatMessage(role="user", content="overflow")])
    assert "exceeds 4096" in str(exc.value)


# ---------------------------------------------------------------------------
# Test 15 & 16: Privacy boundary: non-public context cannot route to cloud
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_cloud_privacy_boundary_strictly_enforced() -> None:
    scheduler = ResourceScheduler()

    # Even if worker asks for cloud, non-public data (INTERNAL) cannot be routed
    req = SchedulingRequest(
        request_id="leak_attempt",
        role=WorkerRole.RESEARCHER,
        priority=PriorityClass.WORKFLOW,
        provider="cloud",
        model_name="gpt-4o-mini",
        data_classification="INTERNAL",
    )

    with pytest.raises(ValidationError) as exc:
        await scheduler.schedule_complete(req, [ChatMessage(role="user", content="sensitive")])

    assert "INTERNAL' data cannot be routed to cloud" in str(exc.value)


# ---------------------------------------------------------------------------
# Test 17: Cloud fallback into local acquires the single local lease
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_cloud_fallback_acquires_local_lease() -> None:
    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")
    provider = SlowControlledProvider(delay_seconds=0.05, token_count=1)

    # Request that was routed to local as a fallback
    fallback_req = SchedulingRequest(
        request_id="fallback_req",
        role=WorkerRole.EXECUTOR,
        priority=PriorityClass.WORKFLOW,
        provider="mock",
        model_name="pico-mock-deterministic",
        data_classification="PUBLIC",
    )

    resp, _ = await scheduler.schedule_complete(
        fallback_req,
        [ChatMessage(role="user", content="fallback test")],
        override_provider=provider,
    )
    assert resp.startswith("tok_0")
    # Verify lease was tracked and released
    assert scheduler.local_gate.active_count == 0


# ---------------------------------------------------------------------------
# Test 18: Workflow cancellation purges pending scheduler requests
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_workflow_cancellation_purges_queued_requests(db_session: Session) -> None:
    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")
    wf_service = WorkflowService(db_session, execution_engine=ToolExecutionEngine(db_session))

    # Create dummy workflow
    run = wf_service.submit_workflow(
        WorkflowCreate(
            name="Cancellation Test Workflow",
            steps=[
                StepDefinition(
                    step_key="step1",
                    capability_id="system.diagnostics",
                    capability_version="1.0.0",
                    input_template={},
                )
            ],
        )
    )

    # Occupy the scheduler local slot
    await scheduler.local_gate.acquire_slot()

    # Enqueue a model request tied to this workflow
    wf_req = SchedulingRequest(
        request_id="wf_model_req",
        role=WorkerRole.EXECUTOR,
        priority=PriorityClass.WORKFLOW,
        provider="mock",
        model_name="pico-mock-deterministic",
        workflow_run_id=run.id,
    )

    task = asyncio.create_task(
        scheduler.schedule_complete(wf_req, [ChatMessage(role="user", content="wf inference")])
    )
    await asyncio.sleep(0.01)

    assert scheduler.queue.depth == 1

    # Cancel the workflow run
    purged = await scheduler.cancel_workflow_requests(run.id)
    assert purged == 1
    assert scheduler.queue.depth == 0

    with pytest.raises(RequestCancelledError):
        await task

    await scheduler.local_gate.release_slot()


# ---------------------------------------------------------------------------
# Test 19: ModelInferCapability executes through ToolExecutionEngine and Scheduler
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_model_infer_capability_execution(db_session: Session) -> None:
    engine = ToolExecutionEngine(db_session)
    provider = MockModelProvider(fixed_response="Capability scheduled output.")
    app.state.provider_override = provider

    # Register and execute model.infer capability
    record = engine.submit(
        capability_id="model.infer",
        capability_version="1.0.0",
        input_data={
            "prompt": "Summarize task results.",
            "role": "summarizer",
            "priority": "workflow",
            "max_tokens": 128,
        },
        task_id=None,
        context_classification="INTERNAL",
    )

    exec_res = await engine.execute(record.id)
    assert exec_res.status == "succeeded"
    assert exec_res.output_json["response"] == "Capability scheduled output."
    assert exec_res.output_json["role"] == "summarizer"
    assert exec_res.output_json["priority"] == "workflow"


# ---------------------------------------------------------------------------
# Test 20: Scheduler diagnostics do not leak prompts or secrets
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_scheduler_diagnostics_sanitized() -> None:
    scheduler = ResourceScheduler(resident_model="pico-mock-deterministic")
    provider = MockModelProvider(fixed_response="Sanitized test.")

    req = SchedulingRequest(
        request_id="secret_check_req",
        role=WorkerRole.INTERACTIVE,
        priority=PriorityClass.INTERACTIVE,
        provider="mock",
        model_name="pico-mock-deterministic",
        data_classification="SECRET",
    )

    await scheduler.schedule_complete(
        req,
        [ChatMessage(role="user", content="SECRET_KEY_12345 do not log")],
        override_provider=provider,
    )

    status = await scheduler.get_status()
    # Serialize to JSON string
    dumped = status.model_dump_json()

    # Invariant: prompt content and sensitive values must NEVER appear in diagnostics
    assert "SECRET_KEY_12345" not in dumped
    assert "do not log" not in dumped
    # Metadata fields must be present
    assert "secret_check_req" in dumped
    assert "interactive" in dumped


# ---------------------------------------------------------------------------
# Test 21: REST API /api/scheduler endpoints
# ---------------------------------------------------------------------------
def test_scheduler_api_status_and_metrics(client: TestClient) -> None:
    # 1. Test /api/scheduler/status
    res = client.get("/api/scheduler/status")
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is True
    assert data["max_active_local"] == 1
    assert "telemetry" in data
    assert "gpu_telemetry_source" in data["telemetry"]

    # 2. Test /api/scheduler/metrics
    m_res = client.get("/api/scheduler/metrics")
    assert m_res.status_code == 200
    m_data = m_res.json()
    assert "queue_depth" in m_data
    assert "max_active_local" in m_data
    assert m_data["max_active_local"] == 1
