import asyncio
import threading
import time
from collections.abc import Callable
from pico.scheduler.errors import QueueFullError, RequestCancelledError, SchedulingTimeoutError
from pico.scheduler.types import PRIORITY_WEIGHTS, ExecutionLease, PriorityClass, SchedulingRequest


def _safe_fail_future(future: asyncio.Future, exc: Exception) -> None:
    """Safely resolve an asyncio future with an exception across sync/async contexts."""
    if not future.done():
        try:
            loop = future.get_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(future.set_exception, exc)
            else:
                future.set_exception(exc)
        except Exception:
            try:
                future.set_exception(exc)
            except Exception:
                pass


class SchedulingTicket:
    """Internal scheduling ticket tracking state in the queue."""

    def __init__(self, request: SchedulingRequest, time_func: Callable[[], float] | None = None) -> None:
        self.request = request
        self._time_func = time_func or time.perf_counter
        self.created_at = self._time_func()
        try:
            loop = asyncio.get_running_loop()
            self.future: asyncio.Future[ExecutionLease] = loop.create_future()
        except RuntimeError:
            self.future: asyncio.Future[ExecutionLease] = asyncio.Future()
        self.cancelled = False


class PriorityFairQueue:
    """Bounded, deadline-aware priority queue with priority aging to prevent starvation."""

    def __init__(
        self,
        max_depth: int = 50,
        aging_interval_seconds: float = 5.0,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self.max_depth = max_depth
        self.aging_interval_seconds = aging_interval_seconds
        self._time_func = time_func or time.perf_counter
        self._tickets: list[SchedulingTicket] = []
        self._lock = threading.RLock()

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._tickets)

    async def enqueue(self, ticket: SchedulingTicket) -> None:
        """Enqueue ticket if within capacity, otherwise raise QueueFullError."""
        with self._lock:
            if len(self._tickets) >= self.max_depth:
                raise QueueFullError(
                    f"Scheduler queue capacity ({self.max_depth}) exceeded. Request rejected with backpressure.",
                    details={"queue_depth": len(self._tickets), "max_depth": self.max_depth},
                )
            self._tickets.append(ticket)

    async def remove(self, ticket: SchedulingTicket) -> bool:
        """Remove a ticket from the queue."""
        with self._lock:
            if ticket in self._tickets:
                self._tickets.remove(ticket)
                return True
            return False

    def cancel_ticket_sync(self, request_id: str) -> bool:
        """Cancel a queued ticket by request_id (synchronous safe)."""
        with self._lock:
            for ticket in self._tickets:
                if ticket.request.request_id == request_id:
                    ticket.cancelled = True
                    self._tickets.remove(ticket)
                    _safe_fail_future(
                        ticket.future,
                        RequestCancelledError(f"Request '{request_id}' was cancelled by caller."),
                    )
                    return True
            return False

    async def cancel_ticket(self, request_id: str) -> bool:
        """Cancel a queued ticket by request_id."""
        return self.cancel_ticket_sync(request_id)

    def cancel_by_workflow_sync(self, workflow_run_id: str) -> int:
        """Cancel all queued tickets associated with a workflow run (synchronous safe)."""
        cancelled_count = 0
        with self._lock:
            remaining: list[SchedulingTicket] = []
            for ticket in self._tickets:
                if ticket.request.workflow_run_id == workflow_run_id:
                    ticket.cancelled = True
                    cancelled_count += 1
                    _safe_fail_future(
                        ticket.future,
                        RequestCancelledError(
                            f"Workflow '{workflow_run_id}' was cancelled; pending model ticket discarded."
                        ),
                    )
                else:
                    remaining.append(ticket)
            self._tickets = remaining
        return cancelled_count

    async def cancel_by_workflow(self, workflow_run_id: str) -> int:
        """Cancel all queued tickets associated with a workflow run."""
        return self.cancel_by_workflow_sync(workflow_run_id)

    async def pop_next_eligible(
        self,
        predicate: Callable[[SchedulingRequest], bool] | None = None,
    ) -> SchedulingTicket | None:
        """Select and remove the highest priority eligible ticket, applying priority aging and deadline checks."""
        with self._lock:
            if not self._tickets:
                return None

            now = self._time_func()
            remaining: list[SchedulingTicket] = []
            expired: list[SchedulingTicket] = []
            eligible: list[tuple[float, float, SchedulingTicket]] = []

            for ticket in self._tickets:
                if ticket.cancelled:
                    continue

                wait_sec = max(0.0, now - ticket.created_at)

                # Check queue timeout or overall deadline
                if ticket.request.queue_timeout_seconds is not None and wait_sec > ticket.request.queue_timeout_seconds:
                    expired.append(ticket)
                    continue
                if ticket.request.timeout_seconds is not None and wait_sec > ticket.request.timeout_seconds:
                    expired.append(ticket)
                    continue

                remaining.append(ticket)

                if predicate is None or predicate(ticket.request):
                    base_prio = float(PRIORITY_WEIGHTS.get(ticket.request.priority, 10))
                    # Aging: decrease priority score by 1 for each aging interval spent in queue
                    aging_bonus = wait_sec / self.aging_interval_seconds if self.aging_interval_seconds > 0 else 0.0
                    effective_prio = max(0.0, base_prio - aging_bonus)
                    eligible.append((effective_prio, ticket.created_at, ticket))

            # Fail expired tickets
            for exp in expired:
                _safe_fail_future(
                    exp.future,
                    SchedulingTimeoutError(
                        f"Scheduling ticket '{exp.request.request_id}' timed out while waiting in queue."
                    ),
                )

            if not eligible:
                self._tickets = remaining
                return None

            # Sort by effective priority (lower number = higher priority), then arrival timestamp (FIFO)
            eligible.sort(key=lambda item: (item[0], item[1]))
            chosen = eligible[0][2]

            remaining.remove(chosen)
            self._tickets = remaining
            return chosen

    async def get_counts_by_priority(self) -> dict[str, int]:
        """Return count of queued tickets grouped by priority class."""
        with self._lock:
            counts = {p.value: 0 for p in PriorityClass}
            for t in self._tickets:
                p_val = t.request.priority.value
                counts[p_val] = counts.get(p_val, 0) + 1
            return counts
