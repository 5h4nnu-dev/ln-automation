"""Browser-level orchestration: opening the course, detecting auth, recovery."""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Callable

from playwright.async_api import Page

from browser.session import BrowserSession
from config import Settings
from utils.exceptions import (
    AuthenticationRequiredError,
    BrowserClosedError,
    BrowserError,
    NavigationError,
)
from utils.logger import get_logger
from utils.selectors import Selectors

logger = get_logger(__name__)


class AuthStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    UNKNOWN = "unknown"


class BrowserManager:
    """Keeps the browser healthy and gates course access behind manual auth."""

    def __init__(self, settings: Settings, selectors: Selectors) -> None:
        self.settings = settings
        self.selectors = selectors
        self.session = BrowserSession(settings)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> Page:
        return await self.session.start()

    async def close(self) -> None:
        await self.session.close()

    @property
    def page(self) -> Page:
        return self.session.page

    @property
    def is_running(self) -> bool:
        return self.session.is_started and self.session.is_alive()

    async def ensure_ready(self) -> Page:
        """Start or restart Chromium if the window disappeared."""

        if not self.session.is_started:
            return await self.start()
        if not self.session.is_alive():
            logger.warning("Browser window was lost; reopening", extra={"event": "browser_recover"})
            return await self.session.restart()
        return self.session.page

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def open_url(self, url: str, *, retries: int = 1) -> Page:
        page = await self.ensure_ready()
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return await self.session.goto(url)
            except BrowserClosedError:
                page = await self.ensure_ready()
                last_error = BrowserClosedError()
            except NavigationError as exc:
                last_error = exc
                logger.warning(
                    "Navigation failed",
                    extra={"event": "navigation_failed", "url": url, "attempt": attempt + 1},
                )
                if attempt < retries:
                    await asyncio.sleep(1.5)
        if isinstance(last_error, BrowserClosedError):
            raise last_error
        raise last_error or NavigationError(f"Could not open {url}")

    async def open_course(
        self,
        url: str,
        *,
        on_wait: Callable[[float], None] | None = None,
        auth_timeout: float | None = None,
    ) -> Page:
        """Open a course, waiting for the user to log in manually if needed."""

        if not url:
            raise NavigationError("No course URL configured.")

        timeout = float(auth_timeout if auth_timeout is not None else self.settings.auth_timeout)

        await self.open_url(url)
        status = await self.detect_auth_status()

        if status is AuthStatus.UNAUTHENTICATED:
            logger.warning(
                "LinkedIn session is not authenticated; waiting for manual login",
                extra={"event": "authentication_required", "url": self.page.url},
            )
            await self.wait_for_authentication(timeout=timeout, on_wait=on_wait)
            await self.open_url(url)

        elif status is AuthStatus.UNKNOWN:
            logger.warning(
                "Could not positively verify the LinkedIn session; continuing",
                extra={"event": "authentication_unknown", "url": self.page.url},
            )

        return self.page

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    async def _any_visible(self, selectors: tuple[str, ...]) -> bool:
        page = self.page
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def detect_auth_status(self) -> AuthStatus:
        """Best-effort, marker-based session check (no credentials involved)."""

        page = self.page
        url = (page.url or "").lower()

        if any(fragment in url for fragment in self.selectors.auth_login_url_fragments):
            return AuthStatus.UNAUTHENTICATED

        if await self._any_visible(self.selectors.auth_login_form):
            return AuthStatus.UNAUTHENTICATED

        if await self._any_visible(self.selectors.auth_verified_markers):
            return AuthStatus.AUTHENTICATED

        # A visible curriculum or video is a stronger positive signal than the
        # absence of a login form.
        for selector in (*self.selectors.toc_root, *self.selectors.video):
            try:
                if await page.locator(selector).first.is_visible():
                    return AuthStatus.AUTHENTICATED
            except Exception:
                continue

        if await self._any_visible(self.selectors.auth_signin_markers):
            return AuthStatus.UNAUTHENTICATED

        return AuthStatus.UNKNOWN

    async def wait_for_authentication(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 3.0,
        on_wait: Callable[[float], None] | None = None,
    ) -> None:
        """Block until the user has logged in inside the browser window."""

        limit = float(timeout if timeout is not None else self.settings.auth_timeout)
        elapsed = 0.0
        while elapsed < limit:
            try:
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
            elapsed += poll_interval
            if not self.session.is_alive():
                raise BrowserClosedError()
            status = await self.detect_auth_status()
            if status is AuthStatus.AUTHENTICATED:
                logger.info("LinkedIn session authenticated", extra={"event": "authentication_ok"})
                return
            if on_wait:
                on_wait(elapsed)

        raise AuthenticationRequiredError(
            "Timed out waiting for manual LinkedIn login.",
        )

    async def restart(self) -> Page:
        return await self.session.restart()

    # ------------------------------------------------------------------ #
    # Small DOM helpers used by higher layers
    # ------------------------------------------------------------------ #
    async def count(self, selector: str) -> int:
        try:
            return await self.page.locator(selector).count()
        except Exception:
            return 0

    async def exists(self, selector: str) -> bool:
        return await self.count(selector) > 0

    async def wait_for_any(
        self,
        selectors: tuple[str, ...],
        *,
        timeout: float = 20.0,
        poll_interval: float = 0.5,
    ) -> str | None:
        """Wait until any selector is visible and return it."""

        elapsed = 0.0
        while elapsed < timeout:
            for selector in selectors:
                try:
                    if await self.page.locator(selector).first.is_visible():
                        return selector
                except Exception:
                    continue
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        return None
