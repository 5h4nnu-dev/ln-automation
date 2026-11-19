"""End-to-end integration tests against a local course fixture.

These tests run a real Chromium with a real WebM clip. The fixture site mimics
the relevant parts of LinkedIn Learning: a table of contents, lesson pages with
an HTML5 video, and completion markers that only appear *after* the media ends
(never created by the automation itself).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from rich.console import Console

from config import Settings
from database.models import ExecutionStatus, LessonStatus
from database.storage import Storage
from main import CourseRunner
from utils.course_list import read_course_urls

COURSE_TITLE = "Demo Python Course"


def _settings(tmp_path: Path, course_url: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "course_url": course_url,
        "headless": True,
        "browser_profile": tmp_path / "profile",
        "database_path": tmp_path / "progress.db",
        "log_dir": tmp_path / "logs",
        "playback_speed": 4.0,
        "max_playback_speed": 4.0,
        "poll_interval": 0.1,
        "stall_timeout": 20.0,
        "playback_timeout": 60.0,
        "completion_timeout": 8.0,
        "assessment_wait_timeout": 10.0,
        "max_retries": 2,
        "max_consecutive_failures": 3,
        "assessment_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


def _toc(lessons: list[tuple[str, str]], base: str = "demo") -> str:
    items = "\n".join(
        f"""
        <li class="classroom-toc-item" data-lesson="{slug}" data-course="{base}">
          <a class="classroom-toc-item__link" href="/learning/{base}/{slug}.html">{title}</a>
          <span class="classroom-toc-item__duration">3s</span>
        </li>"""
        for slug, title in lessons
    )
    return f"""
    <aside class="classroom-toc">
      <div class="classroom-toc-section">
        <h2 class="classroom-toc-section__title">Lessons</h2>
        <ul>{items}</ul>
      </div>
    </aside>"""


COMPLETION_JS = """
const applyCompletion = () => {
  document.querySelectorAll(".classroom-toc-item").forEach((item) => {
    const slug = item.getAttribute("data-lesson");
    const course = item.getAttribute("data-course") || "demo";
    if (!slug) return;
    if (localStorage.getItem("ll-done:" + course + ":" + slug) === "1") {
      item.classList.add("completed");
      if (!item.querySelector(".completed-icon")) {
        const marker = document.createElement("span");
        marker.className = "completed-icon";
        marker.setAttribute("aria-hidden", "true");
        marker.textContent = "✓";
        item.appendChild(marker);
      }
    }
  });
};
applyCompletion();
"""


def _lesson_page(
    slug: str,
    title: str,
    lessons: list[tuple[str, str]],
    base: str = "demo",
    course_title: str = COURSE_TITLE,
) -> str:
    return f"""<!doctype html>
<html>
<head><title>{title} | {course_title}</title></head>
<body>
  <header><h1>{course_title}</h1></header>
  {_toc(lessons, base)}
  <main>
    <h2>{title}</h2>
    <video id="lesson-video" src="/media/sample.webm" muted playsinline preload="auto"></video>
  </main>
  <script>
    {COMPLETION_JS}
    const slug = "{slug}";
    const course = "{base}";
    const video = document.querySelector("#lesson-video");
    video.addEventListener("ended", () => {{
      localStorage.setItem("ll-done:" + course + ":" + slug, "1");
      applyCompletion();
    }});
  </script>
</body>
</html>"""


def _course_index(
    lessons: list[tuple[str, str]],
    base: str = "demo",
    course_title: str = COURSE_TITLE,
) -> str:
    return f"""<!doctype html>
<html>
<head><title>{course_title} | LinkedIn Learning</title></head>
<body>
  <header><h1>{course_title}</h1></header>
  {_toc(lessons, base)}
  <main><p>Select a lesson to begin.</p></main>
  <script>{COMPLETION_JS}</script>
