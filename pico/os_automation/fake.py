"""Fake OS automation adapter for deterministic, isolated testing without real desktop side effects."""

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


class FakeOSAutomationAdapter(BaseOSAutomationAdapter):
    """
    Fake OS adapter used by automated tests.
    Strictly prohibits real desktop launches or native API calls in CI.
    """

    def __init__(
        self,
        synthetic_windows: list[WindowMetadata] | None = None,
        inspection_status: str = "success",
        platform_name: str = "windows",
        launch_outcome: LaunchVerificationStatus = LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED,
        simulated_pid: int = 4321,
        launch_message: str | None = None,
    ) -> None:
        self.synthetic_windows: list[WindowMetadata] = synthetic_windows or []
        self.inspection_status: str = inspection_status
        self.platform_name: str = platform_name
        self.launch_outcome: LaunchVerificationStatus = launch_outcome
        self.simulated_pid: int = simulated_pid
        self.launch_message: str | None = launch_message
        self.launch_history: list[dict[str, Any]] = []
        self.inspection_history: list[dict[str, Any]] = []

    async def inspect_windows(
        self,
        max_windows: int = 50,
        max_title_length: int = 256,
    ) -> WindowInspectionResult:
        self.inspection_history.append({
            "max_windows": max_windows,
            "max_title_length": max_title_length,
        })

        if self.inspection_status == "unsupported":
            return WindowInspectionResult(
                status="unsupported",
                platform=self.platform_name,
                windows=[],
                total_windows_found=0,
                truncated=False,
                message=f"Window inspection is unsupported on platform '{self.platform_name}'.",
            )

        if self.inspection_status == "unavailable":
            return WindowInspectionResult(
                status="unavailable",
                platform=self.platform_name,
                windows=[],
                total_windows_found=0,
                truncated=False,
                message="Window inspection service is temporarily unavailable.",
            )

        total = len(self.synthetic_windows)
        bounded = self.synthetic_windows[:max_windows]
        truncated = total > max_windows

        # Clamp titles to max_title_length
        clamped_windows = [
            WindowMetadata(
                handle=w.handle,
                title=w.title[:max_title_length],
                process_id=w.process_id,
                is_visible=w.is_visible,
                is_minimized=w.is_minimized,
                is_foreground=w.is_foreground,
            )
            for w in bounded
        ]

        return WindowInspectionResult(
            status="success",
            platform=self.platform_name,
            windows=clamped_windows,
            total_windows_found=total,
            truncated=truncated,
            message=None,
        )

    async def launch_application(
        self,
        profile: LaunchProfile,
    ) -> ApplicationLaunchResult:
        # Build sanitized environment exactly as real adapter does
        sanitized_env = build_sanitized_environment()

        call_record = {
            "alias": profile.alias,
            "executable_path": profile.executable_path,
            "args": list(profile.args),
            "working_dir": profile.working_dir,
            "sanitized_env": sanitized_env,
            "shell": False,
        }
        self.launch_history.append(call_record)

        if self.launch_outcome == LaunchVerificationStatus.REQUEST_REJECTED:
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.REQUEST_REJECTED,
                executable_path=profile.executable_path,
                verification_status="rejected",
                verification_details={"reason": "explicit_rejection"},
                message=self.launch_message or "Launch request rejected.",
            )

        if self.launch_outcome == LaunchVerificationStatus.LAUNCH_FAILED:
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.LAUNCH_FAILED,
                executable_path=profile.executable_path,
                verification_status="failed",
                verification_details={"reason": "simulated_failure"},
                message=self.launch_message or "Process launch failed.",
            )

        if self.launch_outcome == LaunchVerificationStatus.LAUNCH_UNCERTAIN:
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.LAUNCH_UNCERTAIN,
                executable_path=profile.executable_path,
                verification_status="uncertain",
                verification_details={"reason": "simulated_uncertainty"},
                message=self.launch_message or "Launch outcome uncertain.",
            )

        if self.launch_outcome == LaunchVerificationStatus.INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED:
            return ApplicationLaunchResult(
                alias=profile.alias,
                status=LaunchVerificationStatus.INDEPENDENT_WINDOW_OBSERVATION_CONFIRMED,
                executable_path=profile.executable_path,
                process_id=self.simulated_pid,
                verification_status="verified_window_observed",
                verification_details={
                    "pid": self.simulated_pid,
                    "window_observed": True,
                    "window_handle": 9999,
                    "window_title": "Synthetic Window",
                },
                message=None,
            )

        return ApplicationLaunchResult(
            alias=profile.alias,
            status=LaunchVerificationStatus.PROCESS_CREATION_CONFIRMED,
            executable_path=profile.executable_path,
            process_id=self.simulated_pid,
            verification_status="process_creation_confirmed",
            verification_details={
                "pid": self.simulated_pid,
                "window_observed": False,
                "note": "Process creation confirmed; no top-level window reliably associated within observation window.",
            },
            message=None,
        )
