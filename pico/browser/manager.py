"""Global accessors and adapter lifecycle management for browser automation (Milestone P8.3)."""

from pico.browser.adapter import BaseBrowserAdapter
from pico.browser.security import OriginAllowlistManager
from pico.core.config import settings

_current_adapter: BaseBrowserAdapter | None = None
_allowlist_manager: OriginAllowlistManager | None = None


def get_browser_adapter() -> BaseBrowserAdapter:
    """
    Returns the globally configured browser adapter.
    Defaults to PlaywrightBrowserAdapter (lazily initialized).
    """
    global _current_adapter
    if _current_adapter is None:
        from pico.browser.playwright_adapter import PlaywrightBrowserAdapter

        _current_adapter = PlaywrightBrowserAdapter()
    return _current_adapter


def set_browser_adapter(adapter: BaseBrowserAdapter | None) -> None:
    """Sets or overrides the browser adapter (primarily used for test isolation)."""
    global _current_adapter
    _current_adapter = adapter


def get_browser_allowlist() -> OriginAllowlistManager:
    """Returns the server-side browser origin allowlist manager."""
    global _allowlist_manager
    if _allowlist_manager is None:
        raw_origins = getattr(settings, "BROWSER_ALLOWED_ORIGINS", [])
        _allowlist_manager = OriginAllowlistManager(raw_origins)
    return _allowlist_manager


def set_browser_allowlist(manager: OriginAllowlistManager | None) -> None:
    """Sets or overrides the origin allowlist manager (primarily used for test isolation)."""
    global _allowlist_manager
    _allowlist_manager = manager


def reset_browser_state() -> None:
    """Resets global adapter and allowlist state to defaults."""
    global _current_adapter, _allowlist_manager
    _current_adapter = None
    _allowlist_manager = None
