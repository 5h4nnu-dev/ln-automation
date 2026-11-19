"""Shared pytest fixtures: real Chromium, a generated sample video, local site."""

from __future__ import annotations

import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, async_playwright

CHROMIUM_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
    "--disable-dev-shm-usage",
]


class LocalSite:
    """A throwaway HTTP server for offline DOM/playback integration tests."""

    def __init__(self, root: Path, server: ThreadingHTTPServer) -> None:
        self.root = root
        self._server = server
        host, port = server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def url(self, path: str = "") -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def write(self, path: str, content: str) -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self.url(path)

    def write_bytes(self, path: str, content: bytes) -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return self.url(path)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D401 - silence test server
        return


@pytest.fixture
def site(tmp_path: Path) -> Iterator[LocalSite]:
    root = tmp_path / "site"
    root.mkdir()
    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = LocalSite(root, server)
    try:
        yield local
    finally:
        local.stop()
        thread.join(timeout=5)



@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    try:
        async with async_playwright() as playwright:
            instance = await playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            try:
                yield instance
            finally:
                await instance.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright Chromium is unavailable: {exc}")


@pytest_asyncio.fixture
async def page(browser: Browser) -> AsyncIterator[Page]:
    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    new_page = await context.new_page()
    try:
        yield new_page
    finally:
        await context.close()


@pytest.fixture(scope="session")
def video_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real short WebM clip so playback tests exercise actual media."""

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required to generate the sample video")

    target = tmp_path_factory.mktemp("media") / "sample.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15:duration=3",
            "-c:v",
            "libvpx",
            "-b:v",
            "300k",
            str(target),
        ],
        check=True,
    )
    return target
