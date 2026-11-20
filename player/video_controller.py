"""HTML5 video control and playback monitoring.

The controller only uses the real ``<video>`` element: it reads the media clock,
starts/pauses playback, sets the player's own ``playbackRate`` and waits for the
media to reach its true end. It never changes completion flags, never seeks
forward and never simulates progress. Platform-side completion is verified
separately against the course curriculum.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel, Field
from playwright.async_api import Page

from config import Settings
from utils.exceptions import (
    ShutdownRequested,
    VideoNotFoundError,
    VideoPlaybackError,
    VideoStalledError,
    VideoTimeoutError,
)
from utils.logger import get_logger
from utils.selectors import Selectors

logger = get_logger(__name__)

# Chromium's HTMLMediaElement accepts 0.0625x-16x. 16x is the hard ceiling;
# 32x raises NotSupportedError. The player may still clamp/reset the value.
HARD_MAX_RATE = 16.0

# Shared JS helper: choose the most relevant video element on the page.
_PICK_VIDEO = """
const pickVideo = (cfg) => {
  let best = null;
  let bestScore = -1;
  for (const selector of cfg.video) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(selector)); } catch (e) { continue; }
    for (const node of nodes) {
      if (!node || node.tagName !== 'VIDEO') continue;
      const duration = Number.isFinite(node.duration) ? node.duration : 0;
      const area = (node.clientWidth || 0) * (node.clientHeight || 0);
      const score = duration * 1e6 + area;
      if (score > bestScore) { bestScore = score; best = node; }
    }
  }
  return best;
};
const uiError = (cfg) => {
  const patterns = /(error occurred|playback error|unable to play|video failed|try again)/i;
  for (const selector of cfg.errorText || []) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(selector)); } catch (e) { continue; }
    for (const node of nodes) {
      const text = (node.innerText || '').trim();
      if (text && patterns.test(text)) return text.slice(0, 200);
    }
  }
  return null;
};
"""

_STATE_JS = (
    """
(cfg) => {
  """
    + _PICK_VIDEO
    + """
  const v = pickVideo(cfg);
  if (!v) {
    return {
      found: false,
      ui_error: uiError(cfg),
      page_url: location.href,
      page_title: document.title,
    };
  }
  const err = v.error ? {code: v.error.code, message: v.error.message || ''} : null;
  return {
    found: true,
    current_time: Number.isFinite(v.currentTime) ? v.currentTime : 0,
    duration: Number.isFinite(v.duration) ? v.duration : 0,
    paused: !!v.paused,
    ended: !!v.ended,
    playback_rate: v.playbackRate || 1,
    ready_state: v.readyState,
    network_state: v.networkState,
    error_code: err ? err.code : null,
    error_message: err ? err.message : null,
    ui_error: uiError(cfg),
    src: v.currentSrc || v.src || null,
    page_url: location.href,
    page_title: document.title,
  };
}
"""
)

_ACTION_JS = (
    """
