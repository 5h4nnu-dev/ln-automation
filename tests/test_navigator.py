"""Curriculum extraction and course navigation tests against a local DOM."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings
from course.curriculum import (
    Course,
    CourseNavigator,
    Lesson,
    Section,
    clean_title,
    make_lesson_key,
    normalize_url,
    parse_duration,
    select_next_lesson,
)
from utils.exceptions import CurriculumError
from utils.selectors import Selectors

COURSE_HTML = """
<!doctype html>
<html>
<head><title>Python Essentials | LinkedIn Learning</title></head>
<body>
  <header><h1>Python Essentials</h1></header>
  <aside class="classroom-toc">
    <div class="classroom-toc-section">
      <h2 class="classroom-toc-section__title">Getting Started</h2>
      <ul>
        <li class="classroom-toc-item completed" data-lesson="intro">
          <a class="classroom-toc-item__link" href="/learning/test-course/intro.html">Introduction</a>
          <span class="classroom-toc-item__duration">2m 15s</span>
          <span class="completed-icon" aria-hidden="true">&#10003;</span>
        </li>
        <li class="classroom-toc-item" data-lesson="setup">
          <a class="classroom-toc-item__link" href="/learning/test-course/setup.html">Setup</a>
          <span class="classroom-toc-item__duration">1:05</span>
        </li>
      </ul>
    </div>
    <div class="classroom-toc-section">
      <h2 class="classroom-toc-section__title">Core Concepts</h2>
      <ul>
        <li class="classroom-toc-item locked" data-lesson="loops" aria-disabled="true">
          <a class="classroom-toc-item__link" aria-disabled="true" href="/learning/test-course/loops.html">Loops</a>
          <span class="classroom-toc-item__duration">3m</span>
        </li>
        <li class="classroom-toc-item active" data-lesson="functions">
          <a class="classroom-toc-item__link" aria-current="page" href="/learning/test-course/functions.html">Functions</a>
          <span class="classroom-toc-item__duration">45s</span>
        </li>
      </ul>
    </div>
    <div class="related">
      <a href="/learning/other-course/intro.html">Other course</a>
      <a href="/login">Sign in</a>
    </div>
  </aside>
