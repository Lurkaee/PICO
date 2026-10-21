"""PICO OS & Desktop Application Automation Layer (Milestone P8.1)."""

from pico.os_automation.adapter import BaseOSAutomationAdapter
from pico.os_automation.allowlist import (
    ApplicationAllowlistManager,
    validate_launch_profile,
)
from pico.os_automation.contracts import (
    ApplicationLaunchResult,
    LaunchProfile,
    LaunchVerificationStatus,
    WindowInspectionResult,
    WindowMetadata,
    WindowState,
)
from pico.os_automation.fake import FakeOSAutomationAdapter
from pico.os_automation.manager import (
    get_allowlist_manager,
    get_os_adapter,
    set_allowlist_manager,
    set_os_adapter,
)
from pico.os_automation.sanitization import build_sanitized_environment
from pico.os_automation.windows import WindowsAutomationAdapter

__all__ = [
    "BaseOSAutomationAdapter",
    "WindowsAutomationAdapter",
    "FakeOSAutomationAdapter",
    "ApplicationAllowlistManager",
    "validate_launch_profile",
    "build_sanitized_environment",
    "get_os_adapter",
    "set_os_adapter",
    "get_allowlist_manager",
    "set_allowlist_manager",
    "WindowState",
    "WindowMetadata",
    "WindowInspectionResult",
    "LaunchVerificationStatus",
    "LaunchProfile",
    "ApplicationLaunchResult",
]
