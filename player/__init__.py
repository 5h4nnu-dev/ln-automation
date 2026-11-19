"""HTML5 video playback control and monitoring."""

from player.video_controller import (
    PlaybackResult,
    VideoController,
    VideoState,
    clamp_rate,
    compute_progress,
)

__all__ = [
    "PlaybackResult",
    "VideoController",
    "VideoState",
    "clamp_rate",
    "compute_progress",
]
