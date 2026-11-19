"""Persistence tests: courses, lessons, sessions, events and resume state."""

from __future__ import annotations

from pathlib import Path

from database.models import ExecutionStatus, LessonStatus, LessonUpsert
from database.storage import Storage


def _lessons() -> list[LessonUpsert]:
    return [
        LessonUpsert(lesson_key="intro", position=0, title="Introduction", url="http://x/intro"),
        LessonUpsert(lesson_key="setup", position=1, title="Setup", url="http://x/setup"),
        LessonUpsert(lesson_key="loops", position=2, title="Loops", url="http://x/loops", locked=True),
    ]


def test_course_upsert_and_sync(tmp_path: Path) -> None:
    with Storage(tmp_path / "progress.db") as storage:
        course = storage.upsert_course(
            "https://example.test/course",
            title="Python",
            total_lessons=3,
            playback_speed=2.0,
        )
        assert storage.sync_lessons(course.id, _lessons()) == 3
        assert storage.sync_lessons(course.id, _lessons()) == 3

        lessons = storage.get_lessons(course.id)
        assert [lesson.lesson_key for lesson in lessons] == ["intro", "setup", "loops"]
        assert lessons[2].status is LessonStatus.LOCKED
        stats = storage.stats(course.id)
        assert stats.total == 3
        assert stats.pending == 2
        assert stats.locked == 1
        assert stats.remaining == 3


def test_completion_is_persisted_and_not_reset_on_resync(tmp_path: Path) -> None:
    with Storage(tmp_path / "progress.db") as storage:
        course = storage.upsert_course("https://example.test/course", title="Python")
        storage.sync_lessons(course.id, _lessons())

        storage.mark_lesson_status(course.id, "intro", LessonStatus.COMPLETED, watched_seconds=42.5)
        storage.increment_attempt(course.id, "intro")

        assert storage.completed_keys(course.id) == ["intro"]
        stats = storage.stats(course.id)
        assert stats.completed == 1
        assert stats.remaining == 2

        # A later discovery must not resurrect a completed lesson.
        storage.sync_lessons(course.id, _lessons())
        lesson = storage.get_lesson(course.id, "intro")
        assert lesson is not None
        assert lesson.status is LessonStatus.COMPLETED
        assert lesson.watched_seconds == 42.5
        assert lesson.attempts == 1

        # Unlocking a lesson transitions locked -> pending.
        unlocked = _lessons()
        unlocked[2] = unlocked[2].model_copy(update={"locked": False})
        storage.sync_lessons(course.id, unlocked)
        assert storage.get_lesson(course.id, "loops").status is LessonStatus.PENDING


def test_platform_completion_reconciliation(tmp_path: Path) -> None:
    with Storage(tmp_path / "progress.db") as storage:
        course = storage.upsert_course("https://example.test/course")
        storage.sync_lessons(course.id, _lessons())

        updated = storage.mark_completed_from_platform(course.id, ["intro", "setup"])
        assert updated == 2
        assert set(storage.completed_keys(course.id)) == {"intro", "setup"}
        assert storage.mark_completed_from_platform(course.id, ["intro"]) == 0


def test_status_transitions_and_errors(tmp_path: Path) -> None:
    with Storage(tmp_path / "progress.db") as storage:
        course = storage.upsert_course("https://example.test/course")
        storage.sync_lessons(course.id, _lessons())

        storage.set_course_status(course.id, ExecutionStatus.RUNNING, start_session=True)
        storage.set_current_lesson(course.id, "setup")
        storage.mark_lesson_status(
            course.id, "setup", LessonStatus.FAILED, error="video stalled", watched_seconds=12.0
        )

        lesson = storage.get_lesson(course.id, "setup")
        assert lesson is not None
        assert lesson.status is LessonStatus.FAILED
        assert lesson.last_error == "video stalled"

        storage.mark_lesson_status(course.id, "setup", LessonStatus.COMPLETED, watched_seconds=30)
        lesson = storage.get_lesson(course.id, "setup")
        assert lesson.status is LessonStatus.COMPLETED
        assert lesson.last_error is None

        record = storage.get_course_by_id(course.id)
        assert record is not None
        assert record.current_lesson_key == "setup"
        assert record.status is ExecutionStatus.RUNNING
        assert record.session_started_at is not None


