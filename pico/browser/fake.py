"""Deterministic fake browser adapter for unit tests and offline execution (Milestone P8.3)."""

from typing import Any
from pico.browser.adapter import BaseBrowserAdapter
from pico.browser.contracts import NavigationStatus, PageLink, PageObservation
from pico.browser.manager import get_browser_allowlist
from pico.browser.security import sanitize_url, validate_safe_destination
from pico.core.config import settings


class FakePageData:
    """Simulated web page data for test registration."""

    def __init__(
        self,
        title: str = "Test Page",
        text_content: str = "",
        links: list[dict[str, str]] | None = None,
        redirect_to: str | None = None,
        simulate_timeout: bool = False,
        simulate_error: str | None = None,
    ):
        self.title = title
        self.text_content = text_content
        self.links = links or []
        self.redirect_to = redirect_to
        self.simulate_timeout = simulate_timeout
        self.simulate_error = simulate_error


class FakeBrowserAdapter(BaseBrowserAdapter):
    """
    Deterministic fake browser adapter.
    Enforces identical security, allowlist, and SSRF gates as the production adapter,
    while returning registered synthetic pages without network or browser dependencies.
    """

    def __init__(
        self,
        simulate_unavailable: bool = False,
        allow_private_for_tests: bool = False,
        skip_dns_for_tests: bool = True,
        mock_dns: dict[str, str | list[str]] | None = None,
    ):
        self.simulate_unavailable = simulate_unavailable
        self.allow_private = allow_private_for_tests
        self.skip_dns = skip_dns_for_tests
        self.mock_dns = mock_dns or {}
        self.pages: dict[str, FakePageData] = {}
        self.navigation_log: list[str] = []

    def register_page(
        self,
        url: str,
        title: str = "Test Page",
        text_content: str = "",
        links: list[dict[str, str]] | None = None,
        redirect_to: str | None = None,
        simulate_timeout: bool = False,
        simulate_error: str | None = None,
    ) -> None:
        """Register a mock page for a given URL."""
        self.pages[url.strip()] = FakePageData(
            title=title,
            text_content=text_content,
            links=links,
            redirect_to=redirect_to,
            simulate_timeout=simulate_timeout,
            simulate_error=simulate_error,
        )

    async def open_page(
        self,
        url: str,
        timeout_seconds: float = 15.0,
        max_bytes: int = 65536,
        max_links: int = 50,
    ) -> PageObservation:
        self.navigation_log.append(url)
        clean_url = url.strip()

        # Gate 1: Truthful unavailable simulation
        if self.simulate_unavailable:
            return PageObservation(
                status=NavigationStatus.UNAVAILABLE,
                requested_url=sanitize_url(clean_url),
                message="Playwright is not installed or browser binary is unavailable.",
            )

        # Gate 2: Server configuration enablement check
        if not getattr(settings, "BROWSER_AUTOMATION_ENABLED", False):
            return PageObservation(
                status=NavigationStatus.REJECTED,
                requested_url=sanitize_url(clean_url),
                message="Browser automation is disabled by server configuration.",
            )

        # Gate 3: Origin allowlist check on initial destination
        allowlist = get_browser_allowlist()
        is_allowed, allow_reason = allowlist.is_origin_allowed(clean_url)
        if not is_allowed:
            return PageObservation(
                status=NavigationStatus.REJECTED,
                requested_url=sanitize_url(clean_url),
                message=allow_reason or "Origin is not permitted.",
            )

        # Gate 4: SSRF check on initial destination
        is_safe, ssrf_reason = validate_safe_destination(
            clean_url,
            allow_private=self.allow_private,
            skip_dns=self.skip_dns,
            mock_dns=self.mock_dns,
        )
        if not is_safe:
            return PageObservation(
                status=NavigationStatus.REJECTED,
                requested_url=sanitize_url(clean_url),
                message=ssrf_reason or "Destination rejected by network safety checks.",
            )

        # Resolve registered mock page
        page_data = self.pages.get(clean_url)

        # Handle simulation flags on initial page
        if page_data and page_data.simulate_timeout:
            return PageObservation(
                status=NavigationStatus.TIMED_OUT,
                requested_url=sanitize_url(clean_url),
                message=f"Navigation timed out after {timeout_seconds}s.",
            )

        if page_data and page_data.simulate_error:
            return PageObservation(
                status=NavigationStatus.FAILED,
                requested_url=sanitize_url(clean_url),
                message=f"Navigation failed: {page_data.simulate_error}",
            )

        # Gate 5: Handle redirects (enforcing allowlist & SSRF on every redirect step)
        final_url = clean_url
        if page_data and page_data.redirect_to:
            redirect_url = page_data.redirect_to.strip()
            # Enforce allowlist on redirect target
            redir_allowed, redir_allow_reason = allowlist.is_origin_allowed(redirect_url)
            if not redir_allowed:
                return PageObservation(
                    status=NavigationStatus.REJECTED,
                    requested_url=sanitize_url(clean_url),
                    final_url=sanitize_url(redirect_url),
                    message=f"Redirect rejected: {redir_allow_reason}",
                )

            # Enforce SSRF on redirect target
            redir_safe, redir_ssrf_reason = validate_safe_destination(
                redirect_url,
                allow_private=self.allow_private,
                skip_dns=self.skip_dns,
                mock_dns=self.mock_dns,
            )
            if not redir_safe:
                return PageObservation(
                    status=NavigationStatus.REJECTED,
                    requested_url=sanitize_url(clean_url),
                    final_url=sanitize_url(redirect_url),
                    message=f"Redirect rejected: {redir_ssrf_reason}",
                )

            final_url = redirect_url
            # Follow redirect data if registered
            if redirect_url in self.pages:
                page_data = self.pages[redirect_url]

        # Extract and bound content
        title = page_data.title if page_data else "Default Page"
        raw_text = page_data.text_content if page_data else f"Content of {clean_url}"
        raw_links = page_data.links if page_data else []

        truncated = False

        # Bound text content bytes
        text_bytes = raw_text.encode("utf-8")
        if len(text_bytes) > max_bytes:
            # Decode with ignore up to max_bytes
            truncated_bytes = text_bytes[:max_bytes]
            bounded_text = truncated_bytes.decode("utf-8", errors="ignore")
            truncated = True
        else:
            bounded_text = raw_text

        # Bound and sanitize links
        parsed_links: list[PageLink] = []
        if len(raw_links) > max_links:
            truncated = True
            raw_links = raw_links[:max_links]

        for lnk in raw_links:
            raw_target = lnk.get("url", "")
            sanitized_target = sanitize_url(raw_target)
            parsed_links.append(
                PageLink(
                    url=sanitized_target,
                    text=lnk.get("text", "")[:128],
                )
            )

        return PageObservation(
            status=NavigationStatus.SUCCESS,
            requested_url=sanitize_url(clean_url),
            final_url=sanitize_url(final_url),
            title=title[:256],
            text_content=bounded_text,
            links=parsed_links,
            truncated=truncated,
            classification="INTERNAL",
            message=None,
        )
