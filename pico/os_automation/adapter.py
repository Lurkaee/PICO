"""Base abstract adapter for operating system and application automation."""

from abc import ABC, abstractmethod

from pico.os_automation.contracts import (
    ApplicationLaunchResult,
    LaunchProfile,
    WindowInspectionResult,
)


class BaseOSAutomationAdapter(ABC):
    """
    Abstract OS automation boundary.
    Separates OS-specific primitives from capability and API definitions.
    """

    @abstractmethod
    async def inspect_windows(
        self,
        max_windows: int = 50,
        max_title_length: int = 256,
    ) -> WindowInspectionResult:
        """
        Enumerate visible top-level windows up to max_windows, bounding title lengths.
        Strictly read-only; zero process creation or window mutation.
        """
        pass

    @abstractmethod
    async def launch_application(
        self,
        profile: LaunchProfile,
    ) -> ApplicationLaunchResult:
        """
        Launch an allowlisted application using direct argument-list process creation
        with shell=False and sanitized environment.
        """
        pass
