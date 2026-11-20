"""Video controller tests: helpers, real playback, pause, errors."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import Settings
from player.video_controller import (
    VideoController,
    VideoState,
    clamp_rate,
    compute_progress,
    describe_media_error,
)
from utils.exceptions import VideoNotFoundError
from utils.selectors import Selectors

PLAYER_HTML = """
<!doctype html>
<html><body>
  <video id="lesson" src="/media/sample.webm" muted playsinline preload="auto"></video>
</body></html>
"""

EMPTY_HTML = """
<!doctype html>
<html><body><main>No player here</main></body></html>
"""


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "headless": True,
        "poll_interval": 0.1,
        "stall_timeout": 8,
        "playback_timeout": 60,
        "playback_speed": 4.0,
        "max_playback_speed": 4.0,
        "database_path": tmp_path / "progress.db",
        "log_dir": tmp_path / "logs",
        "browser_profile": tmp_path / "profile",
    }
    base.update(overrides)
    return Settings(**base)


def _controller(page, settings: Settings) -> VideoController:
    return VideoController(lambda: page, Selectors(), settings)


async def _open_player(page, site, video_file: Path) -> None:
    site.write_bytes("media/sample.webm", video_file.read_bytes())
    url = site.write("player.html", PLAYER_HTML)
    await page.goto(url)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_compute_progress_is_clamped() -> None:
    assert compute_progress(0, 100) == 0.0
    assert compute_progress(50, 100) == 0.5
    assert compute_progress(150, 100) == 1.0
    assert compute_progress(10, 0) == 0.0
    assert compute_progress(-5, 100) == 0.0


@pytest.mark.parametrize(
    ("requested", "maximum", "expected"),
    [
        (2.0, 2.0, 2.0),
        (1.5, 2.0, 1.5),
        (3.0, 2.0, 2.0),
        (5.0, 4.0, 4.0),
        (16.0, 16.0, 16.0),
        (20.0, 16.0, 16.0),
        (0.1, 2.0, 0.25),
        (0.0, 2.0, 1.0),
        (-1.0, 2.0, 1.0),
    ],
)
def test_clamp_rate(requested: float, maximum: float, expected: float) -> None:
    assert clamp_rate(requested, maximum) == expected


def test_media_error_descriptions() -> None:
    assert "network" in describe_media_error(2).lower()
    assert "decode" in describe_media_error(3).lower()
    assert "unknown" in describe_media_error(None).lower()


def test_state_progress_and_description() -> None:
    state = VideoState(found=True, current_time=30, duration=120)
    assert state.progress_ratio == 0.25
    assert "30.0/120.0s" in state.describe()
    assert "no video element" in VideoState(found=False).describe()


# --------------------------------------------------------------------------- #
# Real Chromium playback
# --------------------------------------------------------------------------- #
async def test_detects_video_metadata(page, site, video_file: Path, tmp_path: Path) -> None:
    await _open_player(page, site, video_file)
    controller = _controller(page, _settings(tmp_path))

    state = await controller.wait_for_video(timeout=5)
    assert state.found is True
    assert 2.5 <= state.duration <= 3.5
    assert state.paused is True
    assert state.src and "sample.webm" in state.src


async def test_play_pause_and_speed(page, site, video_file: Path, tmp_path: Path) -> None:
    await _open_player(page, site, video_file)
    controller = _controller(page, _settings(tmp_path))

    rate = await controller.set_playback_rate(2.0)
    assert rate == pytest.approx(2.0, abs=0.01)

    state = await controller.play()
    assert state.paused is False
    await asyncio.sleep(0.6)
    state = await controller.find_state()
    assert state.current_time > 0.1
    assert state.playback_rate == pytest.approx(2.0, abs=0.01)

    state = await controller.pause()
    assert state.paused is True
    frozen_at = state.current_time
    await asyncio.sleep(0.4)
    state = await controller.find_state()
    assert state.current_time == pytest.approx(frozen_at, abs=0.2)


async def test_sixteen_x_rate_is_supported_by_chromium(
    page, site, video_file: Path, tmp_path: Path
) -> None:
    await _open_player(page, site, video_file)
    controller = _controller(
        page,
        _settings(tmp_path, playback_speed=16.0, max_playback_speed=16.0),
    )

    rate = await controller.set_playback_rate()
    assert rate == pytest.approx(16.0, abs=0.1)
    state = await controller.find_state()
    assert state.playback_rate == pytest.approx(16.0, abs=0.1)


async def test_wait_for_completion_with_real_media(page, site, video_file: Path, tmp_path: Path) -> None:
    await _open_player(page, site, video_file)
    controller = _controller(page, _settings(tmp_path))

    progress: list[float] = []
    result = await controller.wait_for_completion(
        lesson_key="intro",
        on_progress=lambda state, watched: progress.append(watched),
    )

    assert result.media_ended is True
    assert result.navigated_away is False
    assert 2.5 <= result.duration <= 3.5
    assert result.watched_seconds >= result.duration - 0.5
    assert result.watch_ratio >= 0.9
    assert result.final_rate == pytest.approx(4.0, abs=0.1)
    assert progress  # the callback observed playback


async def test_missing_video_raises(page, site, tmp_path: Path) -> None:
    url = site.write("empty.html", EMPTY_HTML)
    await page.goto(url)
    controller = _controller(page, _settings(tmp_path))
    with pytest.raises(VideoNotFoundError):
        await controller.wait_for_video(timeout=0.5)


async def test_pause_request_then_resume(page, site, video_file: Path, tmp_path: Path) -> None:
    await _open_player(page, site, video_file)
    controller = _controller(page, _settings(tmp_path, playback_speed=2.0, max_playback_speed=2.0))

    paused = [True]
    task = asyncio.create_task(
        controller.wait_for_completion(
            lesson_key="pause-test",
            should_pause=lambda: paused[0],
        )
    )
    await asyncio.sleep(0.7)
    state = await controller.find_state()
    assert state.paused is True

    paused[0] = False
    result = await asyncio.wait_for(task, timeout=20)
    assert result.media_ended is True