async (payload) => {
  const cfg = payload.cfg || {};
  const action = payload.action;
  const rate = payload.rate;
  """
    + _PICK_VIDEO
    + """
  const v = pickVideo(cfg);
  if (!v) return {found: false, ok: false, error: 'video element not found'};
  let error = null;
  try {
    if (action === 'play') {
      if (v.ended) return {found: true, ok: false, error: 'video already ended'};
      await v.play();
    } else if (action === 'pause') {
      v.pause();
    } else if (action === 'rate') {
      if (rate > 0) {
        v.playbackRate = rate;
        v.defaultPlaybackRate = rate;
      }
    } else if (action === 'mute') {
      v.muted = true;
    }
  } catch (e) {
    error = String(e && e.message ? e.message : e);
  }
  return {
    found: true,
    ok: error === null,
    error,
    paused: !!v.paused,
    ended: !!v.ended,
    current_time: Number.isFinite(v.currentTime) ? v.currentTime : 0,
    duration: Number.isFinite(v.duration) ? v.duration : 0,
    playback_rate: v.playbackRate || 1,
    ready_state: v.readyState,
  };
}
"""
)


class VideoState(BaseModel):
    found: bool
    current_time: float = 0.0
    duration: float = 0.0
    paused: bool = True
    ended: bool = False
    playback_rate: float = 1.0
    ready_state: int = 0
    network_state: int = 0
    error_code: int | None = None
    error_message: str | None = None
    ui_error: str | None = None
    src: str | None = None
    page_url: str | None = None
    page_title: str | None = None

    @property
    def progress_ratio(self) -> float:
        return compute_progress(self.current_time, self.duration)

    @property
    def has_error(self) -> bool:
        return bool(self.error_code) or bool(self.ui_error)

    def describe(self) -> str:
        if not self.found:
            return "no video element"
        return (
            f"{self.current_time:.1f}/{self.duration:.1f}s at {self.playback_rate:.2f}x "
            f"({'paused' if self.paused else 'playing'})"
        )


class PlaybackResult(BaseModel):
    lesson_key: str | None = None
    start_url: str | None = None
    watch_url: str | None = None
    media_ended: bool = False
    navigated_away: bool = False
    duration: float = 0.0
    watched_seconds: float = 0.0
    final_rate: float = 1.0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def watch_ratio(self) -> float:
        return compute_progress(self.watched_seconds, self.duration)


def compute_progress(current_time: float, duration: float) -> float:
    """Clamp media progress to ``[0, 1]`` without inventing progress."""

    if not duration or duration <= 0 or current_time <= 0:
        return 0.0
    return max(0.0, min(current_time / duration, 1.0))


def clamp_rate(
    requested: float,
    max_rate: float = HARD_MAX_RATE,
    *,
    hard_max: float = HARD_MAX_RATE,
) -> float:
    """Clamp to the user's ceiling and the HTML5 range supported by Chromium."""

    try:
        value = float(requested)
    except (TypeError, ValueError):
        return 1.0
    if value <= 0:
        return 1.0
    ceiling = min(max(float(max_rate), 0.25), hard_max)
    return round(max(0.25, min(value, ceiling)), 2)


def describe_media_error(code: int | None) -> str:
    """Map ``MediaError`` codes to actionable messages."""

    return {
        1: "Playback was aborted by the browser.",
        2: "A network error interrupted the video.",
        3: "The video could not be decoded.",
        4: "The video source is not supported or is unavailable.",
    }.get(code or 0, "The video player reported an unknown error.")


