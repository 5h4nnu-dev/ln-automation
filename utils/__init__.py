"""Shared logging, selector and exception utilities."""

from utils.exceptions import (
    AssessmentError,
    AuthenticationRequiredError,
    AutomationError,
    BrowserClosedError,
    BrowserError,
    ConfigurationError,
    CourseListError,
    CurriculumError,
    LessonAccessError,
    NavigationError,
    ShutdownRequested,
    StorageError,
    VideoError,
    VideoNotFoundError,
    VideoPlaybackError,
    VideoStalledError,
    VideoTimeoutError,
)
from utils.selectors import Selectors, load_selectors

__all__ = [
    "AssessmentError",
    "AuthenticationRequiredError",
    "AutomationError",
    "BrowserClosedError",
    "BrowserError",
    "ConfigurationError",
    "CourseListError",
    "CurriculumError",
    "LessonAccessError",
    "NavigationError",
    "Selectors",
    "ShutdownRequested",
    "StorageError",
    "VideoError",
    "VideoNotFoundError",
    "VideoPlaybackError",
    "VideoStalledError",
    "VideoTimeoutError",
    "load_selectors",
]
