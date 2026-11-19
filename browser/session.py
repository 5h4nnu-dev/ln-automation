"""Playwright lifecycle with a persistent Chromium profile.

The profile directory is the only thing that carries the LinkedIn session
between runs. Credentials (password / OTP) are never requested, typed, read or
stored by the automation: the user signs in manually in the visible browser.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config import Settings
from utils.exceptions import (
    BrowserClosedError,
    BrowserError,
    NavigationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class BrowserSession:
    """Owns the Playwright instance, persistent context and active page."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> Page:
        if self._context is not None and self._page is not None and not self._page.is_closed():
            return self._page

        profile = Path(self.settings.browser_profile)
        profile.mkdir(parents=True, exist_ok=True)

        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.settings.headless,
                slow_mo=self.settings.slow_mo_ms or 0,
                viewport={
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                },
                accept_downloads=False,
                timeout=60_000,
                # Playback is started by the tool, so Chromium must be allowed to
                # honour programmatic play() without a fresh click gesture.
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            self._context.set_default_timeout(20_000)
            self._context.set_default_navigation_timeout(60_000)
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        except Exception as exc:
            await self._cleanup()
            raise BrowserError(
                f"Could not launch Chromium: {exc}",
                hint=(
                    "Close any Chromium instance using the same profile directory, "
                    "then retry. Run `playwright install chromium` if the browser is missing."
                ),
            ) from exc

        logger.info(
            "Chromium started",
            extra={"event": "browser_started", "profile": str(profile), "headless": self.settings.headless},
        )
        return self._page

    async def close(self) -> None:
        await self._cleanup()
        logger.info("Chromium stopped", extra={"event": "browser_stopped"})

    async def restart(self) -> Page:
        """Reopen Chromium with the same profile (cookies/session preserved)."""

        logger.warning("Restarting Chromium", extra={"event": "browser_restart"})
        await self._cleanup()
        await asyncio.sleep(1.0)
        return await self.start()

    async def _cleanup(self) -> None:
        page, context, playwright = self._page, self._context, self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        del page  # page closes with its context

    # ------------------------------------------------------------------ #
    # Access / navigation
    # ------------------------------------------------------------------ #
    @property
    def is_started(self) -> bool:
        return self._context is not None and self._page is not None

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserClosedError("Browser has not been started yet.")
        return self._context

    @property
    def page(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise BrowserClosedError()
        return self._page

    def is_alive(self) -> bool:
        try:
            return (
                self._context is not None
                and self._page is not None
                and not self._page.is_closed()
            )
        except Exception:
            return False

    async def new_page(self) -> Page:
        if self._context is None:
            raise BrowserClosedError("Browser has not been started yet.")
        self._page = await self._context.new_page()
        return self._page

    async def goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: float = 60_000,
    ) -> Page:
        page = self.page
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
        except PlaywrightTimeoutError as exc:
            # A slow page can still be usable; keep the current content instead
            # of failing the entire lesson for a single slow resource.
            if page.url and page.url != "about:blank":
                logger.warning(
                    "Navigation timed out but a page is loaded",
                    extra={"event": "navigation_timeout", "url": page.url},
                )
                return page
            raise NavigationError(
                f"Timed out opening {url}",
                hint="Check the network connection and retry.",
                retryable=True,
            ) from exc
        except Exception as exc:
            if self._page is None or self._page.is_closed():
                raise BrowserClosedError() from exc
            raise NavigationError(
                f"Could not open {url}: {exc}",
                hint="Check the course URL and network connection.",
                retryable=True,
            ) from exc
        return page