def test_sessions_and_events(tmp_path: Path) -> None:
    with Storage(tmp_path / "progress.db") as storage:
        course = storage.upsert_course("https://example.test/course")
        session_id = storage.start_session(course.id)
        storage.finish_session(session_id, lessons_completed=2, notes="done")
        sessions = storage.get_sessions(course.id)
        assert len(sessions) == 1
        assert sessions[0].lessons_completed == 2
        assert sessions[0].ended_at is not None

        storage.log_event(course.id, "info", "lesson_completed", "Intro done", {"key": "intro"})
        events = storage.get_recent_events(course.id)
        assert events[-1].event == "lesson_completed"
        assert events[-1].payload == {"key": "intro"}


def test_reset_course_progress(tmp_path: Path) -> None:
    with Storage(tmp_path / "progress.db") as storage:
        course = storage.upsert_course("https://example.test/course")
        storage.sync_lessons(course.id, _lessons())
        storage.mark_lesson_status(course.id, "intro", LessonStatus.COMPLETED)
        storage.set_current_lesson(course.id, "intro")

        storage.reset_course_progress(course.id)

        assert storage.completed_keys(course.id) == []
        record = storage.get_course_by_id(course.id)
        assert record is not None
        assert record.current_lesson_key is None
        assert storage.stats(course.id).pending == 2


def test_progress_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "progress.db"
    with Storage(path) as storage:
        course = storage.upsert_course("https://example.test/course", title="Python")
        storage.sync_lessons(course.id, _lessons())
        storage.mark_lesson_status(course.id, "intro", LessonStatus.COMPLETED, watched_seconds=60)

    with Storage(path) as storage:
        record = storage.get_course("https://example.test/course")
        assert record is not None
        assert record.title == "Python"
        assert storage.completed_keys(record.id) == ["intro"]
        lesson = storage.get_lesson(record.id, "intro")
        assert lesson is not None
        assert lesson.watched_seconds == 60


# --------------------------------------------------------------------------- #
# ProgressTracker: synchronization, resume plans and lesson lifecycle
# --------------------------------------------------------------------------- #
from config import Settings
from course.curriculum import Course, Lesson, Section
from course.progress import ProgressTracker


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_path": tmp_path / "progress.db",
        "log_dir": tmp_path / "logs",
        "browser_profile": tmp_path / "profile",
        "headless": True,
        "max_retries": 3,
    }
    base.update(overrides)
    return Settings(**base)


def _course(active_index: int | None = None) -> Course:
    titles = ["Intro", "Setup", "Loops", "Functions"]
    lessons = [
        Lesson(
            position=index,
            title=title,
            url=f"/learning/test/{title.lower()}",
            completed=index == 0,
            locked=index == 2,
            active=active_index == index,
        )
        for index, title in enumerate(titles)
    ]
    return Course(
        url="https://example.test/learning/test",
        title="Test Course",
        sections=[Section(title="All", lessons=lessons)],
    )


def test_tracker_sync_and_resume(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "progress.db")
    tracker = ProgressTracker(storage, _settings(tmp_path))
    course = _course()

    record, stats = tracker.sync_course(course)
    assert record.title == "Test Course"
    assert record.total_lessons == 4
    assert stats.completed == 1
    assert stats.pending == 2
    assert stats.locked == 1

    plan = tracker.plan_resume(course, record)
    assert plan.completed == {"/learning/test/intro"}
    assert plan.next_lesson is not None
    assert plan.next_lesson.title == "Setup"

    setup = plan.next_lesson
    tracker.mark_started(record, setup)
    record = storage.get_course_by_id(record.id)
    assert record is not None
    assert record.current_lesson_key == setup.key
    assert storage.get_lesson(record.id, setup.key).attempts == 1

    tracker.mark_completed(record, setup, watched_seconds=95.0)
    plan = tracker.plan_resume(course, record)
    assert plan.next_lesson is not None
    assert plan.next_lesson.title == "Functions"
    storage.close()


