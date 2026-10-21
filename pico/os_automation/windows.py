"""Windows native OS automation adapter using standard library ctypes and subprocess."""

import asyncio
import os
import subprocess
import sys
from typing import Any

from pico.os_automation.adapter import BaseOSAutomationAdapter
from pico.os_automation.contracts import (
    ApplicationLaunchResult,
    LaunchProfile,
    LaunchVerificationStatus,
    WindowInspectionResult,
    WindowMetadata,
)
from pico.os_automation.sanitization import build_sanitized_environment

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
else:
    user32 = None
    kernel32 = None
    WNDENUMPROC = None


class WindowsAutomationAdapter(BaseOSAutomationAdapter):
    """
    Native Windows desktop adapter.
    Uses standard library ctypes for read-only window enumeration
    and direct subprocess.Popen with shell=False for allowlisted execution.
    """

    async def inspect_windows(
        self,
        max_windows: int = 50,
        max_title_length: int = 256,
    ) -> WindowInspectionResult:
        if sys.platform != "win32" or user32 is None:
            return WindowInspectionResult(
                status="unsupported",
                platform=sys.platform,
                windows=[],
                total_windows_found=0,
                truncated=False,
                message=f"Window inspection is unsupported on platform '{sys.platform}'.",
            )

        bounded_max = max(1, min(max_windows, 100))
        bounded_title_len = max(1, min(max_title_length, 512))

        # Run ctypes enumeration in thread pool to prevent blocking asyncio loop
        return await asyncio.to_thread(
            self._enumerate_windows_sync,
            bounded_max,
            bounded_title_len,
        )

    def _enumerate_windows_sync(
        self,
        max_windows: int,
        max_title_length: int,
    ) -> WindowInspectionResult:
        windows: list[WindowMetadata] = []
        total_discovered = 0
        truncated = False

        try:
            foreground_hwnd = user32.GetForegroundWindow()

            def enum_proc(hwnd: wintypes.HWND, lparam: wintypes.LPARAM) -> wintypes.BOOL:
                nonlocal total_discovered, truncated
                try:
                    # Only visible windows
                    if not user32.IsWindowVisible(hwnd):
                        return 1

                    length = user32.GetWindowTextLengthW(hwnd)
                    if length <= 0:
                        return 1

                    buf = ctypes.create_unicode_buffer(length + 1)
                    chars = user32.GetWindowTextW(hwnd, buf, length + 1)
                    if chars <= 0:
                        return 1

                    title = buf.value.strip()
                    if not title:
                        return 1

                    total_discovered += 1

                    if len(windows) >= max_windows:
                        truncated = True
                        return 0  # Stop enumeration once bounded limit reached

                    # Strict title clamping
                    clamped_title = title[:max_title_length]

                    # Read PID safely
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    process_id = int(pid.value) if pid.value != 0 else None

                    is_minimized = bool(user32.IsIconic(hwnd))
                    is_foreground = (hwnd == foreground_hwnd)

                    windows.append(
                        WindowMetadata(
                            handle=int(hwnd),
                            title=clamped_title,
                            process_id=process_id,
                            is_visible=True,
                            is_minimized=is_minimized,
                            is_foreground=is_foreground,
                        )
                    )
                    return 1
                except Exception:
                    # Inaccessible or changing window; continue enumeration
                    return 1

            cb = WNDENUMPROC(enum_proc)
            user32.EnumWindows(cb, 0)

            return WindowInspectionResult(
                status="success",
                platform="windows",
                windows=windows,
                total_windows_found=total_discovered,
                truncated=truncated,
                message=None,
            )
        except Exception as exc:
            return WindowInspectionResult(
                status="unavailable",
                platform="windows",
                windows=[],
                total_windows_found=0,
                truncated=False,
                message=f"Windows inspection failed: {str(exc)}",
            )

    async def launch_application(
        self,
        profile: LaunchProfile,
    ) -> ApplicationLaunchResult:
        if sys.platform != "win32":
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.LAUNCH_FAILED,
                executable_path=profile.executable_path,
                verification_status="failed",
                verification_details={"reason": "unsupported_platform"},
                message=f"Process launch is unsupported on platform '{sys.platform}'.",
            )

        cmd = [profile.executable_path, *profile.args]
        clean_env = build_sanitized_environment()

        try:
            # Check executable existence before launch
            if not os.path.isfile(profile.executable_path):
                return ApplicationLaunchResult(
                    alias=profile.alias,
                    status=LaunchVerificationStatus.LAUNCH_FAILED,
                    executable_path=profile.executable_path,
                    verification_status="failed",
                    verification_details={"reason": "executable_not_found"},
                    message=f"Executable file not found at '{profile.executable_path}'.",
                )

            # Direct process creation with shell=False and minimal environment
            proc = subprocess.Popen(
                cmd,
                cwd=profile.working_dir,
                env=clean_env,
                shell=False,
            )

            pid = proc.pid

            # Brief non-blocking check: did the process crash or terminate immediately?
            await asyncio.sleep(0.1)
            poll_code = proc.poll()

            if poll_code is not None and poll_code != 0:
                return ApplicationLaunchResult(
                    alias=profile.alias,
                    status=LaunchVerificationStatus.LAUNCH_FAILED,
                    executable_path=profile.executable_path,
                    process_id=pid,
                    verification_status="failed",
                    verification_details={"exit_code": poll_code, "reason": "process_exited_with_error"},
                    message=f"Process exited immediately with code {poll_code}.",
                )

            # Check if a visible window can be reliably associated with this PID
            inspection = await self.inspect_windows(max_windows=100)
            matching_window = next(
                (w for w in inspection.windows if w.process_id == pid),
                None,
            )

            if matching_window:
                status = LaunchVerificationStatus.INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED
                v_status = "verified_window_observed"
                details: dict[str, Any] = {
                    "pid": pid,
                    "window_observed": True,
                    "window_handle": matching_window.handle,
                    "window_title": matching_window.title,
                }
            else:
                status = LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED
                v_status = "process_creation_confirmed"
                details = {
                    "pid": pid,
                    "window_observed": False,
                    "note": "Process creation confirmed; no top-level window reliably associated within observation window.",
                }

            return ApplicationLaunchResult(
                alias=profile.alias,
                status=status,
                executable_path=profile.executable_path,
                process_id=pid,
                verification_status=v_status,
                verification_details=details,
                message=None,
            )

        except FileNotFoundError as exc:
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.LAUNCH_FAILED,
                executable_path=profile.executable_path,
                verification_status="failed",
                verification_details={"error": str(exc)},
                message=f"Executable not found: {str(exc)}",
            )
        except PermissionError as exc:
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.LAUNCH_FAILED,
                executable_path=profile.executable_path,
                verification_status="failed",
                verification_details={"error": str(exc)},
                message=f"Permission denied: {str(exc)}",
            )
        except Exception as exc:
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.LAUNCH_UNCERTAIN,
                executable_path=profile.executable_path,
                verification_status="uncertain",
                verification_details={"error": str(exc)},
                message=f"Launch outcome uncertain due to unexpected exception: {str(exc)}",
            )
