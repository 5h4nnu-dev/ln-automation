"""Interactive terminal layer: command console and assessment panels.

This module contains no Playwright or database logic. It renders questions and
suggestions, waits for the user to submit answers *in the browser*, and parses
operator commands such as ``pause`` and ``speed 2.5``.

The assessment code here deliberately has no ability to click, select, type or
submit an answer. The only browser interaction is read-only polling through the
injected :class:`~assessment.detector.AssessmentDetector`.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, TextIO

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from assessment.assistant import Confidence, Suggestion
from assessment.extractor import Question
from utils.exceptions import ShutdownRequested

CONFIDENCE_STYLE = {
    Confidence.HIGH: "bold green",
    Confidence.MEDIUM: "bold yellow",
    Confidence.LOW: "bold red",
}

MANUAL_INSTRUCTION = (
    "Select and submit your answer manually in the browser. "
    "This tool never selects or submits an assessment answer."
)


# --------------------------------------------------------------------------- #
# Assessment rendering
# --------------------------------------------------------------------------- #
def assessment_panel(
    question: Question,
    suggestion: Suggestion | None = None,
    *,
    index: int | None = None,
    total: int | None = None,
) -> Panel:
    """Render one question with lettered options and an optional suggestion."""

    title = "ASSESSMENT DETECTED"
    if index is not None and total is not None and total > 0:
        title = f"ASSESSMENT DETECTED — {index}/{total}"

    body: list[Any] = [
        Text("Question", style="bold cyan"),
        Text(question.prompt, style="bold white"),
        Text(""),
        Text("Options", style="bold cyan"),
    ]
    for option in question.options:
        body.append(
            Text.assemble(
                (f"{option.key}. ", "bold yellow"),
                (option.text, "white"),
            )
        )

    if question.kind == "multi_select":
        body.extend(
            [
                Text(""),
                Text("This question may allow multiple selections.", style="yellow"),
            ]
        )

    if suggestion is not None:
        body.append(Text(""))
        if suggestion.option_key:
            label = f"{suggestion.option_key}. {suggestion.option_text or ''}".strip()
            body.append(Text.assemble(("Suggested answer: ", "bold cyan"), (label, "bold green")))
        else:
            body.append(
                Text.assemble(
                    ("Suggested answer: ", "bold cyan"),
                    ("none — insufficient signal", "bold red"),
                )
            )
        body.extend(
            [
                Text(""),
                Text("Explanation", style="bold cyan"),
                Text(suggestion.explanation, style="white"),
                Text(""),
                Text.assemble(
                    ("Confidence: ", "bold cyan"),
                    (suggestion.confidence.value, CONFIDENCE_STYLE.get(suggestion.confidence, "white")),
                ),
            ]
        )
        for note in suggestion.notes:
            body.append(Text.assemble(("Note: ", "dim"), (note, "yellow")))
        body.extend([Text(""), Text(suggestion.disclaimer, style="dim italic")])

    body.extend([Text(""), Text(MANUAL_INSTRUCTION, style="bold yellow")])
    return Panel(
        Group(*body),
        title=title,
        border_style="magenta",
        padding=(0, 1),
    )


def waiting_panel(message: str) -> Panel:
    return Panel(
        Text(message, style="cyan"),
        title="WAITING FOR YOU",
        border_style="cyan",
        padding=(0, 1),
    )


def command_help() -> Table:
    table = Table(title="Commands", title_style="bold cyan", show_header=True, header_style="bold")
    table.add_column("Command", style="bold green", no_wrap=True)
    table.add_column("Description")
    table.add_row("start", "Resume the automation (starts it if idle)")
    table.add_row("pause", "Pause after the current playback poll")
    table.add_row("resume", "Continue a paused run")
    table.add_row("progress", "Show the persisted course progress")
    table.add_row("speed <x>", "Change playback speed (0.25–4.0)")
    table.add_row("exit", "Graceful shutdown; progress is saved")
    table.add_row("help", "Show this list")
    return table


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
class Command(str, Enum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    PROGRESS = "progress"
    SPEED = "speed"
    EXIT = "exit"
    HELP = "help"


@dataclass(frozen=True)
class ParsedCommand:
    name: Command
    argument: str | None = None


_ALIASES: dict[str, Command] = {
    "start": Command.START,
    "run": Command.START,
    "begin": Command.START,
    "pause": Command.PAUSE,
    "stop": Command.PAUSE,  # "stop" pauses here; use "exit" for shutdown
    "resume": Command.RESUME,
    "continue": Command.RESUME,
    "progress": Command.PROGRESS,
    "status": Command.PROGRESS,
    "speed": Command.SPEED,
    "rate": Command.SPEED,
    "exit": Command.EXIT,
    "quit": Command.EXIT,
    "help": Command.HELP,
    "?": Command.HELP,
}


def parse_command(line: str) -> ParsedCommand | None:
    """Parse a console line; returns ``None`` for empty or unknown input."""

    cleaned = (line or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split()
    name = parts[0].lower()
    argument: str | None = None

    # Accept "set speed 2.5" and "speed=2.5" as well as "speed 2.5".
    if name in {"set", "change"} and len(parts) >= 2:
        name = parts[1].lower()
        parts = [name, *parts[2:]]
    if "=" in name:
        name, _, value = name.partition("=")
        argument = value or None
        parts = [name, *parts[1:]]

    command = _ALIASES.get(name)
    if command is None:
        return None

    if command is Command.SPEED:
        if argument is None and len(parts) >= 2:
            argument = parts[1]
    return ParsedCommand(command, argument)


# --------------------------------------------------------------------------- #
# Terminal UI
# --------------------------------------------------------------------------- #
class TerminalUI:
    """Assessment panels and command-driven output.

    ``dashboard`` is optional so non-interactive runs (and tests) can use the
    same code without a Rich live region.
    """

    def __init__(self, console: Console, dashboard: Any | None = None) -> None:
        self.console = console
        self.dashboard = dashboard
        self._input_closed = False

    async def print(self, renderable: Any) -> None:
        if self.dashboard is not None:
            async with self.dashboard.interactive():
                self.console.print(renderable)
        else:
            self.console.print(renderable)

    async def show_assessment(
        self,
        question: Question,
        suggestion: Suggestion | None,
        *,
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        await self.print(assessment_panel(question, suggestion, index=index, total=total))

    async def show_help(self) -> None:
        await self.print(command_help())

    async def wait_for_manual_submission(
        self,
        detector: Any,
        *,
        timeout: float,
        should_stop: Callable[[], bool] | None = None,
        poll_interval: float = 1.0,
        reminder_after: float = 45.0,
        panels: list[Any] | None = None,
    ) -> bool:
        """Show a waiting panel until the quiz disappears or the timeout expires.

        ``panels`` (the rendered assessment panels) are printed inside the same
        suspended-live block so the live dashboard cannot overwrite them. The
        detector only reads the page; the user submits in the Chromium window.
        """

        async def _poll() -> bool:
            deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
            last_nag = asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() < deadline:
                if should_stop and should_stop():
                    raise ShutdownRequested()
                try:
                    remaining = await detector.detect()
                except Exception:
                    remaining = []
                if not remaining:
                    return True
                await asyncio.sleep(max(0.2, poll_interval))
                now = asyncio.get_running_loop().time()
                if now - last_nag >= reminder_after:
                    last_nag = now
                    await self.print(
                        waiting_panel(
                            "The assessment is still open. Submit it in the browser "
                            "window when you are ready; press Ctrl+C or use `exit` to stop."
                        )
                    )
            return False

        if self.dashboard is not None:
            async with self.dashboard.interactive():
                for panel in panels or []:
                    self.console.print(panel)
                self.console.print(
                    waiting_panel(
                        "The question above is still open in the browser. "
                        "Submit your answer there; monitoring will resume automatically."
                    )
                )
                return await _poll()

        for panel in panels or []:
            self.console.print(panel)
        self.console.print(
            waiting_panel(
                "The question above is still open in the browser. "
                "Submit your answer there; monitoring will resume automatically."
            )
        )
        return await _poll()


class CommandConsole:
    """Reads operator commands and dispatches them to async handlers.

    Handlers are supplied by ``main.py`` and return an optional Rich renderable
    or string that is printed to the console.
    """

    def __init__(
        self,
        ui: TerminalUI,
        handlers: Mapping[Command, Callable[[str | None], Awaitable[Any]]],
        *,
        stream: TextIO | None = None,
    ) -> None:
        self.ui = ui
        self.handlers = dict(handlers)
        self.stream = stream if stream is not None else sys.stdin
        self._task: asyncio.Task | None = None

    async def dispatch(self, line: str) -> bool:
        """Execute one command line. Returns ``False`` when the app should exit."""

        parsed = parse_command(line)
        if parsed is None:
            if (line or "").strip():
                await self.ui.print(
                    Text(f"Unknown command: {line.strip()}", style="bold red")
                )
            await self.ui.show_help()
            return True

        if parsed.name is Command.HELP:
            await self.ui.show_help()
            return True

        handler = self.handlers.get(parsed.name)
        if handler is None:
            await self.ui.print(
                Text(f"The {parsed.name.value} command is not available right now.", style="yellow")
            )
            return parsed.name is not Command.EXIT

        try:
            result = await handler(parsed.argument)
        except ShutdownRequested:
            return False
        except Exception as exc:  # command failures must not kill the run
            await self.ui.print(Text(f"Command failed: {exc}", style="bold red"))
            return True

        if result is not None:
            if isinstance(result, str):
                renderable: Any = Text(result)
            else:
                renderable = result
            await self.ui.print(renderable)
        return parsed.name is not Command.EXIT

    # ------------------------------------------------------------------ #
    # stdin listener
    # ------------------------------------------------------------------ #
    @asynccontextmanager
    async def listen(self) -> AsyncIterator[None]:
        """Listen for typed commands on a TTY without blocking the event loop."""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()
        reader_registered = False
        fileno: int | None = None

        try:
            fileno = self.stream.fileno()
        except (AttributeError, OSError, ValueError):
            fileno = None

        try:
            if fileno is None or not self.stream.isatty():
                # Non-interactive stdin (pipes, tests): expose dispatch() only.
                yield
                return

            def _on_readable() -> None:
                try:
                    line = self.stream.readline()
                except Exception:
                    line = ""
                if line:
                    queue.put_nowait(line)
                else:
                    with suppress(Exception):
                        loop.remove_reader(fileno)  # type: ignore[arg-type]
                    queue.put_nowait("")

            try:
                loop.add_reader(fileno, _on_readable)
                reader_registered = True
            except (NotImplementedError, AttributeError, ValueError):
                yield
                return

            async def _consume() -> None:
                while True:
                    line = await queue.get()
                    if not line:
                        self._input_closed = True
                        return
                    if not await self.dispatch(line):
                        return

            self._task = asyncio.create_task(_consume(), name="command-console")
            try:
                yield
            finally:
                self._task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self._task
                self._task = None
        finally:
            if reader_registered and fileno is not None:
                with suppress(Exception):
                    loop.remove_reader(fileno)
