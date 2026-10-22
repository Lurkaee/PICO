"""Sandboxed browser automation subsystem (Milestone P8.3)."""

from pico.browser.adapter import BaseBrowserAdapter
from pico.browser.contracts import (
    NavigationStatus,
    OpenPageRequest,
    PageLink,
    PageObservation,
)
from pico.browser.fake import FakeBrowserAdapter
from pico.browser.manager import (
    get_browser_adapter,
    get_browser_allowlist,
    reset_browser_state,
    set_browser_adapter,
    set_browser_allowlist,
)
from pico.browser.playwright_adapter import PlaywrightBrowserAdapter
from pico.browser.security import (
    OriginAllowlistManager,
    normalize_origin,
    sanitize_url,
    validate_safe_destination,
)

__all__ = [
    "BaseBrowserAdapter",
    "PlaywrightBrowserAdapter",
    "FakeBrowserAdapter",
    "NavigationStatus",
    "PageLink",
    "PageObservation",
    "OpenPageRequest",
    "OriginAllowlistManager",
    "normalize_origin",
    "validate_safe_destination",
    "sanitize_url",
    "get_browser_adapter",
    "set_browser_adapter",
    "get_browser_allowlist",
    "set_browser_allowlist",
    "reset_browser_state",
]
