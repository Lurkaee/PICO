from fastapi import APIRouter, Request

from pico.scheduler.service import ResourceScheduler, get_scheduler
from pico.scheduler.types import SchedulerStatus

router = APIRouter(prefix="/api/scheduler", tags=["Scheduler"])


def _get_active_scheduler(request: Request) -> ResourceScheduler:
    """Retrieve scheduler instance from app state, falling back to singleton."""
    return getattr(request.app.state, "scheduler", None) or get_scheduler()


@router.get("/status", response_model=SchedulerStatus, summary="Get scheduler operational status and diagnostics")
async def get_scheduler_status(request: Request) -> SchedulerStatus:
    """Retrieve bounded scheduler diagnostics and truthful hardware telemetry."""
    scheduler = _get_active_scheduler(request)
    return await scheduler.get_status()


@router.get("/metrics", summary="Get lightweight scheduler metrics")
async def get_scheduler_metrics(request: Request) -> dict:
    """Retrieve lightweight operational counters and queue breakdown."""
    scheduler = _get_active_scheduler(request)
    status = await scheduler.get_status()
    return {
        "enabled": status.enabled,
        "active_local_model": status.active_local_model,
        "current_active_local": status.current_active_local,
        "max_active_local": status.max_active_local,
        "current_active_cloud": status.current_active_cloud,
        "max_active_cloud": status.max_active_cloud,
        "queue_depth": status.queue_depth,
        "max_queue_depth": status.max_queue_depth,
        "queue_by_priority": status.queue_by_priority,
        "counts_by_status": status.counts_by_status,
        "gpu_available": status.telemetry.gpu_available,
        "gpu_telemetry_source": status.telemetry.gpu_telemetry_source,
    }
