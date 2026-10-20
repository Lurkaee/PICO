"""Hardware telemetry collector for PICO benchmark runner.

Collects system RAM and GPU VRAM without requiring third-party dependencies.
Distinguishes real runtime measurements from unavailable metrics. Never fabricates values.
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys
from ctypes import wintypes
from typing import Any
from pydantic import BaseModel, Field


class SystemMemorySnapshot(BaseModel):
    available: bool = True
    total_mb: float = 0.0
    used_mb: float = 0.0
    available_mb: float = 0.0
    percent_used: float = 0.0
    process_working_set_mb: float = 0.0
    source: str = "runtime_os_api"
    error_reason: str | None = None


class GpuMemorySnapshot(BaseModel):
    available: bool = False
    gpu_name: str | None = None
    total_vram_mb: float | None = None
    used_vram_mb: float | None = None
    free_vram_mb: float | None = None
    source: str = "nvidia-smi"
    error_reason: str | None = None


# Windows memory structures
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def get_system_memory() -> SystemMemorySnapshot:
    """Measure total and currently available system RAM and process working set."""
    if sys.platform != "win32":
        return SystemMemorySnapshot(
            available=False,
            error_reason="Non-Windows platform telemetry not implemented without psutil.",
        )

    try:
        # System physical RAM
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return SystemMemorySnapshot(
                available=False,
                error_reason="GlobalMemoryStatusEx returned False",
            )

        total_mb = round(stat.ullTotalPhys / (1024 * 1024), 2)
        avail_mb = round(stat.ullAvailPhys / (1024 * 1024), 2)
        used_mb = round(total_mb - avail_mb, 2)
        pct_used = float(stat.dwMemoryLoad)

        # Process working set
        working_set_mb = 0.0
        try:
            fn = getattr(
                ctypes.windll.kernel32,
                "K32GetProcessMemoryInfo",
                ctypes.windll.psapi.GetProcessMemoryInfo,
            )
            fn.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
                wintypes.DWORD,
            ]
            fn.restype = wintypes.BOOL

            h_proc = ctypes.windll.kernel32.GetCurrentProcess()
            pmc = PROCESS_MEMORY_COUNTERS_EX()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            if fn(h_proc, ctypes.byref(pmc), pmc.cb):
                working_set_mb = round(pmc.WorkingSetSize / (1024 * 1024), 2)
        except Exception:
            pass

        return SystemMemorySnapshot(
            available=True,
            total_mb=total_mb,
            used_mb=used_mb,
            available_mb=avail_mb,
            percent_used=pct_used,
            process_working_set_mb=working_set_mb,
            source="windows_kernel32",
        )

    except Exception as exc:
        return SystemMemorySnapshot(
            available=False,
            error_reason=f"Failed to query system RAM: {exc}",
        )


def get_gpu_memory() -> GpuMemorySnapshot:
    """Measure dedicated GPU VRAM using nvidia-smi if present. Never fabricates values."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode != 0:
            return GpuMemorySnapshot(
                available=False,
                error_reason=f"nvidia-smi exited with code {proc.returncode}: {proc.stderr.strip()}",
            )

        line = proc.stdout.strip().splitlines()
        if not line:
            return GpuMemorySnapshot(
                available=False,
                error_reason="nvidia-smi returned empty output",
            )

        parts = [p.strip() for p in line[0].split(",")]
        if len(parts) >= 4:
            return GpuMemorySnapshot(
                available=True,
                gpu_name=parts[0],
                total_vram_mb=float(parts[1]),
                used_vram_mb=float(parts[2]),
                free_vram_mb=float(parts[3]),
                source="nvidia-smi",
            )
        else:
            return GpuMemorySnapshot(
                available=False,
                error_reason=f"Unrecognized nvidia-smi format: {line[0]}",
            )

    except FileNotFoundError:
        return GpuMemorySnapshot(
            available=False,
            error_reason="nvidia-smi executable not found on PATH",
        )
    except subprocess.TimeoutExpired:
        return GpuMemorySnapshot(
            available=False,
            error_reason="nvidia-smi query timed out after 2.0s",
        )
    except Exception as exc:
        return GpuMemorySnapshot(
            available=False,
            error_reason=f"GPU query failed: {exc}",
        )


def get_free_disk_gb(path: str = ".") -> float | None:
    """Measure free disk space in GB on the drive containing path."""
    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        return round(free / (1024 ** 3), 2)
    except Exception:
        return None


def get_hardware_environment() -> dict[str, Any]:
    """Capture snapshot of the host execution environment."""
    sys_mem = get_system_memory()
    gpu_mem = get_gpu_memory()
    free_disk = get_free_disk_gb()

    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python_version": sys.version.split()[0],
        "system_ram_total_mb": sys_mem.total_mb if sys_mem.available else None,
        "system_ram_available_mb": sys_mem.available_mb if sys_mem.available else None,
        "gpu_available": gpu_mem.available,
        "gpu_name": gpu_mem.gpu_name if gpu_mem.available else None,
        "gpu_vram_total_mb": gpu_mem.total_vram_mb if gpu_mem.available else None,
        "gpu_vram_used_mb": gpu_mem.used_vram_mb if gpu_mem.available else None,
        "gpu_telemetry_source": gpu_mem.source if gpu_mem.available else "unavailable",
        "gpu_error_reason": gpu_mem.error_reason,
        "free_disk_gb": free_disk,
    }
