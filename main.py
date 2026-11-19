"""LinkedIn Learning automation entry point.

The runner coordinates the browser, video controller, curriculum navigator,
assessment assistant and SQLite progress store. All browser interaction stays in
``browser``/``course``/``player``/``assessment``; this module only decides the
workflow and renders state through the Rich UI.

Safety guarantees:
* no credentials are requested or stored;
* a lesson is only marked completed after the platform's own DOM advertises it;
* assessment answers are never selected or submitted by the tool.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from assessment.assistant import AssessmentAssistant, Suggestion
from assessment.detector import AssessmentDetector
from assessment.extractor import Question
from browser.manager import AuthStatus, BrowserManager
from config import LOG_LEVELS, Settings
from course.curriculum import Course, CourseNavigator, Lesson
from course.progress import ProgressTracker
from database.models import CourseRecord, CourseStats, ExecutionStatus, LessonStatus
from database.storage import Storage
from player.video_controller import VideoController, VideoState
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
)
from utils.exceptions import (
    AutomationError,
    BrowserClosedError,
    ConfigurationError,
    CourseListError,
    CurriculumError,
    LessonAccessError,
    NavigationError,
    ShutdownRequested,
    VideoNotFoundError,
)
from utils.course_list import read_course_urls
from utils.logger import configure_logging, get_logger
from utils.selectors import load_selectors

logger = get_logger(__name__)


def _fmt_seconds(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "0:00"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class CourseRunner:
    """Coordinates one course run and exposes commands for the console."""

    def __init__(
        self,
        settings: Settings,
        *,
        console: Console | None = None,
        live: bool = True,
        command_stream: Any | None = None,
        course_urls: Sequence[str] | None = None,
    ) -> None:
        self.settings = settings
        self.course_urls = (
            [url.strip() for url in course_urls if url and url.strip()]
            if course_urls is not None
            else ([settings.course_url.strip()] if settings.course_url.strip() else [])
        )
        self.outcomes: list[CourseOutcome] = []
        self._summary_printed = False
        self.console = console or Console()
        self.selectors = load_selectors(settings.selectors_path)
        self.storage = Storage(settings.database_path)
        self.browser = BrowserManager(settings, self.selectors)
        self.navigator = CourseNavigator(lambda: self.browser.page, self.selectors, settings)
        self.player = VideoController(lambda: self.browser.page, self.selectors, settings)
        self.detector = AssessmentDetector(lambda: self.browser.page, self.selectors, settings)
        self.assistant = AssessmentAssistant()
        self.tracker = ProgressTracker(self.storage, settings)

        self.state = AppState(
            course_url=self.course_urls[0] if self.course_urls else settings.course_url,
            playback_speed=settings.playback_speed,
        )
        self.dashboard = Dashboard(self.console, lambda: self.state) if live else None
        self.ui = TerminalUI(self.console, self.dashboard)
        self.commands = CommandConsole(
            self.ui,
            {
                Command.START: self._cmd_start,
                Command.PAUSE: self._cmd_pause,
                Command.RESUME: self._cmd_resume,
                Command.PROGRESS: self._cmd_progress,
                Command.SPEED: self._cmd_speed,
                Command.EXIT: self._cmd_exit,
            },
            stream=command_stream,
        )

        self._record: CourseRecord | None = None
        self._course: Course | None = None
        self._session_id: int | None = None
        self._resume = asyncio.Event()
        self._resume.set()
        self._stop = False
        self._consecutive_failures = 0
        self._last_auth_notice = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._signal_handlers_installed = False

    # ------------------------------------------------------------------ #
    # Public lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> ExecutionStatus:
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        self.state.started_at = datetime.now()
        self.state.status = ExecutionStatus.STARTING
        self.state.playback_speed = self.settings.playback_speed

        try:
            if self.dashboard is not None:
                await self.dashboard.start()
            async with self.commands.listen():
                await self._run_session()
        except ShutdownRequested:
            self.state.status = ExecutionStatus.STOPPED
            self.state.detail = "Stopped by user. Run again to resume."
            self.state.add_activity("Shutdown requested", "WARNING")
        except AutomationError as exc:
            self.state.status = ExecutionStatus.ERROR
            self.state.last_error = str(exc)
            self.state.add_activity(str(exc), "ERROR")
            logger.error("Automation stopped: %s", exc, extra={"event": "run_failed"})
        except asyncio.CancelledError:
            self.state.status = ExecutionStatus.STOPPED
            self.state.detail = "Run cancelled."
            raise
        except Exception as exc:  # pragma: no cover - defensive top-level guard
            logger.exception("Unexpected failure")
            self.state.status = ExecutionStatus.ERROR
            self.state.last_error = f"{type(exc).__name__}: {exc}"
            self.state.add_activity(str(exc), "ERROR")
        finally:
            await self._finalize()

        return self.state.status

    def request_stop(self) -> None:
        """Signal-safe shutdown request used by signals and the exit command."""

        if self._stop:
            return
        self._stop = True
        self._resume.set()
        self.state.status = ExecutionStatus.STOPPED
        self.state.detail = "Stopping after the current poll…"
        self.state.add_activity("Graceful shutdown requested", "WARNING")

    # ------------------------------------------------------------------ #
    # Session workflow
    # ------------------------------------------------------------------ #
    async def _run_session(self) -> None:
        urls = list(self.course_urls)
        if not urls:
            raise ConfigurationError(
                "No course URLs to run.",
                hint="Set COURSE_URL or pass --courses-file with a CSV/text list.",
            )

        total = len(urls)
        try:
            for index, url in enumerate(urls, start=1):
                if self._stop:
                    raise ShutdownRequested()
                self.state.course_index = index
                self.state.course_total = total
                self._reset_for_course(url)
                self.state.add_activity(f"Course {index}/{total}: {url}")
                outcome = await self._run_one_course(url)
                self.outcomes.append(outcome)
                if self._stop:
                    break
        except ShutdownRequested:
            if total > 1 and self.outcomes:
                await self.ui.print(batch_summary_table(self.outcomes))
            raise

        if total > 1 and self.outcomes:
            await self.ui.print(batch_summary_table(self.outcomes))
            failed = [item for item in self.outcomes if item.status is ExecutionStatus.ERROR]
            if failed and not self._stop:
                self.state.status = ExecutionStatus.ERROR
                self.state.detail = (
                    f"{len(failed)} of {len(self.outcomes)} course(s) failed. See the batch summary."
                )
                self.state.last_error = self.state.detail

    def _reset_for_course(self, url: str) -> None:
        """Clear per-course view state before starting the next batch item."""

        self._course = None
        self._record = None
        self._session_id = None
        self._consecutive_failures = 0
        self.state.course_title = ""
        self.state.course_url = url
        self.state.total_lessons = 0
        self.state.completed = 0
        self.state.skipped = 0
        self.state.locked = 0
        self.state.current_lesson = None
        self.state.current_section = None
        self.state.playback_position = 0.0
        self.state.playback_duration = 0.0
        self.state.lesson_watched_seconds = 0.0
        self.state.total_watched_seconds = 0.0
        self.state.assessments = 0
        self.state.detail = ""
        self.state.last_error = None

    async def _run_one_course(self, url: str) -> CourseOutcome:
        """Run one course. Failures are returned so a batch can continue."""

        outcome = CourseOutcome(url=url, status=ExecutionStatus.ERROR)
        try:
            await self._open_course(url)

            self.state.status = ExecutionStatus.STARTING
            self.state.detail = "Reading the course curriculum…"
            course = await self._discover(url)
            self._course = course
            self.state.course_title = course.title
            self.state.course_url = course.url
            self.state.add_activity(f"Curriculum discovered: {course.total_lessons} lessons")

            record, stats = self.tracker.sync_course(course)
            self._record = record
            self._session_id = self.tracker.start(record)
            self._refresh_ui(record, stats=stats)
            self.state.status = ExecutionStatus.RUNNING
            self.state.detail = "Starting course automation."
            if not self.settings.auto_start:
                self._resume.clear()
                self.state.status = ExecutionStatus.PAUSED
                self.state.detail = "Ready. Type `start` (or `resume`) to begin playback."
                self.state.add_activity("AUTO_START is disabled; waiting for the start command")

            while not self._stop:
                await self._wait_while_paused()
                record = self._reload_record()
                if record is None:
                    raise AutomationError("Course progress record disappeared.")

                plan = self.tracker.plan_resume(course, record)
                self._refresh_ui(record, stats=plan.stats)
                self.state.current_lesson = (
                    plan.next_lesson.display_title if plan.next_lesson else None
                )
                self.state.current_section = plan.next_lesson.section if plan.next_lesson else None

                if plan.next_lesson is None:
                    self.state.add_activity("No further accessible lessons remain", "WARNING")
                    break

                await self._process_lesson(plan.next_lesson)

                if self._stop:
                    break
                if not self.settings.auto_navigate:
                    # Keep the browser on the finished lesson until the operator
                    # explicitly resumes. The next lesson is still chosen from the
                    # discovered curriculum, never by screen coordinates.
                    self._resume.clear()
                    self.state.status = ExecutionStatus.PAUSED
                    self.state.detail = (
                        "AUTO_NAVIGATE is disabled. Type `resume` to open the next lesson."
                    )
                    self.state.add_activity("Auto-navigation paused after the lesson")

            record = self._reload_record()
            if record is None:
                raise AutomationError("Course progress record disappeared before final summary.")
            stats = self.tracker.stats(record)
            self._refresh_ui(record, stats=stats)

            if stats.remaining == 0:
                self.state.status = ExecutionStatus.COMPLETED
                self.state.detail = "All discovered lessons were verified complete by the platform."
                self.state.add_activity("Course completed", "INFO")
            elif self._stop:
                self.state.status = ExecutionStatus.STOPPED
                self.state.detail = (
                    f"Stopped with {stats.remaining} lesson(s) remaining. Run again to resume."
                )
            else:
                self.state.status = ExecutionStatus.STOPPED
                self.state.detail = (
                    f"{stats.remaining} lesson(s) remain (locked, skipped or failed). "
                    "See the lesson list and logs for details."
                )

            outcome.status = self.state.status
        except ShutdownRequested:
            self.state.status = ExecutionStatus.STOPPED
            if not self.state.detail:
                self.state.detail = "Stopped by user. Run again to resume."
            self.state.add_activity("Shutdown requested", "WARNING")
            outcome.status = ExecutionStatus.STOPPED
            outcome.error = self.state.detail
            raise
        except AutomationError as exc:
            logger.error("Course failed: %s", exc, extra={"event": "course_failed", "url": url})
            self.state.status = ExecutionStatus.ERROR
            self.state.last_error = str(exc)
            self.state.detail = str(exc)
            self.state.add_activity(f"Course failed: {exc}", "ERROR")
            outcome.status = ExecutionStatus.ERROR
            outcome.error = str(exc)
        except Exception as exc:  # one bad course must not abort the batch
            logger.exception("Unexpected course failure for %s", url)
            message = f"{type(exc).__name__}: {exc}"
            self.state.status = ExecutionStatus.ERROR
            self.state.last_error = message
            self.state.detail = message
            self.state.add_activity(f"Course failed: {message}", "ERROR")
            outcome.status = ExecutionStatus.ERROR
            outcome.error = message
        finally:
            outcome.title = self.state.course_title or url
            outcome.completed = self.state.completed
            outcome.total = self.state.total_lessons
            self._persist_current_course()
            self._summary_printed = True
            await self.ui.print(summary_panel(self.state))

        return outcome

    async def _open_course(self, url: str) -> None:
        self.state.detail = "Launching Chromium…"
        await self.browser.open_url(url)
        self.state.browser_ready = True
        self.state.status = ExecutionStatus.STARTING
        self.state.add_activity("Browser initialized")

        if await self._wait_for_login_if_needed():
            await self.browser.open_url(url)
        self.state.add_activity("LinkedIn session checked")

    async def _wait_for_login_if_needed(self) -> bool:
        """Wait for a manual login. Returns ``True`` if a login was required."""

        try:
            status = await self.browser.detect_auth_status()
        except BrowserClosedError:
            await self.browser.ensure_ready()
            status = await self.browser.detect_auth_status()

        if status is not AuthStatus.UNAUTHENTICATED:
            if status is AuthStatus.UNKNOWN:
                self.state.add_activity(
                    "Session could not be positively verified; continuing", "WARNING"
                )
            return False

        self.state.status = ExecutionStatus.AUTH_REQUIRED
        self.state.detail = "Log in to LinkedIn in the browser window."
        self.state.add_activity("Manual LinkedIn login required", "WARNING")
        self._last_auth_notice = 0.0

        await self.browser.wait_for_authentication(
            timeout=self.settings.auth_timeout,
            on_wait=self._on_auth_wait,
        )
        self.state.add_activity("LinkedIn session authenticated")
        self.state.status = ExecutionStatus.STARTING
        self.state.detail = "Login detected; reopening the course."
        return True

    def _on_auth_wait(self, elapsed: float) -> None:
        if self._stop:
            raise ShutdownRequested()
        self.state.detail = f"Waiting for manual login ({int(elapsed)}s / {int(self.settings.auth_timeout)}s)"
        if elapsed - self._last_auth_notice >= 30:
            self._last_auth_notice = elapsed
            self.state.add_activity(
                "Still waiting for manual login in the browser", "WARNING"
            )

    async def _discover(self, course_url: str) -> Course:
        last_error: Exception | None = None
        attempts = max(3, self.settings.max_retries + 1)
        for attempt in range(attempts):
            if self._stop:
                raise ShutdownRequested()
            try:
                return await self.navigator.discover(course_url)
            except CurriculumError as exc:
                last_error = exc
                logger.warning(
                    "Curriculum discovery failed",
                    extra={"event": "curriculum_retry", "attempt": attempt + 1},
                )
                if await self._wait_for_login_if_needed():
                    await self.browser.open_url(course_url)
                    continue
                self.state.detail = (
                    "Waiting for the course curriculum to appear "
                    f"({attempt + 1}/{attempts})…"
                )
                await self._sleep(1.0 + attempt, interruptible=True)

        if isinstance(last_error, CurriculumError):
            raise last_error
        raise CurriculumError("The course curriculum could not be discovered.")

    # ------------------------------------------------------------------ #
    # Lesson workflow
    # ------------------------------------------------------------------ #
    async def _process_lesson(self, lesson: Lesson) -> None:
        record = self._reload_record()
        if record is None:
            raise AutomationError("No course record is available for the lesson.")

        self.state.status = ExecutionStatus.RUNNING
        self.state.current_lesson = lesson.display_title
        self.state.current_section = lesson.section
        self.state.playback_position = 0.0
        self.state.playback_duration = 0.0
        self.state.lesson_watched_seconds = 0.0
        self.state.detail = "Opening lesson"
        self.state.add_activity(f"Lesson started: {lesson.title}")
        self.tracker.mark_started(record, lesson)
        self._record = self._reload_record()

        try:
            await self._open_lesson(lesson)
        except ShutdownRequested:
            raise
        except LessonAccessError as exc:
            self._mark_skipped(lesson, str(exc))
            return
        except BrowserClosedError as exc:
            self._handle_failure(lesson, str(exc))
            return
        except NavigationError as exc:
            self._handle_failure(lesson, str(exc))
            return

        # Quiz-only lessons do not always contain a video. Look for an
        # assessment first so the user can submit it before we verify progress.
        if self.settings.assessment_enabled:
            questions = await self.detector.wait_for_assessment(timeout=1.5)
            if questions:
                submitted = await self._handle_assessment(lesson, questions)
                if submitted:
                    result = await self._verify_platform(lesson)
                    if result.get("verified"):
                        self._mark_completed(lesson, watched_seconds=0.0)
                    elif result.get("locked"):
                        self._mark_skipped(lesson, str(result.get("reason") or "Locked lesson"))
                    else:
                        self._handle_failure(
                            lesson,
                            str(result.get("reason") or "The platform did not confirm completion."),
                        )
                else:
                    self._handle_failure(
                        lesson,
                        "The assessment was not submitted within the configured timeout.",
                    )
                return

        self.state.detail = "Waiting for the video player"
        try:
            await self.player.wait_for_video(timeout=min(self.settings.stall_timeout, 30.0))
        except ShutdownRequested:
            raise
        except VideoNotFoundError as exc:
            if self.settings.assessment_enabled:
                questions = await self.detector.wait_for_assessment(timeout=2.0)
                if questions:
                    submitted = await self._handle_assessment(lesson, questions)
                    if submitted:
                        result = await self._verify_platform(lesson)
                        if result.get("verified"):
                            self._mark_completed(lesson, watched_seconds=0.0)
                            return
                    self._handle_failure(
                        lesson,
                        "An assessment was found but completion was not confirmed.",
                    )
                    return
            self._handle_failure(lesson, str(exc))
            return

        self.state.status = ExecutionStatus.RUNNING
        self.state.detail = f"Playing at {self.settings.playback_speed:.2f}x"
        self.state.add_activity(f"Playback started: {lesson.title}")

        try:
            result = await self.player.wait_for_completion(
                lesson_key=lesson.key,
                should_pause=lambda: not self._resume.is_set(),
                should_stop=lambda: self._stop,
                on_progress=self._on_playback_progress,
                start_url=self.browser.page.url,
            )
        except ShutdownRequested:
            raise
        except AutomationError as exc:
            self._handle_failure(lesson, str(exc))
            return
        except Exception as exc:  # defensive: one lesson must not crash the run
            logger.exception("Unexpected playback failure")
            self._handle_failure(lesson, f"{type(exc).__name__}: {exc}")
            return

        self.state.playback_position = result.watched_seconds
        self.state.playback_duration = result.duration
        self.state.lesson_watched_seconds = result.watched_seconds

        # A quiz can appear right after the media ends.
        assessment_handled = False
        if self.settings.assessment_enabled:
            questions = await self.detector.wait_for_assessment(timeout=1.5)
            if questions:
                assessment_handled = await self._handle_assessment(lesson, questions)

        verification = await self._verify_platform(lesson)
        if not verification.get("verified") and self.settings.assessment_enabled and not assessment_handled:
            questions = await self.detector.detect()
            if questions:
                await self._handle_assessment(lesson, questions)
                verification = await self._verify_platform(lesson)

        watched = max(result.watched_seconds, self.state.lesson_watched_seconds, 0.0)
        if verification.get("verified"):
            self._mark_completed(lesson, watched_seconds=watched)
            return
        if verification.get("locked"):
            self._mark_skipped(
                lesson, str(verification.get("reason") or "The platform marks this lesson as locked.")
            )
            return

        self._handle_failure(
            lesson,
            str(verification.get("reason") or "The platform did not confirm completion."),
            watched_seconds=watched,
        )

    async def _open_lesson(self, lesson: Lesson) -> None:
        record = self._reload_record()

        async def _navigate() -> None:
            await self.navigator.open_lesson(
                lesson,
                course_url=record.url if record else None,
                retries=max(0, min(self.settings.max_retries - 1, 2)),
            )

        try:
            await _navigate()
        except BrowserClosedError:
            self.state.add_activity("Browser window was lost; reopening", "WARNING")
            await self.browser.ensure_ready()
            await _navigate()
        except LessonAccessError:
            if not self.browser.is_running:
                self.state.add_activity("Browser window was lost; reopening", "WARNING")
                await self.browser.ensure_ready()
                await _navigate()
            else:
                raise

        if await self._wait_for_login_if_needed():
            await _navigate()

        # A navigation can land on the login page without raising.
        status = await self.browser.detect_auth_status()
        if status is AuthStatus.UNAUTHENTICATED:
            if await self._wait_for_login_if_needed():
                await _navigate()

    async def _verify_platform(self, lesson: Lesson) -> dict:
        self.state.status = ExecutionStatus.WAITING
        self.state.detail = "Waiting for the platform to register legitimate completion"
        record = self._reload_record()
        try:
            return await self.navigator.wait_for_platform_completion(
                lesson,
                course_url=record.url if record else None,
                should_stop=lambda: self._stop,
            )
        except ShutdownRequested:
            raise
        except Exception as exc:
            logger.warning("Completion verification failed: %s", exc)
            return {
                "verified": False,
                "found": False,
                "completed": False,
                "locked": False,
                "reason": f"Completion check failed: {exc}",
            }

    def _mark_completed(self, lesson: Lesson, *, watched_seconds: float) -> None:
        record = self._reload_record()
        if record is None:
            return
        self.tracker.mark_completed(record, lesson, watched_seconds=watched_seconds)
        self._consecutive_failures = 0
        self.state.add_activity(f"Lesson verified complete: {lesson.title}")
        self.state.detail = "Lesson completed and saved."
        self._record = self._reload_record()
        if self._record is not None:
            self._refresh_ui(self._record)

    def _mark_skipped(self, lesson: Lesson, reason: str) -> None:
        record = self._reload_record()
        if record is None:
            return
        self.tracker.mark_skipped(record, lesson, reason)
        self.session_unavailable(lesson, reason)
        self._consecutive_failures = 0

    def session_unavailable(self, lesson: Lesson, reason: str) -> None:
        self.state.add_activity(f"Lesson unavailable: {lesson.title}", "WARNING")
        self.state.detail = reason
        self._record = self._reload_record()
        if self._record is not None:
            self._refresh_ui(self._record)

    def _handle_failure(
        self,
        lesson: Lesson,
        error: str,
        *,
        watched_seconds: float = 0.0,
    ) -> None:
        record = self._reload_record()
        if record is None:
            return
        self.tracker.mark_failed(record, lesson, error, watched_seconds=watched_seconds)
        stored = self.storage.get_lesson(record.id, lesson.key)
        attempts = stored.attempts if stored else 0
        self.state.last_error = error
        self.state.detail = error
        self.state.add_activity(f"Lesson failed: {lesson.title}", "ERROR")
        self._record = self._reload_record()
        if self._record is not None:
            self._refresh_ui(self._record)

        if attempts >= self.settings.max_retries:
            # Exclude the lesson from automatic retries, but keep it visible as
            # failed so the run can continue with the remaining lessons.
            self.state.add_activity(
                f"Giving up on '{lesson.title}' after {attempts} attempts", "WARNING"
            )
            self._consecutive_failures = 0
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self.settings.max_consecutive_failures:
            raise AutomationError(
                f"Stopping after {self._consecutive_failures} consecutive lesson failures. "
                f"Last error: {error}",
                hint=(
                    "The course layout may have changed. Check the browser window, "
                    "update utils/selectors.py if needed, then resume."
                ),
            )

    def _on_playback_progress(self, video_state: VideoState, watched: float) -> None:
        self.state.playback_position = video_state.current_time
        self.state.playback_duration = video_state.duration
        self.state.lesson_watched_seconds = watched

    # ------------------------------------------------------------------ #
    # Assessments
    # ------------------------------------------------------------------ #
    async def _handle_assessment(self, lesson: Lesson, questions: Sequence[Question]) -> bool:
        """Display questions and wait for the user to submit them manually.

        Returns ``True`` when the assessment disappeared (submitted), ``False``
        when the wait timed out or the run is stopping. Nothing is clicked or
        submitted by this method.
        """

        if not questions:
            return True

        course = self._course
        context = [
            course.title if course else "",
            lesson.section or "",
            lesson.title,
        ]
        context = [part for part in context if part]

        self.state.status = ExecutionStatus.WAITING
        self.state.detail = "Assessment detected — waiting for manual submission"
        self.state.add_activity(
            f"Assessment detected: {len(questions)} question(s)", "WARNING"
        )

        remaining: list[Question] = list(questions)
        cycles = 0
        while remaining and cycles < 10:
            cycles += 1
            self.state.assessments += len(remaining)
            suggestions: list[Suggestion] = [
                self.assistant.suggest(question, context) for question in remaining
            ]
            panels = [
                assessment_panel(
                    question,
                    suggestion,
                    index=index,
                    total=len(remaining),
                )
                for index, (question, suggestion) in enumerate(
                    zip(remaining, suggestions), start=1
                )
            ]
            submitted = await self.ui.wait_for_manual_submission(
                self.detector,
                timeout=self.settings.assessment_wait_timeout,
                should_stop=lambda: self._stop,
                panels=panels,
            )
            if not submitted:
                self.state.add_activity(
                    "Assessment still open after the wait timeout; leaving the lesson pending.",
                    "WARNING",
                )
                self.state.detail = "Assessment was not submitted."
                return False
            self.state.add_activity("Assessment submitted manually; checking completion")
            try:
                await self._sleep(0.8, interruptible=True)
            except ShutdownRequested:
                raise
            try:
                remaining = await self.detector.detect()
            except Exception:
                remaining = []

        return not remaining

    # ------------------------------------------------------------------ #
    # Command handlers
    # ------------------------------------------------------------------ #
    async def _cmd_start(self, argument: str | None = None) -> str:
        if self._stop:
            return "A shutdown is already in progress."
        if not self._resume.is_set():
            self._resume.set()
            self.state.status = ExecutionStatus.RUNNING
            self.state.detail = "Resumed by user."
            self.state.add_activity("Automation resumed")
            return "Automation resumed."
        return "Automation is already running."

    async def _cmd_pause(self, argument: str | None = None) -> str:
        if self._stop:
            return "A shutdown is already in progress."
        self._resume.clear()
        self.state.status = ExecutionStatus.PAUSED
        self.state.detail = "Paused by user. Use `resume` or `start` to continue."
        self.state.add_activity("Paused by user")
        return "Paused. Playback stops at the next poll and resumes on `resume`."

    async def _cmd_resume(self, argument: str | None = None) -> str:
        return await self._cmd_start(argument)

    async def _cmd_progress(self, argument: str | None = None) -> Table:
        record = self._reload_record()
        if record is not None:
            self._refresh_ui(record)
        return progress_table(self.state)

    async def _cmd_speed(self, argument: str | None = None) -> str:
        if argument is None:
            return "Usage: speed <0.25-4.0>   (for example: speed 2.5)"
        try:
            value = float(argument)
        except (TypeError, ValueError):
            return f"'{argument}' is not a number. Usage: speed <0.25-4.0>"
        if not 0.25 <= value <= 4.0:
            return "Playback speed must be between 0.25x and 4.0x."

        if value > self.settings.max_playback_speed:
            self.settings.max_playback_speed = value
        self.settings.playback_speed = value
        self.state.playback_speed = value
        record = self._reload_record()
        if record is not None:
            self.tracker.set_playback_speed(record, value)
            self._record = self._reload_record()
        self.state.add_activity(f"Playback speed set to {value:.2f}x")
        self.state.detail = f"Playback speed {value:.2f}x"
        return (
            f"Playback speed set to {value:.2f}x. "
            "It is applied to the active player at the next poll."
        )

    async def _cmd_exit(self, argument: str | None = None) -> str:
        self.request_stop()
        return "Shutting down gracefully. Progress is saved after every verified lesson."

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _reload_record(self) -> CourseRecord | None:
        if self._record is None:
            return None
        updated = self.storage.get_course_by_id(self._record.id)
        if updated is not None:
            self._record = updated
        return self._record

    def _refresh_ui(
        self,
        record: CourseRecord | None = None,
        *,
        stats: CourseStats | None = None,
    ) -> None:
        record = record or self._record
        if record is None:
            return
        stats = stats or self.tracker.stats(record)
        self.state.course_title = record.title
        self.state.course_url = record.url
        self.state.total_lessons = stats.total
        self.state.completed = stats.completed
        self.state.skipped = stats.skipped
        self.state.locked = stats.locked
        self.state.playback_speed = self.settings.playback_speed
        try:
            self.state.total_watched_seconds = self.tracker.watched_seconds(record)
        except Exception:
            pass
        if self.state.current_lesson is None and record.current_lesson_key:
            stored = self.storage.get_lesson(record.id, record.current_lesson_key)
            if stored is not None:
                self.state.current_lesson = stored.title
                self.state.current_section = stored.section

    async def _wait_while_paused(self) -> None:
        while not self._resume.is_set():
            if self._stop:
                raise ShutdownRequested()
            await asyncio.sleep(0.2)
        if self._stop:
            raise ShutdownRequested()

    async def _sleep(self, seconds: float, *, interruptible: bool = False) -> None:
        deadline = time.monotonic() + max(seconds, 0.0)
        while True:
            if interruptible and self._stop:
                raise ShutdownRequested()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.25 if interruptible else remaining))

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
                self._signal_handlers_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                continue

    def _persist_current_course(self) -> CourseRecord | None:
        """Close out the active course session and return its record."""

        record = self._reload_record()
        if record is None:
            return None

        status = self.state.status
        if status not in {ExecutionStatus.COMPLETED, ExecutionStatus.ERROR} and self._stop:
            status = ExecutionStatus.STOPPED
            self.state.status = status
        if status is ExecutionStatus.ERROR and not self.state.detail:
            self.state.detail = "The run stopped after an error; see last error and logs."

        try:
            if self._session_id is not None:
                self.tracker.finish(
                    record,
                    status=status,
                    session_id=self._session_id,
                    notes=self.state.detail or None,
                )
                self._session_id = None
            else:
                self.storage.set_course_status(record.id, status)
        except Exception:
            logger.exception("Could not persist the final session state")
        self._refresh_ui(record)
        return record

    async def _finalize(self) -> None:
        if self.dashboard is not None:
            await self.dashboard.stop()

        needs_summary = not self._summary_printed
        record = self._persist_current_course()

        try:
            await self.browser.close()
        except Exception:
            pass
        self.storage.close()

        if self._loop is not None and self._signal_handlers_installed:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    self._loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            self._signal_handlers_installed = False

        if record is not None and needs_summary:
            self.console.print(summary_panel(self.state))


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #
def resolve_course_urls(settings: Settings) -> list[str]:
    """Return the run list: course file wins over a single configured URL."""

    if settings.courses_file is not None:
        return read_course_urls(settings.courses_file)
    return [settings.course_url.strip()] if settings.course_url.strip() else []


def _saved_courses(storage: Storage, course_url: str | None) -> list[CourseRecord]:
    if course_url:
        record = storage.get_course(course_url) or storage.get_course(course_url.rstrip("/"))
        return [record] if record else []
    return storage.list_courses()


def print_saved_progress(settings: Settings, console: Console, course_url: str | None = None) -> int:
    storage = Storage(settings.database_path)
    try:
        courses = _saved_courses(storage, course_url or settings.course_url or None)
        if not courses:
            console.print(
                Panel(
                    Text("No saved course progress was found.", style="yellow"),
                    title="COURSE PROGRESS",
                    border_style="yellow",
                )
            )
            return 0

        table = Table(title="Saved Course Progress", header_style="bold cyan")
        table.add_column("Course", style="bold", min_width=18, no_wrap=True)
        table.add_column("Completed", justify="right", style="green")
        table.add_column("Total", justify="right")
        table.add_column("Remaining", justify="right", style="yellow")
        table.add_column("Failed", justify="right", style="red")
        table.add_column("Locked", justify="right")
        table.add_column("Status")
        table.add_column("Speed", justify="right")
        table.add_column("Last run", style="dim", no_wrap=True)

        for course in courses:
            stats = storage.stats(course.id)
            table.add_row(
                course.title or course.url,
                str(stats.completed),
                str(stats.total),
                str(stats.remaining),
                str(stats.failed),
                str(stats.locked),
                course.status.value,
                f"{course.playback_speed:.2f}x",
                course.last_run_at.strftime("%Y-%m-%d %H:%M") if course.last_run_at else "—",
            )
        console.print(table)

        if len(courses) == 1:
            record = courses[0]
            lessons = storage.get_lessons(record.id)
            if lessons:
                detail = Table(title="Lessons", header_style="bold cyan", show_lines=False)
                detail.add_column("#", justify="right", style="dim")
                detail.add_column("Lesson")
                detail.add_column("Section", style="dim")
                detail.add_column("Status")
                detail.add_column("Watched", justify="right")
                styles = {
                    LessonStatus.COMPLETED: "green",
                    LessonStatus.FAILED: "red",
                    LessonStatus.SKIPPED: "yellow",
                    LessonStatus.LOCKED: "bright_black",
                    LessonStatus.IN_PROGRESS: "cyan",
                }
                for lesson in lessons:
                    detail.add_row(
                        str(lesson.position + 1),
                        lesson.title,
                        lesson.section or "—",
                        Text(lesson.status.value, style=styles.get(lesson.status, "white")),
                        _fmt_seconds(lesson.watched_seconds),
                    )
                console.print(detail)
        console.print(Text(f"Database: {settings.database_path}", style="dim"))
        return 0
    finally:
        storage.close()


def reset_course_progress(settings: Settings, console: Console, course_url: str | None) -> int:
    url = (course_url or settings.course_url or "").strip()
    if not url:
        console.print(
            Text(
                "Refusing to reset: pass the course URL with --course-url so the "
                "correct progress record is chosen.",
                style="bold red",
            )
        )
        return 2
    storage = Storage(settings.database_path)
    try:
        record = storage.get_course(url) or storage.get_course(url.rstrip("/"))
        if record is None:
            console.print(Text(f"No saved progress found for {url}", style="yellow"))
            return 1
        storage.reset_course_progress(record.id)
        console.print(
            Panel(
                Text(
                    f"Cleared local progress for {record.title or record.url}. "
                    "Nothing was changed on LinkedIn Learning.",
                    style="green",
                ),
                title="PROGRESS RESET",
                border_style="green",
            )
        )
        return 0
    finally:
        storage.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkedin-learning-automation",
        description=(
            "Watch LinkedIn Learning lessons in a visible Chromium window at a "
            "chosen speed, track real completion, and get MCQ suggestions. "
            "Assessments are always submitted manually."
        ),
    )
    parser.add_argument("--config", type=Path, help="Optional JSON config file.")
    parser.add_argument("--course-url", help="LinkedIn Learning course URL.")
    parser.add_argument(
        "--courses-file",
        type=Path,
        help="CSV or text file with one course URL per row/line (batch mode).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        help="Playback speed for new playback (0.25-4.0); can be changed with the speed command.",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run Chromium headless (not recommended; manual login needs a window).",
    )
    parser.add_argument("--profile", type=Path, help="Persistent Chromium profile directory.")
    parser.add_argument("--database", type=Path, help="SQLite progress database path.")
    parser.add_argument("--log-level", choices=LOG_LEVELS, help="Logging level.")
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the resolved configuration and exit.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show saved progress without opening a browser, then exit.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear local progress for --course-url (does not change LinkedIn).",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the live dashboard (plain log output; useful for CI).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict[str, Any] = {}
    if args.course_url:
        overrides["course_url"] = args.course_url
    if args.courses_file is not None:
        overrides["courses_file"] = args.courses_file
    if args.speed is not None:
        overrides["playback_speed"] = args.speed
    if args.headless is not None:
        overrides["headless"] = args.headless
    if args.profile is not None:
        overrides["browser_profile"] = args.profile
    if args.database is not None:
        overrides["database_path"] = args.database
    if args.log_level is not None:
        overrides["log_level"] = args.log_level

    console = Console()
    try:
        settings = Settings.load(config_file=args.config, overrides=overrides)
    except (ValidationError, ValueError, FileNotFoundError) as exc:
        console.print(Panel(Text(str(exc), style="red"), title="CONFIGURATION ERROR", border_style="red"))
        return 2

    if args.show_config:
        safe = settings.public_dict()
        console.print_json(data=safe)
        return 0

    if args.progress:
        return print_saved_progress(settings, console, args.course_url)

    if args.reset:
        return reset_course_progress(settings, console, args.course_url)

    if settings.courses_file is not None and settings.course_url:
        console.print(
            Text(
                "Using --courses-file; --course-url / COURSE_URL is ignored for this run.",
                style="yellow",
            )
        )

    try:
        course_urls = resolve_course_urls(settings)
    except CourseListError as exc:
        console.print(
            Panel(Text(str(exc), style="red"), title="COURSE LIST ERROR", border_style="red")
        )
        return 2

    if not course_urls:
        console.print(
            Panel(
                Text(
                    "No COURSE_URL is configured.\n"
                    "Set it in .env / config.json, pass --course-url, "
                    "or pass --courses-file with a CSV/text list.",
                    style="yellow",
                ),
                title="COURSE URL REQUIRED",
                border_style="yellow",
            )
        )
        return 2

    settings.ensure_directories()
    configure_logging(
        log_dir=settings.log_dir,
        log_level=settings.log_level,
        log_json=settings.log_json,
        console=args.no_dashboard,
    )

    runner = CourseRunner(
        settings,
        console=console,
        live=not args.no_dashboard,
        course_urls=course_urls,
    )
    try:
        status = asyncio.run(runner.run())
    except KeyboardInterrupt:  # pragma: no cover - terminal-specific
        console.print(Text("Interrupted.", style="yellow"))
        return 130
    return 1 if status is ExecutionStatus.ERROR else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
