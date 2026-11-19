"""Tests for terminal command parsing, panels and the dashboard view."""

from __future__ import annotations

import inspect
from datetime import datetime

from rich.console import Console

from assessment.assistant import AssessmentAssistant, Confidence, Suggestion
from assessment.extractor import AnswerOption, Question
from database.models import ExecutionStatus
from ui import terminal as terminal_module
from ui.dashboard import (
    AppState,
    CourseOutcome,
    Dashboard,
    batch_summary_table,
    progress_table,
    summary_panel,
)
from ui.terminal import (
    Command,
    CommandConsole,
    TerminalUI,
    assessment_panel,
    parse_command,
    waiting_panel,
)


def _question() -> Question:
    return Question(
        prompt="Which protocol is commonly used for secure web communication?",
        options=[
            AnswerOption(key="A", text="HTTP"),
            AnswerOption(key="B", text="HTTPS"),
            AnswerOption(key="C", text="FTP"),
            AnswerOption(key="D", text="SMTP"),
        ],
    )


def _console() -> Console:
    return Console(record=True, width=100, force_terminal=False)


# --------------------------------------------------------------------------- #
# Command parsing
# --------------------------------------------------------------------------- #
def test_parse_known_commands_and_aliases() -> None:
    assert parse_command("pause") == terminal_module.ParsedCommand(Command.PAUSE)
    assert parse_command(" continue ") == terminal_module.ParsedCommand(Command.RESUME)
    assert parse_command("status") == terminal_module.ParsedCommand(Command.PROGRESS)
    assert parse_command("quit") == terminal_module.ParsedCommand(Command.EXIT)
    assert parse_command("help") == terminal_module.ParsedCommand(Command.HELP)


def test_parse_speed_variants() -> None:
    assert parse_command("speed 2.5").argument == "2.5"
    assert parse_command("speed=1.5").argument == "1.5"
    assert parse_command("set speed 1.75").argument == "1.75"
    assert parse_command("speed").name is Command.SPEED
    assert parse_command("speed").argument is None


def test_parse_unknown_and_blank() -> None:
    assert parse_command("") is None
    assert parse_command("   ") is None
    assert parse_command("teleport") is None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_assessment_panel_contains_question_options_and_manual_instruction() -> None:
    console = _console()
    question = _question()
    suggestion = AssessmentAssistant().suggest(question)
    console.print(assessment_panel(question, suggestion, index=1, total=2))
    output = console.export_text()

    assert "ASSESSMENT DETECTED — 1/2" in output
    assert "secure web communication" in output
    assert "A. HTTP" in output
    assert "B. HTTPS" in output
    assert "Suggested answer" in output
    assert "Explanation" in output
    assert "Confidence" in output
    assert "manually in the browser" in output
    assert "never selects or submits" in output


def test_assessment_panel_handles_no_suggestion() -> None:
    console = _console()
    console.print(assessment_panel(_question(), None))
    output = console.export_text()
    assert "none" not in output.lower() or "insufficient signal" in output
    assert "HTTP" in output


def test_waiting_panel_and_command_help_render() -> None:
    console = _console()
    console.print(waiting_panel("Submit in the browser"))
    console.print(terminal_module.command_help())
    output = console.export_text()
    assert "WAITING FOR YOU" in output
    assert "Submit in the browser" in output
    assert "speed <x>" in output
    assert "exit" in output


def test_terminal_assessment_code_cannot_click_or_submit() -> None:
    source = inspect.getsource(terminal_module)
    for forbidden in (".click(", ".fill(", ".press(", ".check(", ".submit(", ".select_option("):
        assert forbidden not in source


# --------------------------------------------------------------------------- #
# Command console
# --------------------------------------------------------------------------- #
async def test_command_console_dispatches_handlers() -> None:
    console = _console()
    ui = TerminalUI(console)
    calls: list[object] = []

    async def start(argument: str | None) -> str:
        calls.append(("start", argument))
        return "started"

    async def speed(argument: str | None) -> str:
        calls.append(("speed", argument))
        return f"speed={argument}"

    async def exit_handler(argument: str | None) -> str:
        calls.append(("exit", argument))
        return "bye"

    commands = CommandConsole(
        ui,
        {
            Command.START: start,
            Command.SPEED: speed,
            Command.EXIT: exit_handler,
        },
    )

    assert await commands.dispatch("start") is True
    assert await commands.dispatch("speed 3.0") is True
    output = console.export_text()
    assert "started" in output
    assert "speed=3.0" in output

    assert await commands.dispatch("exit") is False
    assert calls == [("start", None), ("speed", "3.0"), ("exit", None)]


async def test_command_console_unknown_command_shows_help() -> None:
    console = _console()
    commands = CommandConsole(TerminalUI(console), {})
    assert await commands.dispatch("warp 9") is True
    assert await commands.dispatch("") is True
    output = console.export_text()
    assert "Unknown command: warp 9" in output
    assert "Commands" in output


async def test_command_console_handler_error_is_contained() -> None:
    console = _console()

    async def broken(argument: str | None) -> str:
        raise RuntimeError("boom")

    commands = CommandConsole(TerminalUI(console), {Command.START: broken})
    assert await commands.dispatch("start") is True
    assert "Command failed: boom" in console.export_text()


