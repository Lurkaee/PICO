"""Playwright-backed browser adapter implementation with strict sandbox and ephemeral contexts (Milestone P8.3)."""

from typing import Any
from pico.browser.adapter import BaseBrowserAdapter
from pico.browser.contracts import NavigationStatus, PageLink, PageObservation
from pico.browser.manager import get_browser_allowlist
from pico.browser.security import sanitize_url, validate_safe_destination
from pico.core.config import settings

try:
    from playwright.async_api import (
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )

    PLAYWRIGHT_INSTALLED = True
except ImportError:
    PLAYWRIGHT_INSTALLED = False


class PlaywrightBrowserAdapter(BaseBrowserAdapter):
    """
    Production browser adapter utilizing Playwright and Chromium.
    Enforces strict process sandboxing, ephemeral per-request browser contexts,
    request and redirect route filtering against server allowlists and SSRF boundaries.
    """

    async def open_page(
        self,
        url: str,
        timeout_seconds: float = 15.0,
        max_bytes: int = 65536,
        max_links: int = 50,
    ) -> PageObservation:
        clean_url = url.strip()
        sanitized_req_url = sanitize_url(clean_url)

        # Gate 1: Check optional dependency presence without uncontrolled downloading
        if not PLAYWRIGHT_INSTALLED:
            return PageObservation(
                status=NavigationStatus.UNAVAILABLE,
                requested_url=sanitized_req_url,
                message="Playwright is not installed. Browser automation is unavailable.",
            )

        # Gate 2: Server configuration enablement check
        if not getattr(settings, "BROWSER_AUTOMATION_ENABLED", False):
            return PageObservation(
                status=NavigationStatus.REJECTED,
                requested_url=sanitized_req_url,
                message="Browser automation is disabled by server configuration.",
            )

        # Gate 3: Origin allowlist check on initial destination
        allowlist = get_browser_allowlist()
        is_allowed, allow_reason = allowlist.is_origin_allowed(clean_url)
        if not is_allowed:
            return PageObservation(
                status=NavigationStatus.REJECTED,
                requested_url=sanitized_req_url,
                message=allow_reason or "Origin is not permitted.",
            )

        # Gate 4: Pre-flight SSRF check on destination
        is_safe, ssrf_reason = validate_safe_destination(clean_url)
        if not is_safe:
            return PageObservation(
                status=NavigationStatus.REJECTED,
                requested_url=sanitized_req_url,
                message=ssrf_reason or "Destination rejected by network safety checks.",
            )

        # Ephemeral sandbox launch
        try:
            async with async_playwright() as p:
                try:
                    # Chromium sandbox is strictly ENABLED. NEVER pass --no-sandbox.
                    browser = await p.chromium.launch(
                        headless=getattr(settings, "BROWSER_HEADLESS", True),
                        args=[
                            "--disable-background-networking",
                            "--disable-default-apps",
                            "--disable-extensions",
                            "--disable-sync",
                            "--disable-translate",
                            "--metrics-recording-only",
                            "--no-first-run",
                            "--safebrowsing-disable-auto-update",
                        ],
                    )
                except Exception as exc:
                    return PageObservation(
                        status=NavigationStatus.UNAVAILABLE,
                        requested_url=sanitized_req_url,
                        message=f"Failed to launch browser binary: {str(exc)}",
                    )

                context = None
                try:
                    # Ephemeral, isolated context with blocked downloads and service workers
                    context = await browser.new_context(
                        accept_downloads=False,
                        service_workers="block",
                        ignore_https_errors=False,
                        java_script_enabled=True,
                    )

                    # Intercept all requests and redirects to enforce allowlist & SSRF policy
                    async def handle_route(route: Any, request: Any) -> None:
                        req_url = request.url
                        if request.is_navigation_request():
                            # Enforce allowlist on every redirect / navigation
                            redir_allowed, _ = allowlist.is_origin_allowed(req_url)
                            if not redir_allowed:
                                await route.abort("blockedbyclient")
                                return
                            # Enforce SSRF safety on every redirect / navigation
                            redir_safe, _ = validate_safe_destination(req_url)
                            if not redir_safe:
                                await route.abort("blockedbyclient")
                                return
                        elif getattr(settings, "BROWSER_BLOCK_THIRD_PARTY_SUBRESOURCES", True):
                            sub_allowed, _ = allowlist.is_origin_allowed(req_url)
                            if not sub_allowed:
                                await route.abort("blockedbyclient")
                                return

                        await route.continue_()

                    await context.route("**/*", handle_route)

                    page = await context.new_page()

                    # Block popups and downloads
                    page.on("popup", lambda popup: popup.close())
                    page.on("download", lambda download: download.cancel())

                    # Navigate with bounded timeout
                    await page.goto(
                        clean_url,
                        timeout=int(timeout_seconds * 1000),
                        wait_until="domcontentloaded",
                    )

                    # Re-verify final URL after navigation
                    final_url = page.url
                    final_allowed, final_allow_reason = allowlist.is_origin_allowed(final_url)
                    if not final_allowed:
                        return PageObservation(
                            status=NavigationStatus.REJECTED,
                            requested_url=sanitized_req_url,
                            final_url=sanitize_url(final_url),
                            message=f"Final destination escaped origin allowlist: {final_allow_reason}",
                        )

                    final_safe, final_ssrf_reason = validate_safe_destination(final_url)
                    if not final_safe:
                        return PageObservation(
                            status=NavigationStatus.REJECTED,
                            requested_url=sanitized_req_url,
                            final_url=sanitize_url(final_url),
                            message=f"Final destination failed safety check: {final_ssrf_reason}",
                        )

                    # Extract bounded title and body text
                    title = await page.title()
                    raw_text = await page.inner_text("body")

                    # Extract links
                    raw_links = await page.evaluate(
                        """() => {
                            const results = [];
                            const anchors = document.querySelectorAll('a[href]');
                            for (const a of anchors) {
                                results.push({
                                    url: a.href || '',
                                    text: (a.innerText || a.textContent || '').trim()
                                });
                            }
                            return results;
                        }"""
                    )

                    # Bound extracted text
                    truncated = False
                    text_bytes = raw_text.encode("utf-8")
                    if len(text_bytes) > max_bytes:
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
                        target = lnk.get("url", "")
                        sanitized_target = sanitize_url(target)
                        parsed_links.append(
                            PageLink(
                                url=sanitized_target,
                                text=lnk.get("text", "")[:128],
                            )
                        )

                    return PageObservation(
                        status=NavigationStatus.SUCCESS,
                        requested_url=sanitized_req_url,
                        final_url=sanitize_url(final_url),
                        title=title[:256] if title else None,
                        text_content=bounded_text,
                        links=parsed_links,
                        truncated=truncated,
                        classification="INTERNAL",
                        message=None,
                    )

                finally:
                    if context is not None:
                        await context.close()
                    await browser.close()

        except PlaywrightTimeoutError:
            return PageObservation(
                status=NavigationStatus.TIMED_OUT,
                requested_url=sanitized_req_url,
                message=f"Navigation timed out after {timeout_seconds}s.",
            )
        except PlaywrightError as exc:
            return PageObservation(
                status=NavigationStatus.FAILED,
                requested_url=sanitized_req_url,
                message=f"Playwright navigation failed: {str(exc)}",
            )
        except Exception as exc:
            return PageObservation(
                status=NavigationStatus.FAILED,
                requested_url=sanitized_req_url,
                message=f"Browser automation error: {str(exc)}",
            )
