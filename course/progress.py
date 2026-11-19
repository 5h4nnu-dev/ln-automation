"""High-level progress management on top of SQLite.

``ProgressTracker`` is the only place that decides when a lesson becomes
completed. It persists progress immediately after every verified lesson and
computes the resume plan for an interrupted session.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from config import Settings
from course.curriculum import Course, Lesson, select_next_lesson
from database.models import (
    CourseRecord,
    CourseStats,
    ExecutionStatus,
    LessonRecord,
    LessonStatus,
    LessonUpsert,
    ProgressSnapshot,
)
from database.storage import Storage
from utils.logger import get_logger

logger = get_logger(__name__)


class ResumePlan(BaseModel):
    course_id: int
    completed: set[str] = Field(default_factory=set)
    unavailable: set[str] = Field(default_factory=set)
    resume_key: str | None = None
    next_lesson: Lesson | None = None
    stats: CourseStats = Field(default_factory=CourseStats)

    @property
    def is_complete(self) -> bool:
        return self.next_lesson is None


class ProgressTracker:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self.storage = storage
        self.settings = settings

    # ------------------------------------------------------------------ #
    # Synchronization
    # ------------------------------------------------------------------ #
    def sync_course(self, course: Course) -> tuple[CourseRecord, CourseStats]:
        """Upsert the course and its discovered lessons, then reconcile state."""

        record = self.storage.upsert_course(
            course.url,
            title=course.title,
            total_lessons=course.total_lessons,
            playback_speed=self.settings.playback_speed,
        )
        self.storage.sync_lessons(
            record.id,
            [
                LessonUpsert(
                    lesson_key=lesson.key,
                    position=lesson.position,
                    title=lesson.title,
                    url=lesson.url,
                    section=lesson.section,
                    duration_seconds=lesson.duration_seconds,
                    locked=lesson.locked,
                )
                for lesson in course.lessons
            ],
        )

        # The platform's own checkmarks are authoritative. This never marks a
        # lesson complete without a visible platform indicator.
        reconciled = self.storage.mark_completed_from_platform(record.id, course.completed_keys())
        if reconciled:
            logger.info(
                "Reconciled platform-completed lessons",
                extra={"event": "progress_reconciled", "count": reconciled},
            )

        stats = self.storage.stats(record.id)
        return self.storage.get_course_by_id(record.id) or record, stats

    # ------------------------------------------------------------------ #
    # Resume planning
    # ------------------------------------------------------------------ #
    def plan_resume(self, course: Course, record: CourseRecord) -> ResumePlan:
        completed = set(self.storage.completed_keys(record.id)) | set(course.completed_keys())
        lessons = self.storage.get_lessons(record.id)
        unavailable: set[str] = set()
        for stored in lessons:
            if stored.status is LessonStatus.SKIPPED:
                unavailable.add(stored.lesson_key)
            elif stored.status is LessonStatus.FAILED and stored.attempts >= self.settings.max_retries:
                unavailable.add(stored.lesson_key)

        preferred: str | None = None
        if self.settings.auto_resume and record.current_lesson_key:
            preferred = record.current_lesson_key
        elif not self.settings.auto_resume:
            # Start from whatever the user is looking at, if it is a real lesson.
            active = next((lesson for lesson in course.lessons if lesson.active), None)
            preferred = active.key if active else None

        next_lesson = select_next_lesson(
            course,
            completed=completed,
            unavailable=unavailable,
            preferred_key=preferred,
        )
        stats = self.storage.stats(record.id)
        return ResumePlan(
            course_id=record.id,
            completed=completed,
            unavailable=unavailable,
            resume_key=preferred,
            next_lesson=next_lesson,
            stats=stats,
        )

    # ------------------------------------------------------------------ #
    # Lesson lifecycle
    # ------------------------------------------------------------------ #
    def mark_started(self, record: CourseRecord, lesson: Lesson) -> None:
        self.storage.set_current_lesson(record.id, lesson.key)
        self.storage.mark_lesson_status(record.id, lesson.key, LessonStatus.IN_PROGRESS)
        attempts = self.storage.increment_attempt(record.id, lesson.key)
        self.storage.set_course_status(record.id, ExecutionStatus.RUNNING)
        logger.info(
            "Lesson started",
            extra={
                "event": "lesson_started",
                "lesson": lesson.title,
                "lesson_key": lesson.key,
                "attempt": attempts,
            },
        )

    def mark_completed(self, record: CourseRecord, lesson: Lesson, *, watched_seconds: float) -> None:
        self.storage.mark_lesson_status(
            record.id,
            lesson.key,
            LessonStatus.COMPLETED,
            watched_seconds=watched_seconds,
        )
        self.storage.log_event(
            record.id,
            "INFO",
            "lesson_completed",
            f"Lesson completed: {lesson.title}",
            {"lesson_key": lesson.key, "watched_seconds": round(watched_seconds, 1)},
        )
        logger.info(
            "Lesson completed",
            extra={
                "event": "lesson_completed",
                "lesson": lesson.title,
                "lesson_key": lesson.key,
                "watched_seconds": round(watched_seconds, 1),
            },
        )

    def mark_failed(self, record: CourseRecord, lesson: Lesson, error: str, *, watched_seconds: float = 0.0) -> None:
        self.storage.mark_lesson_status(
            record.id,
            lesson.key,
            LessonStatus.FAILED,
            error=error,
            watched_seconds=watched_seconds,
        )
        self.storage.log_event(
            record.id,
            "ERROR",
            "lesson_failed",
            f"Lesson failed: {lesson.title}",
            {"lesson_key": lesson.key, "error": error},
        )
        logger.error(
            "Lesson failed",
            extra={"event": "lesson_failed", "lesson": lesson.title, "lesson_key": lesson.key, "error": error},
        )

    def mark_skipped(self, record: CourseRecord, lesson: Lesson, reason: str) -> None:
        self.storage.mark_lesson_status(record.id, lesson.key, LessonStatus.SKIPPED, error=reason)
        self.storage.log_event(
            record.id,
            "WARNING",
            "lesson_skipped",
            f"Lesson skipped: {lesson.title}",
            {"lesson_key": lesson.key, "reason": reason},
        )
        logger.warning(
            "Lesson skipped",
            extra={"event": "lesson_skipped", "lesson": lesson.title, "lesson_key": lesson.key, "reason": reason},
        )

    def mark_needs_attention(self, record: CourseRecord, lesson: Lesson, reason: str) -> None:
        """Record a non-fatal issue without pretending the lesson completed."""

        self.storage.mark_lesson_status(record.id, lesson.key, LessonStatus.FAILED, error=reason)
        self.storage.log_event(
            record.id,
            "WARNING",
            "lesson_needs_attention",
            f"Lesson needs attention: {lesson.title}",
            {"lesson_key": lesson.key, "reason": reason},
        )
        logger.warning(
            "Lesson needs attention",
            extra={"event": "lesson_needs_attention", "lesson": lesson.title, "lesson_key": lesson.key, "reason": reason},
        )

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def stats(self, record: CourseRecord) -> CourseStats:
        return self.storage.stats(record.id)

    def lessons(self, record: CourseRecord) -> list[LessonRecord]:
        return self.storage.get_lessons(record.id)

    def watched_seconds(self, record: CourseRecord) -> float:
        return sum(lesson.watched_seconds for lesson in self.lessons(record))

    def snapshot(
        self,
        record: CourseRecord,
        *,
        current_lesson: str | None = None,
        status: ExecutionStatus | None = None,
    ) -> ProgressSnapshot:
        stats = self.stats(record)
        return ProgressSnapshot(
            course_url=record.url,
            course_title=record.title,
            status=status or record.status,
            total_lessons=stats.total,
            completed=stats.completed,
            remaining=stats.remaining,
            current_lesson=current_lesson,
            playback_speed=record.playback_speed,
            watched_seconds=self.watched_seconds(record),
            session_started_at=record.session_started_at,
            last_run_at=record.last_run_at,
        )

    # ------------------------------------------------------------------ #
    # Course/session lifecycle
    # ------------------------------------------------------------------ #
    def start(self, record: CourseRecord) -> int:
        self.storage.set_course_status(record.id, ExecutionStatus.RUNNING, start_session=True)
        return self.storage.start_session(record.id)

    def finish(
        self,
        record: CourseRecord,
        *,
        status: ExecutionStatus,
        session_id: int | None = None,
        notes: str | None = None,
    ) -> None:
        completed_before = self.stats(record).completed
        self.storage.set_course_status(record.id, status)
        if session_id is not None:
            self.storage.finish_session(
                session_id,
                status=status.value,
                lessons_completed=completed_before,
                notes=notes,
            )

    def set_playback_speed(self, record: CourseRecord, speed: float) -> None:
        self.storage.upsert_course(
            record.url,
            title=record.title,
            total_lessons=record.total_lessons,
            playback_speed=speed,
        )