# --------------------------------------------------------------------------- #
# Manual submission wait (read-only polling)
# --------------------------------------------------------------------------- #
class _FakeDetector:
    def __init__(self, responses: list[list[Question]]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def detect(self) -> list[Question]:
        self.calls += 1
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0] if self.responses else []


async def test_wait_for_manual_submission_returns_when_question_closes() -> None:
    console = _console()
    ui = TerminalUI(console)
    question = _question()
    detector = _FakeDetector([[question], [question], []])

    submitted = await ui.wait_for_manual_submission(
        detector,
        timeout=3,
        poll_interval=0.01,
        reminder_after=60,
        panels=[assessment_panel(question, None)],
    )

    assert submitted is True
    assert detector.calls >= 2
    output = console.export_text()
    assert "secure web communication" in output
    assert "still open in the browser" in output


async def test_wait_for_manual_submission_times_out_without_submitting() -> None:
    console = _console()
    ui = TerminalUI(console)
    detector = _FakeDetector([[_question()]])

    submitted = await ui.wait_for_manual_submission(
        detector,
        timeout=0.05,
        poll_interval=0.01,
        reminder_after=60,
    )

    assert submitted is False
    assert detector.calls >= 1


async def test_dashboard_interactive_blocks_can_nest() -> None:
    """A command printed during an assessment must not deadlock on the lock."""

    import asyncio

    console = _console()
    state = AppState()
    dashboard = Dashboard(console, lambda: state, refresh_per_second=20)
    await dashboard.start()
    try:
        async def nested() -> None:
            async with dashboard.interactive():
                assert dashboard._started is False
                async with dashboard.interactive():
                    # Nested blocks reuse the already-suspended live region.
                    assert dashboard._started is False
                assert dashboard._started is False
            assert dashboard._started is True

        await asyncio.wait_for(nested(), timeout=5)
    finally:
        await dashboard.stop()


# --------------------------------------------------------------------------- #
# Dashboard view model
# --------------------------------------------------------------------------- #
def test_app_state_counts_and_progress() -> None:
    state = AppState(total_lessons=10, completed=4, skipped=1, locked=2)
    assert state.remaining == 6
    assert state.progress_fraction == 0.4

    empty = AppState()
    assert empty.remaining == 0
    assert empty.progress_fraction == 0.0


def test_dashboard_renders_course_state() -> None:
    console = _console()
    state = AppState(
        status=ExecutionStatus.RUNNING,
        course_title="Python Essentials",
        course_url="https://www.linkedin.com/learning/python-essentials",
        total_lessons=8,
        completed=3,
        current_lesson="Functions",
        current_section="Core Concepts",
        playback_speed=2.0,
        playback_position=45.0,
        playback_duration=180.0,
        detail="Playing at 2.00x",
    )
    state.add_activity("Lesson verified complete: Variables")
    dashboard = Dashboard(console, lambda: state)
    console.print(dashboard.render())
    output = console.export_text()

    assert "LINKEDIN LEARNING AUTOMATION" in output
    assert "Python Essentials" in output
    assert "Functions" in output
    assert "Core Concepts" in output
    assert "Playback 2.00x" in output
    assert "Lesson verified complete" in output


def test_batch_summary_table_lists_every_course() -> None:
    outcomes = [
        CourseOutcome(
            url="https://www.linkedin.com/learning/one",
            title="Course One",
            status=ExecutionStatus.COMPLETED,
            completed=8,
            total=8,
        ),
        CourseOutcome(
            url="https://www.linkedin.com/learning/two",
            title="Course Two",
            status=ExecutionStatus.ERROR,
            completed=3,
            total=9,
            error="curriculum not found",
        ),
    ]
    console = _console()
    console.print(batch_summary_table(outcomes))
    output = console.export_text()

    assert "BATCH SUMMARY" in output
    assert "Course One" in output
    assert "Course Two" in output
    assert "8/8" in output
    assert "3/9" in output
    assert "curriculum not found" in output


def test_summary_panel_reports_incomplete_and_complete_states() -> None:
    console = _console()
    state = AppState(
        status=ExecutionStatus.COMPLETED,
        course_title="Python Essentials",
        total_lessons=4,
        completed=4,
        total_watched_seconds=600,
    )
    console.print(summary_panel(state))
    output = console.export_text()
    assert "SESSION COMPLETE" in output
    assert "Python Essentials" in output
    assert "Completed" in output

    partial = AppState(
        status=ExecutionStatus.STOPPED,
        course_title="Python Essentials",
        total_lessons=4,
        completed=2,
        skipped=1,
    )
    console.print(summary_panel(partial))
    output = console.export_text()
    assert "SESSION SUMMARY" in output
    assert "Run the tool again to resume" in output


def test_progress_table_reflects_state() -> None:
    console = _console()
    state = AppState(
        status=ExecutionStatus.PAUSED,
        course_title="Course",
        total_lessons=5,
        completed=2,
        playback_speed=1.5,
        started_at=datetime.now(),
    )
    console.print(progress_table(state))
    output = console.export_text()
    assert "Course Progress" in output
    assert "paused" in output.lower()
    assert "1.50x" in output