</body>
</html>"""


def _build_course(
    site,
    video_file: Path,
    *,
    base: str = "demo",
    course_title: str = COURSE_TITLE,
) -> str:
    lessons = [
        ("lesson-one", "Lesson One"),
        ("lesson-two", "Lesson Two"),
    ]
    site.write_bytes("media/sample.webm", video_file.read_bytes())
    site.write(
        f"learning/{base}/index.html",
        _course_index(lessons, base, course_title),
    )
    for slug, title in lessons:
        site.write(
            f"learning/{base}/{slug}.html",
            _lesson_page(slug, title, lessons, base, course_title),
        )
    return site.url(f"learning/{base}/")


async def test_full_course_run_and_resume_without_reprocessing(
    site, video_file: Path, tmp_path: Path
) -> None:
    course_url = _build_course(site, video_file)
    settings = _settings(tmp_path, course_url)
    console = Console(record=True, width=120, force_terminal=False)

    runner = CourseRunner(settings, console=console, live=False)
    status = await asyncio.wait_for(runner.run(), timeout=120)
    assert status is ExecutionStatus.COMPLETED
    assert runner.state.completed == 2
    assert runner.state.remaining == 0
    assert runner.state.total_watched_seconds > 0

    output = console.export_text()
    assert "SESSION COMPLETE" in output
    assert "Demo Python Course" in output

    with Storage(settings.database_path) as storage:
        courses = storage.list_courses()
        assert len(courses) == 1
        record = courses[0]
        stats = storage.stats(record.id)
        assert stats.total == 2
        assert stats.completed == 2
        assert stats.remaining == 0
        lessons = storage.get_lessons(record.id)
        assert all(lesson.status is LessonStatus.COMPLETED for lesson in lessons)
        assert all(lesson.watched_seconds > 0 for lesson in lessons)
        sessions = storage.get_sessions(record.id)
        assert sessions and sessions[0].ended_at is not None
        assert sessions[0].status == ExecutionStatus.COMPLETED.value

    # Second run with the same profile/database: the platform's own TOC now
    # advertises both lessons as completed, so nothing is replayed.
    console2 = Console(record=True, width=120, force_terminal=False)
    runner2 = CourseRunner(settings, console=console2, live=False)
    status2 = await asyncio.wait_for(runner2.run(), timeout=60)
    assert status2 is ExecutionStatus.COMPLETED
    assert runner2.state.completed == 2
    assert runner2.state.playback_position == 0
    assert runner2.state.playback_duration == 0
    assert "SESSION COMPLETE" in console2.export_text()


QUIZ_HTML = """
<!doctype html>
<html>
<head><title>Course Quiz</title></head>
<body>
  <section>
    <h2>Secure Web</h2>
    <form id="quiz">
      <fieldset>
        <legend>Which protocol is commonly used for secure web communication?</legend>
        <label><input type="radio" name="q1" value="http"> HTTP</label>
        <label><input type="radio" name="q1" value="https"> HTTPS</label>
        <label><input type="radio" name="q1" value="ftp"> FTP</label>
      </fieldset>
    </form>
    <button id="submit-quiz">Submit</button>
  </section>
