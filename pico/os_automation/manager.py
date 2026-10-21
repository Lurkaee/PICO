"""Global accessors and adapter lifecycle management for OS automation."""

from pico.core.config import settings
from pico.os_automation.adapter import BaseOSAutomationAdapter
from pico.os_automation.allowlist import ApplicationAllowlistManager
from pico.os_automation.windows import WindowsAutomationAdapter

_current_adapter: BaseOSAutomationAdapter | None = None
_allowlist_manager: ApplicationAllowlistManager | None = None


def get_os_adapter() -> BaseOSAutomationAdapter:
    """Returns the globally configured OS adapter or defaults to WindowsAutomationAdapter."""
    global _current_adapter
    if _current_adapter is None:
        _current_adapter = WindowsAutomationAdapter()
    return _current_adapter


def set_os_adapter(adapter: BaseOSAutomationAdapter | None) -> None:
    """Sets or overrides the OS adapter (primarily used for test isolation)."""
    global _current_adapter
    _current_adapter = adapter


def get_allowlist_manager() -> ApplicationAllowlistManager:
    """Returns the server-side application allowlist manager."""
    global _allowlist_manager
    if _allowlist_manager is None:
        raw_config = getattr(settings, "OS_APPLICATION_ALLOWLIST", "{}")
        _allowlist_manager = ApplicationAllowlistManager(raw_config)
    return _allowlist_manager


def set_allowlist_manager(manager: ApplicationAllowlistManager | None) -> None:
    """Sets or overrides the allowlist manager (primarily used for test isolation)."""
    global _allowlist_manager
    _allowlist_manager = manager
