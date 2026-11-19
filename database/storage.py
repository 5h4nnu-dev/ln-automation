"""SQLite persistence for courses, lessons, sessions and structured events."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from database.models import (
    CourseRecord,
    CourseStats,
    EventRecord,
    ExecutionStatus,
    LessonRecord,
    LessonStatus,
    LessonUpsert,
    SessionRecord,
)
from utils.exceptions import StorageError
from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    total_lessons INTEGER NOT NULL DEFAULT 0,
    playback_speed REAL NOT NULL DEFAULT 2.0,
    current_lesson_key TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    session_started_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lesson_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    section TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    watched_seconds REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(course_id, lesson_key)
);

CREATE INDEX IF NOT EXISTS idx_lessons_course_position
    ON lessons(course_id, position);
CREATE INDEX IF NOT EXISTS idx_lessons_course_status
    ON lessons(course_id, status);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    lessons_completed INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_course_ts ON events(course_id, ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_to_course(row: sqlite3.Row) -> CourseRecord:
    try:
        status = ExecutionStatus(row["status"])
    except ValueError:
        status = ExecutionStatus.IDLE
    return CourseRecord(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        total_lessons=row["total_lessons"],
        playback_speed=row["playback_speed"],
        current_lesson_key=row["current_lesson_key"],
        status=status,
        session_started_at=_parse_dt(row["session_started_at"]),
        last_run_at=_parse_dt(row["last_run_at"]),
        created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
        updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
    )


def _row_to_lesson(row: sqlite3.Row) -> LessonRecord:
    try:
        status = LessonStatus(row["status"])
    except ValueError:
        status = LessonStatus.PENDING
    return LessonRecord(
        id=row["id"],
        course_id=row["course_id"],
        lesson_key=row["lesson_key"],
        position=row["position"],
        title=row["title"],
        url=row["url"],
        section=row["section"],
        duration_seconds=row["duration_seconds"],
        status=status,
        attempts=row["attempts"],
        watched_seconds=row["watched_seconds"],
        last_error=row["last_error"],
        completed_at=_parse_dt(row["completed_at"]),
        updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
    )


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        course_id=row["course_id"],
        started_at=_parse_dt(row["started_at"]) or datetime.now(timezone.utc),
        ended_at=_parse_dt(row["ended_at"]),
        status=row["status"],
        lessons_completed=row["lessons_completed"],
        notes=row["notes"],
    )


class Storage:
    """Thread-safe SQLite store.

    The application is single-threaded (asyncio), but ``check_same_thread=False``
    plus a lock makes the store safe for tests and future worker threads.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - depends on filesystem
            raise StorageError(f"Could not open the progress database: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Courses
    # ------------------------------------------------------------------ #
    def upsert_course(
        self,
        url: str,
        *,
        title: str = "",
        total_lessons: int = 0,
        playback_speed: float = 2.0,
    ) -> CourseRecord:
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM courses WHERE url = ?", (url,)).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO courses
                        (url, title, total_lessons, playback_speed, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (url, title, total_lessons, playback_speed, ExecutionStatus.IDLE.value, now, now),
                )
                course_id = int(cursor.lastrowid)
            else:
                course_id = int(row["id"])
                self._conn.execute(
                    """
                    UPDATE courses
                       SET title = ?, total_lessons = ?, playback_speed = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (title or row["title"], total_lessons, playback_speed, now, course_id),
                )
            row = self._conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
        assert row is not None
        return _row_to_course(row)

    def get_course(self, url: str) -> CourseRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM courses WHERE url = ?", (url,)).fetchone()
        return _row_to_course(row) if row else None

    def get_course_by_id(self, course_id: int) -> CourseRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
        return _row_to_course(row) if row else None

    def list_courses(self) -> list[CourseRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM courses ORDER BY updated_at DESC").fetchall()
        return [_row_to_course(row) for row in rows]

    def set_course_status(
        self,
        course_id: int,
        status: ExecutionStatus | str,
        *,
        current_lesson_key: str | None | object = ...,
        start_session: bool = False,
    ) -> None:
        value = status.value if isinstance(status, ExecutionStatus) else str(status)
        now = _now()
        with self._lock, self._conn:
            if start_session:
                self._conn.execute(
                    """
                    UPDATE courses
                       SET status = ?, last_run_at = ?, session_started_at = COALESCE(session_started_at, ?),
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (value, now, now, now, course_id),
                )
            elif current_lesson_key is not ...:
                self._conn.execute(
                    """
                    UPDATE courses
                       SET status = ?, last_run_at = ?, current_lesson_key = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (value, now, current_lesson_key, now, course_id),
                )
            else:
                self._conn.execute(
                    "UPDATE courses SET status = ?, last_run_at = ?, updated_at = ? WHERE id = ?",
                    (value, now, now, course_id),
                )

    def set_current_lesson(self, course_id: int, lesson_key: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE courses SET current_lesson_key = ?, updated_at = ? WHERE id = ?",
                (lesson_key, _now(), course_id),
            )

    def reset_course_progress(self, course_id: int) -> None:
        """Manual reset used by ``--reset``; never called automatically."""

        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE lessons
                   SET status = CASE WHEN status = 'locked' THEN 'locked' ELSE 'pending' END,
                       attempts = 0, watched_seconds = 0,
                       last_error = NULL, completed_at = NULL, updated_at = ?
                 WHERE course_id = ?
                """,
                (_now(), course_id),
            )
            self._conn.execute(
                """
                UPDATE courses
                   SET current_lesson_key = NULL, status = 'idle', session_started_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (_now(), course_id),
            )

    # ------------------------------------------------------------------ #
    # Lessons
    # ------------------------------------------------------------------ #
    def sync_lessons(self, course_id: int, lessons: Iterable[LessonUpsert]) -> int:
        now = _now()
        count = 0
        with self._lock, self._conn:
            for lesson in lessons:
                initial_status = LessonStatus.LOCKED.value if lesson.locked else LessonStatus.PENDING.value
                self._conn.execute(
                    """
                    INSERT INTO lessons
                        (course_id, lesson_key, position, section, title, url,
                         duration_seconds, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(course_id, lesson_key) DO UPDATE SET
                        position = excluded.position,
                        section = excluded.section,
                        title = excluded.title,
                        url = excluded.url,
                        duration_seconds = COALESCE(excluded.duration_seconds, lessons.duration_seconds),
                        status = CASE
                            WHEN lessons.status = 'completed' THEN 'completed'
                            WHEN excluded.status = 'locked' THEN 'locked'
                            WHEN lessons.status = 'locked' THEN 'pending'
                            ELSE lessons.status
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        course_id,
                        lesson.lesson_key,
                        lesson.position,
                        lesson.section,
                        lesson.title,
                        lesson.url,
                        lesson.duration_seconds,
                        initial_status,
                        now,
                    ),
                )
                count += 1
        return count

    def get_lessons(self, course_id: int) -> list[LessonRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lessons WHERE course_id = ? ORDER BY position ASC", (course_id,)
            ).fetchall()
        return [_row_to_lesson(row) for row in rows]

    def get_lesson(self, course_id: int, lesson_key: str) -> LessonRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lessons WHERE course_id = ? AND lesson_key = ?",
                (course_id, lesson_key),
            ).fetchone()
        return _row_to_lesson(row) if row else None

    def mark_lesson_status(
        self,
        course_id: int,
        lesson_key: str,
        status: LessonStatus,
        *,
        error: str | None = None,
        watched_seconds: float | None = None,
    ) -> None:
        now = _now()
        completed_at = now if status is LessonStatus.COMPLETED else None
        with self._lock, self._conn:
            if status is LessonStatus.COMPLETED:
                self._conn.execute(
                    """
                    UPDATE lessons
                       SET status = ?, last_error = NULL, completed_at = COALESCE(completed_at, ?),
                           watched_seconds = COALESCE(?, watched_seconds), updated_at = ?
                     WHERE course_id = ? AND lesson_key = ?
                    """,
                    (status.value, completed_at, watched_seconds, now, course_id, lesson_key),
                )
            elif status in (LessonStatus.FAILED, LessonStatus.SKIPPED):
                self._conn.execute(
                    """
                    UPDATE lessons
                       SET status = ?, last_error = ?, watched_seconds = COALESCE(?, watched_seconds),
                           updated_at = ?
                     WHERE course_id = ? AND lesson_key = ?
                    """,
                    (status.value, error, watched_seconds, now, course_id, lesson_key),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE lessons
                       SET status = ?, watched_seconds = COALESCE(?, watched_seconds), updated_at = ?
                     WHERE course_id = ? AND lesson_key = ?
                    """,
                    (status.value, watched_seconds, now, course_id, lesson_key),
                )

    def mark_completed_from_platform(self, course_id: int, lesson_keys: Iterable[str]) -> int:
        """Record completion the platform itself advertises (TOC checkmarks)."""

        keys = list(dict.fromkeys(lesson_keys))
        if not keys:
            return 0
        updated = 0
        now = _now()
        with self._lock, self._conn:
            for key in keys:
                row = self._conn.execute(
                    "SELECT status FROM lessons WHERE course_id = ? AND lesson_key = ?",
                    (course_id, key),
                ).fetchone()
                if row is None or row["status"] == LessonStatus.COMPLETED.value:
                    continue
                self._conn.execute(
                    """
                    UPDATE lessons
                       SET status = 'completed', completed_at = COALESCE(completed_at, ?),
                           last_error = NULL, updated_at = ?
                     WHERE course_id = ? AND lesson_key = ?
                    """,
                    (now, now, course_id, key),
                )
                updated += 1
        return updated

    def increment_attempt(self, course_id: int, lesson_key: str) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE lessons
                   SET attempts = attempts + 1, updated_at = ?
                 WHERE course_id = ? AND lesson_key = ?
                """,
                (_now(), course_id, lesson_key),
            )
            row = self._conn.execute(
                "SELECT attempts FROM lessons WHERE course_id = ? AND lesson_key = ?",
                (course_id, lesson_key),
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def stats(self, course_id: int) -> CourseStats:
        stats = CourseStats()
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM lessons WHERE course_id = ? GROUP BY status",
                (course_id,),
            ).fetchall()
        for row in rows:
            count = int(row["n"])
            status = row["status"]
            stats.total += count
            if status in {item.value for item in LessonStatus}:
                setattr(stats, status, getattr(stats, status) + count)
        stats.pending += stats.in_progress
        stats.in_progress = 0
        return stats

    def completed_keys(self, course_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT lesson_key FROM lessons WHERE course_id = ? AND status = 'completed'",
                (course_id,),
            ).fetchall()
        return [row["lesson_key"] for row in rows]

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def start_session(self, course_id: int) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO sessions(course_id, started_at, status) VALUES (?, ?, 'running')",
                (course_id, _now()),
            )
            return int(cursor.lastrowid)

    def finish_session(
        self,
        session_id: int,
        *,
        status: str = "finished",
        lessons_completed: int = 0,
        notes: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE sessions
                   SET ended_at = ?, status = ?, lessons_completed = ?, notes = ?
                 WHERE id = ?
                """,
                (_now(), status, lessons_completed, notes, session_id),
            )

    def get_sessions(self, course_id: int) -> list[SessionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE course_id = ? ORDER BY id DESC", (course_id,)
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Structured events
    # ------------------------------------------------------------------ #
    def log_event(
        self,
        course_id: int | None,
        level: str,
        event: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            encoded = json.dumps(payload, default=str) if payload else None
        except (TypeError, ValueError):
            encoded = json.dumps({"payload": repr(payload)})
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(course_id, ts, level, event, message, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (course_id, _now(), level.upper(), event, message, encoded),
            )

    def get_recent_events(self, course_id: int | None = None, limit: int = 20) -> list[EventRecord]:
        with self._lock:
            if course_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE course_id = ? ORDER BY id DESC LIMIT ?",
                    (course_id, limit),
                ).fetchall()
        records: list[EventRecord] = []
        for row in reversed(rows):
            payload = None
            if row["payload"]:
                try:
                    payload = json.loads(row["payload"])
                except json.JSONDecodeError:
                    payload = {"raw": row["payload"]}
            records.append(
                EventRecord(
                    id=row["id"],
                    course_id=row["course_id"],
                    ts=_parse_dt(row["ts"]) or datetime.now(timezone.utc),
                    level=row["level"],
                    event=row["event"],
                    message=row["message"],
                    payload=payload,
                )
            )
        return records