</body>
</html>
"""


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "headless": True,
        "poll_interval": 0.2,
        "completion_timeout": 5,
        "verify_via_course_page": False,
        "database_path": tmp_path / "progress.db",
        "log_dir": tmp_path / "logs",
        "browser_profile": tmp_path / "profile",
    }
    base.update(overrides)
    return Settings(**base)


def _navigator(page, settings: Settings) -> CourseNavigator:
    return CourseNavigator(lambda: page, Selectors(), settings)


async def _open_course(page, site) -> str:
    url = site.write("learning/test-course/index.html", COURSE_HTML)
    await page.goto(site.url("learning/test-course/"))
    return site.url("learning/test-course/")  # directory index loads COURSE_HTML


# --------------------------------------------------------------------------- #
# Pure parsing helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2m 15s", 135.0),
        ("1:05", 65.0),
        ("1:02:03", 3723.0),
        ("3m", 180.0),
        ("45s", 45.0),
        ("1h 2m", 3720.0),
        ("About 10 minutes", 600.0),
        ("Chapter overview", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_duration(text: str | None, expected: float | None) -> None:
    assert parse_duration(text) == expected


def test_clean_title_strips_duration() -> None:
    assert clean_title("Introduction 2m 15s") == "Introduction"
    assert clean_title("  Loops   1:05 ") == "Loops"
    assert clean_title(None) == ""


def test_normalize_url_and_key() -> None:
    assert normalize_url("https://www.linkedin.com/learning/c/intro/?trk=x#top") == (
        "https://www.linkedin.com/learning/c/intro"
    )
    assert make_lesson_key("https://x/learning/c/intro?u=1", "Intro", 0) == "/learning/c/intro"
    assert make_lesson_key("", "Hello World!", 2) == "/hello-world-2"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
async def test_discovers_sections_lessons_and_markers(page, site, tmp_path: Path) -> None:
    await page.goto(site.url("learning/test-course/"))
    site.write("learning/test-course/index.html", COURSE_HTML)
    await page.goto(site.url("learning/test-course/"))

    course = await _navigator(page, _settings(tmp_path)).discover()

    assert course.title == "Python Essentials"
    assert course.total_lessons == 4
    assert [section.title for section in course.sections] == ["Getting Started", "Core Concepts"]
    assert [lesson.title for lesson in course.lessons] == [
        "Introduction",
        "Setup",
        "Loops",
        "Functions",
    ]
    assert [lesson.duration_seconds for lesson in course.lessons] == [135.0, 65.0, 180.0, 45.0]
    assert course.lessons[0].completed is True
    assert course.lessons[1].completed is False
    assert course.lessons[2].locked is True
    assert course.lessons[3].active is True
    assert [lesson.section for lesson in course.lessons] == [
        "Getting Started",
        "Getting Started",
        "Core Concepts",
        "Core Concepts",
    ]
    assert len({lesson.key for lesson in course.lessons}) == 4
    assert course.completed_keys() == ["/learning/test-course/intro.html"]


async def test_active_lesson_from_dom(page, site, tmp_path: Path) -> None:
    site.write("learning/test-course/index.html", COURSE_HTML)
    await page.goto(site.url("learning/test-course/"))
    navigator = _navigator(page, _settings(tmp_path))
    course = await navigator.discover()

    active = await navigator.active_lesson(course)
    assert active is not None
    assert active.title == "Functions"


async def test_lesson_status_reads_completion(page, site, tmp_path: Path) -> None:
    site.write("learning/test-course/index.html", COURSE_HTML)
    await page.goto(site.url("learning/test-course/"))
    navigator = _navigator(page, _settings(tmp_path))
    course = await navigator.discover()

    intro = course.lessons[0]
    setup = course.lessons[1]
    loops = course.lessons[2]

    assert (await navigator.lesson_status(intro))["completed"] is True
    assert (await navigator.lesson_status(setup))["completed"] is False
    assert (await navigator.lesson_status(loops))["locked"] is True


async def test_fallback_without_toc_root(page, site, tmp_path: Path) -> None:
    site.write(
        "learning/fallback/index.html",
        """
        <!doctype html><html><head><title>Fallback Course</title></head><body>
          <div class="content">
            <a href="/learning/fallback/one.html">One</a>
            <a href="/learning/fallback/two.html">Two</a>
          </div>
        </body></html>
        """,
    )
    await page.goto(site.url("learning/fallback/"))
    course = await _navigator(page, _settings(tmp_path)).discover()
    assert course.total_lessons == 2
    assert course.title == "Fallback Course"
    assert [lesson.title for lesson in course.lessons] == ["One", "Two"]


async def test_no_curriculum_raises(page, site, tmp_path: Path) -> None:
    site.write("learning/empty/index.html", "<html><body><p>Nothing here</p></body></html>")
    await page.goto(site.url("learning/empty/"))
    with pytest.raises(CurriculumError):
        await _navigator(page, _settings(tmp_path)).discover()


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
async def test_open_lesson_by_url(page, site, tmp_path: Path) -> None:
    site.write("learning/test-course/index.html", COURSE_HTML)
    site.write(
        "learning/test-course/setup.html",
        "<html><head><title>Setup</title></head><body><h1>Setup lesson</h1></body></html>",
    )
    await page.goto(site.url("learning/test-course/"))
    navigator = _navigator(page, _settings(tmp_path))
    course = await navigator.discover()

    await navigator.open_lesson(course.lessons[1])
    assert page.url.endswith("/setup.html")
    assert "Setup lesson" in await page.content()


async def test_wait_for_platform_completion_verified(page, site, tmp_path: Path) -> None:
    html = COURSE_HTML.replace(
        "</aside>",
        """
        <script>
          setTimeout(() => {
            const item = document.querySelector('[data-lesson="setup"]');
            item.classList.add('completed');
            const icon = document.createElement('span');
            icon.className = 'completed-icon';
            item.appendChild(icon);
          }, 400);
        </script>
        </aside>
        """,
    )
    site.write("learning/test-course/index.html", html)
    await page.goto(site.url("learning/test-course/"))
    navigator = _navigator(page, _settings(tmp_path))
    course = await navigator.discover()

    result = await navigator.wait_for_platform_completion(course.lessons[1], timeout=5)
    assert result["verified"] is True
    assert result["completed"] is True


async def test_wait_for_platform_completion_never_fabricates(page, site, tmp_path: Path) -> None:
    site.write("learning/test-course/index.html", COURSE_HTML)
    await page.goto(site.url("learning/test-course/"))
    navigator = _navigator(page, _settings(tmp_path))
    course = await navigator.discover()

    result = await navigator.wait_for_platform_completion(course.lessons[1], timeout=0.5)
    assert result["verified"] is False
    assert result["completed"] is False
    assert "left as pending" in (result["reason"] or "")


# --------------------------------------------------------------------------- #
# Selection logic
# --------------------------------------------------------------------------- #
def _course() -> Course:
    lessons = [
        Lesson(position=0, title="Intro", url="/learning/c/intro"),
        Lesson(position=1, title="Setup", url="/learning/c/setup"),
        Lesson(position=2, title="Loops", url="/learning/c/loops", locked=True),
        Lesson(position=3, title="Functions", url="/learning/c/functions"),
    ]
    return Course(url="/learning/c", title="C", sections=[Section(title=None, lessons=lessons)])


def test_select_next_lesson_skips_completed_unavailable_and_locked() -> None:
    course = _course()
    assert select_next_lesson(course).title == "Intro"
    assert select_next_lesson(course, completed={"/learning/c/intro"}).title == "Setup"
    assert (
        select_next_lesson(
            course,
            completed={"/learning/c/intro"},
            unavailable={"/learning/c/setup"},
        ).title
        == "Functions"
    )
    assert select_next_lesson(course, completed={lesson.key for lesson in course.lessons}) is None


def test_select_next_lesson_prefers_resume_key() -> None:
    course = _course()
    preferred = course.lessons[3]
    chosen = select_next_lesson(
        course,
        completed={"/learning/c/intro"},
        preferred_key=preferred.key,
    )
    assert chosen is not None and chosen.key == preferred.key

    # A completed preferred lesson is ignored.
    chosen = select_next_lesson(
        course,
        completed={"/learning/c/intro"},
        preferred_key="/learning/c/intro",
    )
    assert chosen is not None and chosen.key == "/learning/c/setup"
