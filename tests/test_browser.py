"""Browser lifecycle, persistent profile and authentication detection tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from browser.manager import AuthStatus, BrowserManager
from browser.session import BrowserSession
from config import Settings
from utils.selectors import Selectors

LOGIN_PAGE = """
<!doctype html><html><body>
<form action="/login" method="post">
  <input name="session_key" type="text">
  <input name="session_password" type="password">
  <button type="submit">Sign in</button>
</form>
</body></html>
"""

COURSE_PAGE = """
<!doctype html><html><body>
<header><img class="global-nav__me-photo" alt="Me"></header>
<aside class="classroom-toc">
  <a href="/lesson/one">One</a>
</aside>
<h1>Test Course</h1>
</body></html>
"""

UNKNOWN_PAGE = """
<!doctype html><html><body><p>Nothing familiar here.</p></body></html>
"""


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base = {
        "headless": True,
        "browser_profile": tmp_path / "profile",
        "database_path": tmp_path / "data" / "progress.db",
        "log_dir": tmp_path / "logs",
        "auth_timeout": 30,
    }
    base.update(overrides)
    return Settings(**base)


async def test_session_persists_cookies_across_restart(tmp_path: Path, site) -> None:
    settings = _settings(tmp_path)
    session = BrowserSession(settings)
    try:
        page = await session.start()
        await page.goto(site.url("missing.html"))
        # Set the cookie through the page itself (a real browser cookie), which
        # Chromium persists to the profile. Cookies injected via the Playwright
        # API are intentionally not used here because they are session-only.
        await page.evaluate("document.cookie = 'session_probe=keep-me; path=/; max-age=3600'")

        page = await session.restart()
        cookies = await session.context.cookies()
        assert any(cookie["name"] == "session_probe" for cookie in cookies)
        assert page.is_closed() is False
    finally:
        await session.close()


async def test_authentication_markers(tmp_path: Path, site) -> None:
    manager = BrowserManager(_settings(tmp_path), Selectors())
    try:
        login_url = site.write("login.html", LOGIN_PAGE)
        await manager.start()
        await manager.open_url(login_url)
        assert await manager.detect_auth_status() is AuthStatus.UNAUTHENTICATED

        course_url = site.write("course.html", COURSE_PAGE)
        await manager.open_url(course_url)
        assert await manager.detect_auth_status() is AuthStatus.AUTHENTICATED

        unknown_url = site.write("unknown.html", UNKNOWN_PAGE)
        await manager.open_url(unknown_url)
        assert await manager.detect_auth_status() is AuthStatus.UNKNOWN
    finally:
        await manager.close()


async def test_wait_for_authentication_after_manual_login(tmp_path: Path, site) -> None:
    manager = BrowserManager(_settings(tmp_path), Selectors())
    try:
        login_url = site.write("login.html", LOGIN_PAGE)
        course_url = site.write("course.html", COURSE_PAGE)
        await manager.start()
        await manager.open_url(login_url)

        async def simulate_manual_login() -> None:
            await asyncio.sleep(0.3)
            await manager.page.goto(course_url)

        task = asyncio.create_task(simulate_manual_login())
        waits: list[float] = []
        await manager.wait_for_authentication(timeout=5, poll_interval=0.2, on_wait=waits.append)
        await task
        assert waits  # callback was invoked while waiting
        assert await manager.detect_auth_status() is AuthStatus.AUTHENTICATED
    finally:
        await manager.close()


async def test_wait_for_authentication_times_out(tmp_path: Path, site) -> None:
    manager = BrowserManager(_settings(tmp_path), Selectors())
    try:
        login_url = site.write("login.html", LOGIN_PAGE)
        await manager.start()
        await manager.open_url(login_url)
        with pytest.raises(Exception, match="Timed out waiting for manual LinkedIn login"):
            await manager.wait_for_authentication(timeout=0.5, poll_interval=0.1)
    finally:
        await manager.close()


async def test_open_course_returns_unknown_page(tmp_path: Path, site) -> None:
    manager = BrowserManager(_settings(tmp_path), Selectors())
    try:
        page = await manager.open_course(site.write("unknown.html", UNKNOWN_PAGE))
        assert "unknown.html" in page.url
    finally:
        await manager.close()


async def test_manager_recovers_closed_page(tmp_path: Path, site) -> None:
    manager = BrowserManager(_settings(tmp_path), Selectors())
    try:
        page = await manager.start()
        await page.goto(site.url("unknown.html"))
        await manager.session.context.close()

        recovered = await manager.ensure_ready()
        assert recovered.is_closed() is False
        assert manager.is_running is True
    finally:
        await manager.close()
