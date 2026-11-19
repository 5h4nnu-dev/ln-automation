"""Rich dashboard rendering and live refresh.

The dashboard is a pure view over an :class:`AppState` object owned by the
runner. It never touches Playwright or SQLite. Interactive prompts temporarily
suspend the live region so terminal input and assessment panels stay readable.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable, Sequence

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from database.models import ExecutionStatus

STATUS_STYLE = {
    ExecutionStatus.IDLE: ("white", "idle"),
    ExecutionStatus.STARTING: ("yellow", "starting"),
    ExecutionStatus.RUNNING: ("green", "running"),
    ExecutionStatus.PAUSED: ("yellow", "paused"),
    ExecutionStatus.WAITING: ("cyan", "waiting"),
    ExecutionStatus.AUTH_REQUIRED: ("magenta", "login required"),
    ExecutionStatus.COMPLETED: ("green", "finished"),
    ExecutionStatus.STOPPED: ("bright_black", "stopped"),
    ExecutionStatus.ERROR: ("red", "error"),
}


@dataclass
class Activity:
    time: str
    level: str
    message: str


@dataclass
class AppState:
    status: ExecutionStatus = ExecutionStatus.IDLE
    course_title: str = ""
    course_url: str = ""
    course_index: int = 0
    course_total: int = 0
    total_lessons: int = 0
    completed: int = 0
    skipped: int = 0
    locked: int = 0
    current_lesson: str | None = None
    current_section: str | None = None
    playback_speed: float = 2.0
    playback_position: float = 0.0
    playback_duration: float = 0.0
    lesson_watched_seconds: float = 0.0
    total_watched_seconds: float = 0.0
    assessments: int = 0
    detail: str = ""
    last_error: str | None = None
    started_at: datetime | None = None
    browser_ready: bool = False
    activities: deque[Activity] = field(default_factory=lambda: deque(maxlen=7))

    @property
    def remaining(self) -> int:
        return max(self.total_lessons - self.completed, 0)

    @property
    def progress_fraction(self) -> float:
        if self.total_lessons <= 0:
            return 0.0
        return max(0.0, min(self.completed / self.total_lessons, 1.0))

    def add_activity(self, message: str, level: str = "INFO") -> None:
        self.activities.append(
            Activity(time=datetime.now().strftime("%H:%M:%S"), level=level.upper(), message=message)
        )


def _status_text(status: ExecutionStatus) -> Text:
    style, label = STATUS_STYLE.get(status, ("white", status.value))
    return Text.assemble(("● ", style), (label.upper(), f"bold {style}"))


def _fmt_seconds(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "0:00"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class Dashboard:
    """Live terminal dashboard with a suspendable live region."""

    def __init__(
        self,
        console: Console,
        state_provider: Callable[[], AppState],
        *,
        refresh_per_second: int = 4,
    ) -> None:
        self.console = console
        self.state_provider = state_provider
        self.refresh_per_second = refresh_per_second
        self._live = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._interactive_depth = 0
        self._suspended_by_interactive = False

    # ------------------------------------------------------------------ #
    # Live lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._started:
            return
        from rich.live import Live

        self._started = True
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=self.refresh_per_second,
            transient=False,
            auto_refresh=False,
        )
        self._live.start(refresh=True)
        self._task = asyncio.create_task(self._refresh_loop(), name="dashboard-refresh")

    async def _refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.35)
                if self._live is not None:
                    self._live.update(self.render(), refresh=True)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._started = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    @asynccontextmanager
    async def interactive(self) -> AsyncIterator[None]:
        """Stop the live region while the coordinator prompts the user.

        Nested uses are supported (for example a command typed while an
        assessment is being displayed). Only the outermost block suspends and
        restores the live region; the lock is never held across the ``yield``.
        """

        suspend = False
        async with self._lock:
            self._interactive_depth += 1
            if self._interactive_depth == 1 and self._started:
                self._suspended_by_interactive = True
                suspend = True

        if suspend:
            await self.stop()
        try:
            yield
        finally:
            resume = False
            async with self._lock:
                self._interactive_depth = max(0, self._interactive_depth - 1)
                if self._interactive_depth == 0 and self._suspended_by_interactive:
                    self._suspended_by_interactive = False
                    resume = True
            if resume:
                await self.start()

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def render(self) -> Panel:
        state = self.state_provider()

        header = Table.grid(expand=True)
        header.add_column(ratio=1)
        header.add_column(justify="right")
        course_label = state.course_title or "No course loaded"
        if state.course_total > 1:
            course_label = f"[{state.course_index}/{state.course_total}] {course_label}"
        header.add_row(
            Text(course_label, style="bold white"),
            _status_text(state.status),
        )
        if state.course_url:
            header.add_row(Text(state.course_url, style="dim"), Text(""))

        counts = Table.grid(expand=True)
        for _ in range(4):
            counts.add_column(ratio=1)
        counts.add_row(
            Text.assemble(("Total\n", "dim"), (str(state.total_lessons), "bold")),
            Text.assemble(("Completed\n", "dim"), (str(state.completed), "bold green")),
            Text.assemble(("Remaining\n", "dim"), (str(state.remaining), "bold yellow")),
            Text.assemble(
                ("Skipped / Locked\n", "dim"),
                (f"{state.skipped} / {state.locked}", "bold bright_black"),
            ),
        )

        bar = ProgressBar(
            total=max(state.total_lessons, 1),
            completed=min(state.completed, max(state.total_lessons, 1)),
            width=38,
            complete_style="green",
            finished_style="green",
            pulse_style="green",
        )
        percent = int(round(state.progress_fraction * 100))
        progress_line = Table.grid(expand=True)
        progress_line.add_column(ratio=1)
        progress_line.add_column(justify="right")
        progress_line.add_row(bar, Text(f"{percent:>3}%", style="bold"))

        lesson_lines: list[Text] = []
        if state.current_lesson:
            section = f"  ({state.current_section})" if state.current_section else ""
            lesson_lines.append(Text.assemble(("Current lesson\n", "dim"), (state.current_lesson + section, "bold")))
        if state.playback_duration > 0:
            lesson_lines.append(
                Text.assemble(
                    (f"Playback {state.playback_speed:.2f}x  ", "cyan"),
                    (
                        f"{_fmt_seconds(state.playback_position)} / {_fmt_seconds(state.playback_duration)}",
                        "white",
                    ),
                    (f"   watched {_fmt_seconds(state.lesson_watched_seconds)}", "dim"),
                )
            )
        else:
            lesson_lines.append(Text(f"Playback speed {state.playback_speed:.2f}x", style="cyan"))
        if state.detail:
            lesson_lines.append(Text(state.detail, style="dim"))

        activity_lines: list[Text] = []
        if state.activities:
            activity_lines.append(Text("Recent activity", style="dim underline"))
            for item in list(state.activities)[-5:]:
                style = "red" if item.level in {"ERROR", "CRITICAL"} else (
                    "yellow" if item.level == "WARNING" else "white"
                )
                activity_lines.append(
                    Text.assemble(
                        (f"{item.time} ", "bright_black"),
                        (f"{item.level:<7} ", "bright_black"),
                        (item.message, style),
                    )
                )
        else:
            activity_lines.append(Text("Waiting to start…", style="dim"))

        if state.last_error:
            activity_lines.append(Text.assemble(("Last error: ", "bold red"), (state.last_error, "red")))

        footer = Text(
            "Commands: start · pause · resume · progress · speed <x> · stop · exit",
            style="bright_black",
        )

        body = Group(
            header,
            Text(""),
            counts,
            progress_line,
            Text(""),
            *lesson_lines,
            Text(""),
            *activity_lines,
            Text(""),
            footer,
        )
        return Panel(body, title="LINKEDIN LEARNING AUTOMATION", border_style="cyan", padding=(0, 1))


def summary_panel(state: AppState) -> Panel:
    """Final (non-live) summary of a finished run."""

    lines: list[Text] = []
    if state.course_total > 1:
        lines.append(
            Text.assemble(("Batch course: ", "dim"), (f"{state.course_index}/{state.course_total}", "bold cyan"))
        )
    lines.extend(
        [
            Text.assemble(("Course: ", "dim"), (state.course_title or "unknown", "bold")),
            Text.assemble(("Status: ", "dim"), _status_text(state.status)),
            Text(""),
            Text.assemble(("Lessons discovered: ", "dim"), (str(state.total_lessons), "bold")),
            Text.assemble(("Completed: ", "dim"), (str(state.completed), "bold green")),
            Text.assemble(("Remaining: ", "dim"), (str(state.remaining), "bold yellow")),
            Text.assemble(("Skipped: ", "dim"), (str(state.skipped), "yellow")),
            Text.assemble(("Locked / unavailable: ", "dim"), (str(state.locked), "bright_black")),
            Text.assemble(("Watch time recorded: ", "dim"), (_fmt_seconds(state.total_watched_seconds), "cyan")),
            Text.assemble(("Assessments shown: ", "dim"), (str(state.assessments), "cyan")),
        ]
    )
    if state.detail:
        lines.extend([Text(""), Text(state.detail, style="dim")])
    if state.last_error:
        lines.extend([Text(""), Text.assemble(("Last error: ", "bold red"), (state.last_error, "red"))])
    if state.remaining or state.skipped:
        lines.extend(
            [
                Text(""),
                Text(
                    "Run the tool again to resume pending lessons; assessments must be "
                    "submitted manually in the browser.",
                    style="dim",
                ),
            ]
        )
    return Panel(
        Group(*lines),
        title="SESSION COMPLETE" if state.status is ExecutionStatus.COMPLETED else "SESSION SUMMARY",
        border_style="green" if state.status is ExecutionStatus.COMPLETED else "yellow",
        padding=(0, 1),
    )


def progress_table(state: AppState) -> Table:
    table = Table(title="Course Progress", show_header=True, header_style="bold cyan", expand=False)
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("Status", _status_text(state.status))
    if state.course_total > 1:
        table.add_row("Batch course", f"{state.course_index}/{state.course_total}")
    table.add_row("Course", state.course_title or "—")
    table.add_row("URL", state.course_url or "—")
    table.add_row("Total lessons", str(state.total_lessons))
    table.add_row("Completed", str(state.completed))
    table.add_row("Remaining", str(state.remaining))
    table.add_row("Skipped", str(state.skipped))
    table.add_row("Locked", str(state.locked))
    table.add_row("Current lesson", state.current_lesson or "—")
    table.add_row("Playback speed", f"{state.playback_speed:.2f}x")
    table.add_row("Watch time recorded", _fmt_seconds(state.total_watched_seconds))
    table.add_row("Progress", f"{int(round(state.progress_fraction * 100))}%")
    return table


@dataclass
class CourseOutcome:
    """One course's result inside a multi-course batch run."""

    url: str
    title: str = ""
    status: ExecutionStatus = ExecutionStatus.STOPPED
    completed: int = 0
    total: int = 0
    error: str | None = None


def batch_summary_table(outcomes: Sequence[CourseOutcome]) -> Table:
    """Compact final report for a batch of courses."""

    table = Table(title="BATCH SUMMARY", header_style="bold cyan", expand=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Course", style="bold", overflow="fold", max_width=52)
    table.add_column("Status", no_wrap=True)
    table.add_column("Lessons", justify="right")
    table.add_column("Error", style="red", overflow="fold", max_width=40)

    for index, outcome in enumerate(outcomes, start=1):
        label = outcome.title or outcome.url
        error = outcome.error or "—"
        table.add_row(
            str(index),
            label,
            _status_text(outcome.status),
            f"{outcome.completed}/{outcome.total}",
            error,
        )
    return table
