from typing import Any
from pico.core.errors import PicoError


class SchedulerError(PicoError):
    """Base exception for all resource scheduling operations."""
    code: str = "SCHEDULER_ERROR"


class QueueFullError(SchedulerError):
    """Raised when the scheduling queue reaches capacity."""
    code: str = "QUEUE_FULL"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class SchedulingTimeoutError(SchedulerError):
    """Raised when a request expires while waiting in queue or exceeds execution deadline."""
    code: str = "SCHEDULING_TIMEOUT"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class ModelResidencyConflictError(SchedulerError):
    """Raised when a request targets an unapproved or conflicting local model."""
    code: str = "MODEL_RESIDENCY_CONFLICT"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class ContextBudgetExceededError(SchedulerError):
    """Raised when token estimates exceed configured context limits."""
    code: str = "CONTEXT_BUDGET_EXCEEDED"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class RequestCancelledError(SchedulerError):
    """Raised when a queued or active request is cancelled."""
    code: str = "REQUEST_CANCELLED"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class TicketNotFoundError(SchedulerError):
    """Raised when a scheduling ticket is not found."""
    code: str = "TICKET_NOT_FOUND"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