</body>
</html>
"""


async def test_assessment_waits_for_manual_submission_only(
    site, tmp_path: Path
) -> None:
    settings = _settings(
        tmp_path,
        site.url("quiz.html"),
        assessment_wait_timeout=10.0,
        poll_interval=0.1,
    )
    url = site.write("quiz.html", QUIZ_HTML)
    console = Console(record=True, width=120, force_terminal=False)
    runner = CourseRunner(settings, console=console, live=False)

    try:
        page = await runner.browser.start()
        await page.goto(url)
        questions = await runner.detector.detect()
        assert len(questions) == 1
        assert "secure web communication" in questions[0].prompt
        assert [option.text for option in questions[0].options] == ["HTTP", "HTTPS", "FTP"]

        lesson = type(
            "LessonStub",
            (),
            {"title": "Secure Web", "section": "Security", "key": "/quiz", "display_title": "Secure Web"},
        )()
        task = asyncio.create_task(runner._handle_assessment(lesson, questions))

        # The tool must never select an answer while it waits.
        await asyncio.sleep(0.5)
        assert not task.done()
        checked = await page.evaluate("() => document.querySelectorAll('input:checked').length")
        assert checked == 0

        output = console.export_text(clear=False)
        assert "ASSESSMENT DETECTED" in output
        assert "Suggested answer" in output
        assert "manually in the browser" in output

        # Simulate the user selecting and submitting in the Chromium window.
        await page.evaluate(
            """() => {
                const radio = document.querySelector("input[value='https']");
                radio.checked = true;
                radio.dispatchEvent(new Event('change', {bubbles: true}));
                document.querySelector('#quiz').remove();
            }"""
        )

        submitted = await asyncio.wait_for(task, timeout=15)
        assert submitted is True
        assert runner.state.assessments == 1
        assert any(
            "Assessment submitted manually" in activity.message
            for activity in runner.state.activities
        )
    finally:
        await runner.browser.close()
        runner.storage.close()


async def test_locked_lessons_are_not_processed(
    site, video_file: Path, tmp_path: Path
) -> None:
    course_url = _build_course(site, video_file)
    # Mark the second lesson as locked in the platform TOC.
    index = (site.root / "learning/demo/index.html").read_text(encoding="utf-8")
    index = index.replace(
        '<li class="classroom-toc-item" data-lesson="lesson-two" data-course="demo">',
        '<li class="classroom-toc-item locked" data-lesson="lesson-two" '
        'data-course="demo" aria-disabled="true">',
    )
    site.write("learning/demo/index.html", index)

    settings = _settings(tmp_path, course_url)
    console = Console(record=True, width=120, force_terminal=False)
    runner = CourseRunner(settings, console=console, live=False)
    status = await asyncio.wait_for(runner.run(), timeout=120)

    assert status is ExecutionStatus.STOPPED
    with Storage(settings.database_path) as storage:
        record = storage.list_courses()[0]
        lessons = storage.get_lessons(record.id)
        assert lessons[0].status is LessonStatus.COMPLETED
        assert lessons[1].status is LessonStatus.LOCKED
        stats = storage.stats(record.id)
        assert stats.completed == 1
        assert stats.locked == 1
    assert "SESSION SUMMARY" in console.export_text()


async def test_auto_start_and_auto_navigate_require_resume(
    site, video_file: Path, tmp_path: Path
) -> None:
    course_url = _build_course(site, video_file)
    settings = _settings(
        tmp_path,
        course_url,
        auto_start=False,
        auto_navigate=False,
    )
    console = Console(record=True, width=120, force_terminal=False)
    runner = CourseRunner(settings, console=console, live=False)
    task = asyncio.create_task(runner.run())

    async def wait_for(predicate, timeout: float = 60.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.1)
        raise AssertionError("condition was not reached in time")

    try:
        await wait_for(
            lambda: runner.state.status is ExecutionStatus.PAUSED
            and runner.state.completed == 0
            and bool(runner.state.course_title)
        )
        assert "AUTO_START is disabled" in " ".join(
            activity.message for activity in runner.state.activities
        )

        await runner._cmd_start(None)
        await wait_for(
            lambda: runner.state.status is ExecutionStatus.PAUSED
            and runner.state.completed == 1
        )
        assert "AUTO_NAVIGATE is disabled" in runner.state.detail

        await runner._cmd_resume(None)
        await wait_for(
            lambda: runner.state.status is ExecutionStatus.PAUSED
            and runner.state.completed == 2
        )
        await runner._cmd_resume(None)
        status = await asyncio.wait_for(task, timeout=120)
        assert status is ExecutionStatus.COMPLETED
        assert runner.state.completed == 2
    finally:
        if not task.done():
            runner.request_stop()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=30)


async def test_batch_runs_multiple_courses_from_text_file(
    site, video_file: Path, tmp_path: Path
) -> None:
    url_one = _build_course(
        site, video_file, base="batch-one", course_title="Batch Course One"
    )
    url_two = _build_course(
        site, video_file, base="batch-two", course_title="Batch Course Two"
    )
    course_file = tmp_path / "courses.txt"
    course_file.write_text(f"{url_one}\n{url_two}\n", encoding="utf-8")
    urls = read_course_urls(course_file)

    settings = _settings(tmp_path, urls[0])
    console = Console(record=True, width=120, force_terminal=False)
    runner = CourseRunner(
        settings,
        console=console,
        live=False,
        course_urls=urls,
    )

    status = await asyncio.wait_for(runner.run(), timeout=180)
    assert status is ExecutionStatus.COMPLETED
    assert [item.status for item in runner.outcomes] == [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED,
    ]
    assert runner.state.course_index == 2
    assert runner.state.course_total == 2

    with Storage(settings.database_path) as storage:
        records = {record.title: record for record in storage.list_courses()}
        assert set(records) == {"Batch Course One", "Batch Course Two"}
        for record in records.values():
            assert storage.stats(record.id).completed == 2

    output = console.export_text(clear=False)
    assert "BATCH SUMMARY" in output
    assert "Batch Course One" in output
    assert "Batch Course Two" in output
