"""Domain models shared by persistence, navigation and the dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionStatus(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    AUTH_REQUIRED = "auth_required"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


class LessonStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    LOCKED = "locked"


class LessonUpsert(BaseModel):
    """Neutral input used to synchronize discovered lessons into SQLite."""

    lesson_key: str
    position: int
    title: str
    url: str = ""
    section: str | None = None
    duration_seconds: float | None = None
    locked: bool = False


class CourseRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    title: str
    total_lessons: int
    playback_speed: float
    current_lesson_key: str | None = None
    status: ExecutionStatus = ExecutionStatus.IDLE
    session_started_at: datetime | None = None
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LessonRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    lesson_key: str
    position: int
    title: str
    url: str
    section: str | None = None
    duration_seconds: float | None = None
    status: LessonStatus = LessonStatus.PENDING
    attempts: int = 0
    watched_seconds: float = 0.0
    last_error: str | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class CourseStats(BaseModel):
    total: int = 0
    completed: int = 0
    pending: int = 0
    in_progress: int = 0
    skipped: int = 0
    failed: int = 0
    locked: int = 0

    @property
    def remaining(self) -> int:
        return max(self.total - self.completed, 0)


class SessionRecord(BaseModel):
    id: int
    course_id: int
    started_at: datetime
    ended_at: datetime | None = None
    status: str = "running"
    lessons_completed: int = 0
    notes: str | None = None


class EventRecord(BaseModel):
    id: int
    course_id: int | None = None
    ts: datetime
    level: str
    event: str
    message: str
    payload: dict | None = None


class ProgressSnapshot(BaseModel):
    """Serializable view of persisted progress for display / resume."""

    course_url: str
    course_title: str
    status: ExecutionStatus
    total_lessons: int
    completed: int
    remaining: int
    current_lesson: str | None = None
    playback_speed: float = 2.0
    watched_seconds: float = 0.0
    session_started_at: datetime | None = None
    last_run_at: datetime | None = None
    generated_at: datetime = Field(default_factory=utc_now)
