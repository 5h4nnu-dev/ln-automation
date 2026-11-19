"""Typed exception hierarchy used across the application.

Every browser or platform failure is translated into one of these exceptions so
callers can decide whether to retry, pause for the user, or skip a lesson.
"""

from __future__ import annotations


class AutomationError(Exception):
    """Base class for all expected automation failures."""

    def __init__(self, message: str, *, hint: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message} ({self.hint})"
        return self.message


class ConfigurationError(AutomationError):
    """Invalid or missing configuration."""


class CourseListError(ConfigurationError):
    """A CSV/text course list could not be read or contained invalid URLs."""


class BrowserError(AutomationError):
    """The browser could not be launched or navigated."""


class BrowserClosedError(BrowserError):
    """The Chromium context/page disappeared (crash or manual close)."""

    def __init__(self, message: str = "The browser window was closed or crashed.") -> None:
        super().__init__(message, hint="Restart the automation to reopen the browser.", retryable=False)


class AuthenticationRequiredError(AutomationError):
    """LinkedIn requires a manual login before the course can proceed."""

    def __init__(self, message: str = "LinkedIn authentication is required.") -> None:
        super().__init__(
            message,
            hint="Log in inside the opened Chromium window; the tool will resume automatically.",
            retryable=True,
        )


class NavigationError(AutomationError):
    """The requested page/lesson could not be opened."""


class CurriculumError(AutomationError):
    """The course curriculum could not be discovered."""


class LessonAccessError(AutomationError):
    """A lesson is locked, unavailable, or rejected by the platform."""


class VideoError(AutomationError):
    """Base class for HTML5 video failures."""


class VideoNotFoundError(VideoError):
    """No playable HTML5 video element was found on the page."""


class VideoPlaybackError(VideoError):
    """The player reported a media error."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message, hint="The lesson will be retried.", retryable=True)
        self.code = code


class VideoStalledError(VideoError):
    """Playback stopped advancing even though the video is not paused."""

    def __init__(self, message: str = "Video playback stalled.") -> None:
        super().__init__(message, hint="Check the network connection and retry.", retryable=True)


class VideoTimeoutError(VideoError):
    """Playback did not finish within the allotted time."""


class AssessmentError(AutomationError):
    """An assessment was detected but could not be extracted."""


class StorageError(AutomationError):
    """Progress persistence failed."""


class ShutdownRequested(AutomationError):
    """Raised internally to unwind the runner during a graceful shutdown."""

    def __init__(self) -> None:
        super().__init__("Shutdown requested.")
