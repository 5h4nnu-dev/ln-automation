"""CLI and runner-command tests that do not launch a browser."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from config import Settings
from database.models import ExecutionStatus, LessonStatus, LessonUpsert
from database.storage import Storage
from main import CourseRunner, build_parser, main, resolve_course_urls
from ui.dashboard import CourseOutcome


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "course_url": "https://www.linkedin.com/learning/demo-course",
        "headless": True,
        "browser_profile": tmp_path / "profile",
        "database_path": tmp_path / "progress.db",
        "log_dir": tmp_path / "logs",
    }
    base.update(overrides)
    return Settings(**base)


def test_parser_exposes_documented_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--course-url",
            "https://www.linkedin.com/learning/demo",
            "--courses-file",
            "courses.csv",
            "--speed",
            "2.5",
            "--headless",
            "--no-dashboard",
            "--progress",
        ]
    )
    assert args.course_url.endswith("/demo")
    assert args.courses_file == Path("courses.csv")
    assert args.speed == 2.5
    assert args.headless is True
    assert args.no_dashboard is True
    assert args.progress is True


async def test_runner_command_handlers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    console = Console(record=True, width=100, force_terminal=False)
    runner = CourseRunner(settings, console=console, live=False)
    try:
        assert "already running" in await runner._cmd_start()
        assert "Paused" in await runner._cmd_pause()
        assert runner.state.status is ExecutionStatus.PAUSED
        assert "resumed" in (await runner._cmd_resume()).lower()
        assert runner.state.status is ExecutionStatus.RUNNING

        assert "Usage" in await runner._cmd_speed(None)
        assert "not a number" in await runner._cmd_speed("fast")
        assert "between" in await runner._cmd_speed("17")
        message = await runner._cmd_speed("16")
        assert "16.00x" in message
        assert settings.playback_speed == 16.0
        assert settings.max_playback_speed == 16.0
        assert runner.state.playback_speed == 16.0

        message = await runner._cmd_speed("2.75")
        assert "2.75x" in message
        assert settings.playback_speed == 2.75
        assert runner.state.playback_speed == 2.75

        table = await runner._cmd_progress()
        assert isinstance(table, Table)

        assert "gracefully" in await runner._cmd_exit()
        assert runner._stop is True
        assert runner.state.status is ExecutionStatus.STOPPED
        assert "already in progress" in await runner._cmd_start()
    finally:
        runner.storage.close()


def _seed_progress(path: Path) -> None:
    with Storage(path) as storage:
        course = storage.upsert_course(
            "https://www.linkedin.com/learning/demo-course",
            title="Demo Course",
            total_lessons=2,
            playback_speed=2.0,
        )
        storage.sync_lessons(
            course.id,
            [
                LessonUpsert(lesson_key="/lesson-one", position=0, title="One"),
                LessonUpsert(lesson_key="/lesson-two", position=1, title="Two"),
            ],
        )
        storage.mark_lesson_status(
            course.id,
            "/lesson-one",
            LessonStatus.COMPLETED,
            watched_seconds=120,
        )


def _write_config(tmp_path: Path, database: Path) -> Path:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "course_url": "https://www.linkedin.com/learning/demo-course",
                "database_path": str(database),
                "browser_profile": str(tmp_path / "profile"),
                "log_dir": str(tmp_path / "logs"),
                "headless": True,
            }
        ),
        encoding="utf-8",
    )
    return config


def test_cli_progress_and_reset(tmp_path: Path, capsys) -> None:
    database = tmp_path / "progress.db"
    _seed_progress(database)
    config = _write_config(tmp_path, database)

    assert main(["--config", str(config), "--progress"]) == 0
    output = capsys.readouterr().out
    assert "Saved Course Progress" in output
    assert "Demo Course" in output
    assert "One" in output
    assert "120" not in output  # watch time is rendered as 2:00

    assert main(["--config", str(config), "--reset"]) == 0
    output = capsys.readouterr().out
    assert "PROGRESS RESET" in output

    with Storage(database) as storage:
        course = storage.list_courses()[0]
        stats = storage.stats(course.id)
        assert stats.completed == 0
        assert stats.total == 2
        assert stats.pending == 2


def test_cli_show_config_and_missing_course_url(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path, tmp_path / "progress.db")
    assert main(["--config", str(config), "--show-config"]) == 0
    output = capsys.readouterr().out
    assert "course_url" in output
    assert "demo-course" in output

    no_course = tmp_path / "no-course.json"
    no_course.write_text(
        json.dumps({"database_path": str(tmp_path / "empty.db")}),
        encoding="utf-8",
    )
    assert main(["--config", str(no_course)]) == 2
    assert "COURSE URL REQUIRED" in capsys.readouterr().out


def test_resolve_course_urls_prefers_file(tmp_path: Path) -> None:
    course_file = tmp_path / "courses.txt"
    course_file.write_text(
        "https://www.linkedin.com/learning/one\nhttps://www.linkedin.com/learning/two\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path, courses_file=course_file)

    assert resolve_course_urls(settings) == [
        "https://www.linkedin.com/learning/one",
        "https://www.linkedin.com/learning/two",
    ]

    settings.courses_file = None
    assert resolve_course_urls(settings) == [
        "https://www.linkedin.com/learning/demo-course"
    ]


async def test_batch_runner_runs_each_course_and_reports_summary(
    tmp_path: Path, monkeypatch
) -> None:
    urls = [
        "https://www.linkedin.com/learning/one",
        "https://www.linkedin.com/learning/two",
    ]
    console = Console(record=True, width=120, force_terminal=False)
    runner = CourseRunner(
        _settings(tmp_path), console=console, live=False, course_urls=urls
    )
    seen: list[str] = []

    async def fake_course(url: str) -> CourseOutcome:
        seen.append(url)
        runner.state.course_title = url.rsplit("/", 1)[-1]
        runner.state.completed = 1
        runner.state.total_lessons = 1
        status = (
            ExecutionStatus.COMPLETED
            if url.endswith("one")
            else ExecutionStatus.ERROR
        )
        runner.state.status = status
        return CourseOutcome(
            url=url,
            title=runner.state.course_title,
            status=status,
            completed=1,
            total=1,
            error="simulated failure" if status is ExecutionStatus.ERROR else None,
        )

    monkeypatch.setattr(runner, "_run_one_course", fake_course)
    try:
        await runner._run_session()

        assert seen == urls
        assert runner.state.course_index == 2
        assert runner.state.course_total == 2
        assert [item.status for item in runner.outcomes] == [
            ExecutionStatus.COMPLETED,
            ExecutionStatus.ERROR,
        ]
        assert runner.state.status is ExecutionStatus.ERROR
        output = console.export_text(clear=False)
        assert "BATCH SUMMARY" in output
        assert "simulated failure" in output
    finally:
        runner.storage.close()
