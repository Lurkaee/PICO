"""OS automation capabilities package."""

from pico.capabilities.os.inspect_windows import (
    InspectWindowsCapability,
    InspectWindowsInput,
    InspectWindowsOutput,
)
from pico.capabilities.os.launch_application import (
    LaunchApplicationCapability,
    LaunchApplicationInput,
    LaunchApplicationOutput,
)

__all__ = [
    "InspectWindowsCapability",
    "InspectWindowsInput",
    "InspectWindowsOutput",
    "LaunchApplicationCapability",
    "LaunchApplicationInput",
    "LaunchApplicationOutput",
]