class VideoController:
    """Controls one page's HTML5 player and monitors real playback."""

    def __init__(
        self,
        page_provider: Callable[[], Page],
        selectors: Selectors,
        settings: Settings,
        *,
        poll_interval: float | None = None,
    ) -> None:
        self._page_provider = page_provider
        self.selectors = selectors
        self.settings = settings
        self.poll_interval = poll_interval or settings.poll_interval

    @property
    def page(self) -> Page:
        return self._page_provider()

    @property
    def target_rate(self) -> float:
        return clamp_rate(self.settings.playback_speed, self.settings.max_playback_speed)

    def _config(self) -> dict[str, object]:
        return {
            "video": list(self.selectors.video),
            "errorText": [
                "[class*='error']",
                "[data-test-id*='error']",
                "[role='alert']",
            ],
        }

    # ------------------------------------------------------------------ #
    # State / primitives
    # ------------------------------------------------------------------ #
    async def find_state(self) -> VideoState:
        try:
            raw = await self.page.evaluate(_STATE_JS, self._config())
        except Exception as exc:
            raise VideoNotFoundError(
                f"Could not inspect the video element: {exc}",
                hint="The page may have navigated or the browser was closed.",
            ) from exc
        return VideoState(**raw)

    async def wait_for_video(self, timeout: float = 25.0, *, poll_interval: float | None = None) -> VideoState:
        interval = poll_interval or min(self.poll_interval, 0.5)
        elapsed = 0.0
        last = VideoState(found=False)
        while elapsed < timeout:
            last = await self.find_state()
            if last.found:
                return last
            if last.ui_error:
                raise VideoPlaybackError(last.ui_error)
            await asyncio.sleep(interval)
            elapsed += interval
        raise VideoNotFoundError(
            "No HTML5 video element was found on the lesson page.",
            hint=(
                "The course layout may have changed. Update utils/selectors.py "
                "or open the lesson manually to check."
            ),
        )

    async def _action(self, action: str, rate: float | None = None) -> dict:
        return await self.page.evaluate(
            _ACTION_JS,
            {"cfg": self._config(), "action": action, "rate": rate},
        )

    async def play(self) -> VideoState:
        result = await self._action("play")
        if not result.get("found"):
            raise VideoNotFoundError("The video element disappeared before playback started.")

        if not result.get("ok") and result.get("error"):
            # Programmatic play can be rejected by autoplay policies. Clicking
            # the player's own play control counts as a real user gesture and
            # does not bypass any platform tracking.
            logger.debug("Programmatic play failed: %s", result.get("error"))
            await self._click_play_control()
            await asyncio.sleep(0.3)
            state = await self.find_state()
            if not state.paused or state.ended:
                return state
            raise VideoPlaybackError(
                f"The player refused to start: {result.get('error')}",
                hint="Press play once in the browser window if playback stays paused.",
            )

        await asyncio.sleep(0.2)
        state = await self.find_state()
        if state.paused and not state.ended:
            await self._click_play_control()
            await asyncio.sleep(0.3)
            state = await self.find_state()
        return state

    async def _click_play_control(self) -> None:
        for selector in self.selectors.play_button:
            try:
                locator = self.page.locator(selector).first
                if await locator.is_visible():
                    await locator.click(timeout=2_000)
                    return
            except Exception:
                continue

    async def pause(self) -> VideoState:
        try:
            await self._action("pause")
        except Exception as exc:
            logger.debug("Pause failed: %s", exc)
        return await self.find_state()

    async def set_playback_rate(self, rate: float | None = None) -> float:
        target = clamp_rate(
            self.target_rate if rate is None else rate,
            self.settings.max_playback_speed,
        )
        try:
            result = await self._action("rate", target)
        except Exception as exc:
            logger.warning("Could not set playback rate: %s", exc)
            return 1.0
        actual = float(result.get("playback_rate") or 1.0)
        if abs(actual - target) > 0.01:
            logger.warning(
                "Player adjusted the requested playback rate",
                extra={"event": "playback_rate_clamped", "requested": target, "actual": actual},
            )
        return actual

    # ------------------------------------------------------------------ #
    # Playback monitoring
    # ------------------------------------------------------------------ #
    async def wait_for_completion(
        self,
        *,
        lesson_key: str | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[VideoState, float], None] | None = None,
        timeout: float | None = None,
        start_url: str | None = None,
    ) -> PlaybackResult:
        """Play until the media genuinely ends, honouring pause/stop requests.

        Raises :class:`VideoPlaybackError`, :class:`VideoStalledError`,
        :class:`VideoTimeoutError` or :class:`ShutdownRequested`.
        Media end is reported as ``media_ended``; platform completion is
        verified by the course navigator.
        """

        initial = await self.wait_for_video()
        rate = await self.set_playback_rate()
        await self.play()

        current = await self.find_state()
        if not current.found:
            raise VideoNotFoundError("The video element disappeared right after start.")

        state = current
        start_url = start_url or state.page_url or self.page.url
        started = datetime.now(timezone.utc)
        deadline = time.monotonic() + (timeout or self.settings.playback_timeout)

        last_position = state.current_time
        last_advance = time.monotonic()
        missing_since: float | None = None
        paused_by_tool = False
        nudges = 0
        max_nudges = 3
        watched = state.current_time
        duration = state.duration

        while time.monotonic() < deadline:
            if should_stop and should_stop():
                await self.pause()
                raise ShutdownRequested()

            pause_requested = bool(should_pause and should_pause())
            if pause_requested:
                if not paused_by_tool:
                    await self.pause()
                    paused_by_tool = True
                    logger.info("Playback paused by user", extra={"event": "playback_paused", "lesson": lesson_key})
                await asyncio.sleep(self.poll_interval)
                continue

            if paused_by_tool:
                rate = await self.set_playback_rate()
                await self.play()
                paused_by_tool = False
                last_advance = time.monotonic()
                last_position = (await self.find_state()).current_time

            state = await self.find_state()
            now = time.monotonic()

            if not state.found:
                if self.page.url != start_url:
                    logger.info(
                        "Player navigated away (likely auto-advance)",
                        extra={"event": "playback_navigated", "lesson": lesson_key},
                    )
                    return PlaybackResult(
                        lesson_key=lesson_key,
                        start_url=start_url,
                        watch_url=self.page.url,
                        media_ended=True,
                        navigated_away=True,
                        duration=duration,
                        watched_seconds=watched,
                        final_rate=rate,
                        started_at=started,
                    )
                missing_since = missing_since or now
                if now - missing_since > 15:
                    raise VideoNotFoundError("The video element disappeared during playback.")
                await asyncio.sleep(self.poll_interval)
                continue
            missing_since = None

            if state.error_code:
                raise VideoPlaybackError(
                    describe_media_error(state.error_code),
                    code=state.error_code,
                )
            if state.ui_error and state.ui_error.lower() not in {"error", "retry"}:
                raise VideoPlaybackError(state.ui_error)

            if state.duration > duration:
                duration = state.duration

            if state.ended or (duration > 0 and state.current_time >= duration - 0.2):
                await self.pause()
                watched = max(watched, min(state.current_time or duration, duration) if duration else state.current_time)
                logger.info(
                    "Media reached the end",
                    extra={
                        "event": "media_ended",
                        "lesson": lesson_key,
                        "watched_seconds": round(watched, 1),
                        "duration": round(duration, 1),
                    },
                )
                return PlaybackResult(
                    lesson_key=lesson_key,
                    start_url=start_url,
                    watch_url=self.page.url,
                    media_ended=True,
                    duration=duration,
                    watched_seconds=watched,
                    final_rate=state.playback_rate or rate,
                    started_at=started,
                )

            if state.current_time > last_position + 0.01:
                watched = max(watched, state.current_time)
                last_position = state.current_time
                last_advance = now
                nudges = 0
            elif state.ready_state < 2:
                # Buffering is normal; give it more time before calling it a stall.
                last_advance = max(last_advance, now - self.settings.stall_timeout * 0.5)

            if abs((state.playback_rate or 1.0) - rate) > 0.05:
                rate = await self.set_playback_rate()

            if state.paused and not state.ended and now - last_advance > 2.0:
                nudges += 1
                if nudges > max_nudges:
                    raise VideoStalledError("Playback repeatedly paused itself.")
                await self.play()
                last_advance = now

            if now - last_advance > self.settings.stall_timeout and state.ready_state >= 2:
                nudges += 1
                if nudges > max_nudges:
                    raise VideoStalledError(
                        f"Playback stopped advancing at {state.current_time:.1f}s."
                    )
                logger.warning(
                    "Playback stalled; nudging the player",
                    extra={"event": "playback_stall", "lesson": lesson_key, "position": state.current_time},
                )
                await self.play()
                last_advance = now

            if on_progress:
                on_progress(state, watched)

            await asyncio.sleep(self.poll_interval)

        await self.pause()
        raise VideoTimeoutError(
            f"Playback did not finish within {timeout or self.settings.playback_timeout:.0f}s "
            f"(position {last_position:.1f}s of {duration:.1f}s)."
        )
