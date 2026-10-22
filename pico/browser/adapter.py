"""Base abstract adapter for sandboxed browser automation (Milestone P8.3)."""

from abc import ABC, abstractmethod

from pico.browser.contracts import PageObservation


class BaseBrowserAdapter(ABC):
    """
    Abstract browser automation boundary.
    Separates browser driver implementations (Playwright, Fake/Mock)
    from capability execution, authorization, and API boundaries.
    """

    @abstractmethod
    async def open_page(
        self,
        url: str,
        timeout_seconds: float = 15.0,
        max_bytes: int = 65536,
        max_links: int = 50,
    ) -> PageObservation:
        """
        Navigate to url in an ephemeral, sandboxed browser context,
        enforce origin allowlisting on navigation and redirects,
        and return a bounded, sanitized read-only page observation.
        """
        pass
