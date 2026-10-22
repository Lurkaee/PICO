from pico.scheduler.types import SchedulerTelemetry


def capture_scheduler_telemetry() -> SchedulerTelemetry:
    """Collect truthful system RAM and GPU VRAM telemetry.

    Distinguishes measured metrics from unavailable telemetry. Never fabricates values.
    """
    try:
        from tools.bench.telemetry import get_gpu_memory, get_system_memory

        sys_mem = get_system_memory()
        gpu_mem = get_gpu_memory()

        return SchedulerTelemetry(
            system_ram_total_mb=sys_mem.total_mb if sys_mem.available else None,
            system_ram_available_mb=sys_mem.available_mb if sys_mem.available else None,
            gpu_available=gpu_mem.available,
            gpu_name=gpu_mem.gpu_name if gpu_mem.available else None,
            gpu_vram_total_mb=gpu_mem.total_vram_mb if gpu_mem.available else None,
            gpu_vram_used_mb=gpu_mem.used_vram_mb if gpu_mem.available else None,
            gpu_telemetry_source=gpu_mem.source if gpu_mem.available else "unavailable",
            gpu_error_reason=gpu_mem.error_reason,
        )
    except Exception as exc:
        return SchedulerTelemetry(
            system_ram_total_mb=None,
            system_ram_available_mb=None,
            gpu_available=False,
            gpu_telemetry_source="unavailable",
            gpu_error_reason=f"Telemetry capture error: {exc}",
        )