def test_resume_prefers_interrupted_lesson(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "progress.db")
    tracker = ProgressTracker(storage, _settings(tmp_path))
    course = _course()
    record, _ = tracker.sync_course(course)

    functions = course.lessons[3]
    tracker.mark_started(record, functions)
    record = storage.get_course_by_id(record.id)
    assert record is not None

    plan = tracker.plan_resume(course, record)
    assert plan.next_lesson is not None
    assert plan.next_lesson.key == functions.key
    storage.close()


def test_auto_resume_false_starts_at_active_lesson(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "progress.db")
    tracker = ProgressTracker(storage, _settings(tmp_path, auto_resume=False))
    course = _course(active_index=1)
    record, _ = tracker.sync_course(course)

    # Simulate an older interrupted lesson in the database.
    tracker.mark_started(record, course.lessons[3])
    record = storage.get_course_by_id(record.id)
    assert record is not None

    plan = tracker.plan_resume(course, record)
    assert plan.next_lesson is not None
    assert plan.next_lesson.key == course.lessons[1].key
    storage.close()


def test_failed_and_skipped_lessons_become_unavailable(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "progress.db")
    tracker = ProgressTracker(storage, _settings(tmp_path))
    course = _course()
    record, _ = tracker.sync_course(course)

    tracker.mark_failed(record, course.lessons[1], "stalled")
    for _ in range(3):
        storage.increment_attempt(record.id, course.lessons[1].key)
    tracker.mark_skipped(record, course.lessons[3], "assessment not submitted")

    record = storage.get_course_by_id(record.id)
    assert record is not None
    plan = tracker.plan_resume(course, record)
    assert course.lessons[1].key in plan.unavailable
    assert course.lessons[3].key in plan.unavailable
    assert plan.next_lesson is None  # intro completed, loops locked
    storage.close()


def test_platform_completion_is_reconciled_on_sync(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "progress.db")
    tracker = ProgressTracker(storage, _settings(tmp_path))
    course = _course()
    record, _ = tracker.sync_course(course)

    # Platform now shows "Setup" completed as well.
    course.sections[0].lessons[1].completed = True
    record, stats = tracker.sync_course(course)
    assert stats.completed == 2
    assert set(storage.completed_keys(record.id)) == {
        "/learning/test/intro",
        "/learning/test/setup",
    }
    storage.close()


def test_snapshot_and_finish(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "progress.db")
    tracker = ProgressTracker(storage, _settings(tmp_path))
    course = _course()
    record, _ = tracker.sync_course(course)
    session_id = tracker.start(record)

    record = storage.get_course_by_id(record.id)
    assert record is not None
    tracker.mark_completed(record, course.lessons[1], watched_seconds=30.0)
    snapshot = tracker.snapshot(record, current_lesson="Loops")

    assert snapshot.course_title == "Test Course"
    assert snapshot.total_lessons == 4
    assert snapshot.completed == 2
    assert snapshot.remaining == 2
    assert snapshot.current_lesson == "Loops"
    assert snapshot.watched_seconds == 30.0

    tracker.finish(record, status=ExecutionStatus.COMPLETED, session_id=session_id)
    finished = storage.get_course_by_id(record.id)
    assert finished is not None
    assert finished.status is ExecutionStatus.COMPLETED
    sessions = storage.get_sessions(record.id)
    assert sessions[0].status == "completed"
    storage.close()
