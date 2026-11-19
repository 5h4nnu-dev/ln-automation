"""Course curriculum discovery.

LinkedIn Learning renders the table of contents in a sidebar. The exact markup
differs between layouts, so extraction combines several candidate selectors and
falls back to lesson links inside the sidebar. Everything is normalized into a
small, stable model that the navigator and database can consume.

No completion state is invented: a lesson is only marked completed when the
platform's own DOM advertises it (checkmark / completed label).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field, computed_field

from utils.exceptions import (
    CurriculumError,
    LessonAccessError,
    ShutdownRequested,
)
from utils.logger import get_logger
from utils.selectors import Selectors

logger = get_logger(__name__)

_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


class Lesson(BaseModel):
    position: int
    title: str
    url: str
    section: str | None = None
    duration_seconds: float | None = None
    completed: bool = False
    locked: bool = False
    active: bool = False
    index_in_section: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key(self) -> str:
        return make_lesson_key(self.url, self.title, self.position)

    @property
    def display_title(self) -> str:
        if self.section:
            return f"{self.section} — {self.title}"
        return self.title


class Section(BaseModel):
    title: str | None = None
    lessons: list[Lesson] = Field(default_factory=list)


class Course(BaseModel):
    url: str
    title: str
    sections: list[Section] = Field(default_factory=list)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lessons(self) -> list[Lesson]:
        return [lesson for section in self.sections for lesson in section.lessons]

    @property
    def total_lessons(self) -> int:
        return len(self.lessons)

    def by_key(self) -> dict[str, Lesson]:
        return {lesson.key: lesson for lesson in self.lessons}

    def find(self, key: str) -> Lesson | None:
        return self.by_key().get(key)

    def completed_keys(self) -> list[str]:
        return [lesson.key for lesson in self.lessons if lesson.completed]

    def locked_keys(self) -> list[str]:
        return [lesson.key for lesson in self.lessons if lesson.locked]


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def normalize_url(url: str, base: str = "") -> str:
    absolute = urljoin(base, url) if base else url
    parsed = urlparse(absolute)
    if parsed.scheme and parsed.netloc:
        return parsed._replace(query="", fragment="").geturl().rstrip("/")
    return absolute.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def make_lesson_key(url: str, title: str, position: int) -> str:
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    if path and path != "/":
        return path
    slug = _SLUG_CLEAN.sub("-", title.lower()).strip("-") or "lesson"
    return f"/{slug}-{position}"


_DURATION_PATTERNS = (
    (re.compile(r"(\d+)\s*(?:h|hr|hrs|hour|hours)\b", re.I), 3600.0),
    (re.compile(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", re.I), 60.0),
    (re.compile(r"(\d+)\s*(?:s|sec|secs|second|seconds)\b", re.I), 1.0),
)
_CLOCK_PATTERN = re.compile(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b")
_TRAILING_DURATION = re.compile(
    r"\s+(?:\d{1,2}:\d{2}(?::\d{2})?|\d+\s*(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)\b.*)$",
    re.I,
)


def parse_duration(text: str | None) -> float | None:
    """Parse common duration labels (``2m 15s``, ``1:05``, ``1h 2m``)."""

    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None

    clock = _CLOCK_PATTERN.search(cleaned)
    if clock:
        first, second, third = clock.groups()
        if third is not None:
            return float(int(first) * 3600 + int(second) * 60 + int(third))
        return float(int(first) * 60 + int(second))

    total = 0.0
    matched = False
    for pattern, multiplier in _DURATION_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            total += int(match.group(1)) * multiplier
            matched = True
    return total if matched else None


def clean_title(title: str | None) -> str:
    if not title:
        return ""
    return _TRAILING_DURATION.sub("", " ".join(title.split())).strip()


def is_lesson_url(url: str) -> bool:
    path = urlparse(url).path
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "learning":
        return False
    if any(part in {"login", "signup", "authwall", "checkpoint"} for part in parts):
        return False
    return True


def select_next_lesson(
    course: Course,
    *,
    completed: set[str] | None = None,
    unavailable: set[str] | None = None,
    preferred_key: str | None = None,
) -> Lesson | None:
    """Pick the next actionable lesson without ever skipping completed/locked ones."""

    completed = completed or set()
    unavailable = unavailable or set()
    by_key = course.by_key()

    def eligible(lesson: Lesson) -> bool:
        return (
            lesson.key not in completed
            and lesson.key not in unavailable
            and not lesson.locked
        )

    if preferred_key and preferred_key in by_key and eligible(by_key[preferred_key]):
        return by_key[preferred_key]

    for lesson in course.lessons:
        if eligible(lesson):
            return lesson
    return None


# --------------------------------------------------------------------------- #
# DOM extraction
# --------------------------------------------------------------------------- #
_EXTRACT_JS = (
    """
(cfg) => {
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();
  const classText = (el) => {
    if (!el) return '';
    if (typeof el.className === 'string') return el.className;
    return (el.className && el.className.baseVal) || '';
  };
  const unique = (nodes) => Array.from(new Set(nodes));
  const queryAll = (root, selectors) => {
    const out = [];
    for (const selector of selectors) {
      try { out.push(...root.querySelectorAll(selector)); } catch (e) {}
    }
    return unique(out);
  };
  const firstOf = (root, selectors) => {
    for (const selector of selectors) {
      try {
        const el = root.querySelector(selector);
        if (el) return el;
      } catch (e) {}
    }
    return null;
  };
  const normPath = (href) => {
    if (!href) return '';
    try {
      return new URL(href, location.href).pathname.replace(/\\/+$/, '');
    } catch (e) {
      return String(href).split('?')[0].replace(/\\/+$/, '');
    }
  };
  const parts = location.pathname.split('/').filter(Boolean);
  const coursePrefix = (parts.length >= 2 && parts[0] === 'learning')
    ? '/' + parts.slice(0, 2).join('/') + '/'
    : '/learning/';
  const isLessonHref = (href) => {
    const path = normPath(href);
    if (!path) return false;
    // Only links belonging to the current course are considered, which keeps
    // "related courses" and global navigation out of the curriculum.
    if (!path.startsWith(coursePrefix)) return false;
    const segs = path.split('/').filter(Boolean);
    return segs.length >= 3 && segs[0] === 'learning';
  };
  const isCompleted = (item, link) => {
    if (firstOf(item, cfg.tocCompletedMarkers)) return true;
    if (firstOf(link, cfg.tocCompletedMarkers)) return true;
    const aria = ((item.getAttribute && item.getAttribute('aria-label')) || '') + ' ' +
                 ((link.getAttribute && link.getAttribute('aria-label')) || '');
    if (/completed/i.test(aria)) return true;
    const cls = classText(item) + ' ' + classText(link);
    if (/(^|[-_\\s])(completed|is-complete|complete)([-_\\s]|$)/i.test(cls)) return true;
    const cb = item.querySelector('[role="checkbox"][aria-checked="true"], input[type="checkbox"]:checked');
    return !!cb;
  };
  const isLocked = (item, link) => {
    if (firstOf(item, cfg.tocLockedMarkers) || firstOf(link, cfg.tocLockedMarkers)) return true;
    if (item.getAttribute('aria-disabled') === 'true' || link.getAttribute('aria-disabled') === 'true') return true;
    const cls = classText(item) + ' ' + classText(link);
    return /(^|[-_\\s])(locked|disabled|unavailable)([-_\\s]|$)/i.test(cls);
  };
  const isActive = (item, link) => {
    if (firstOf(item, cfg.tocActiveMarkers) || firstOf(link, cfg.tocActiveMarkers)) return true;
    if (link.getAttribute('aria-current') || item.getAttribute('aria-current')) return true;
    const cls = classText(item) + ' ' + classText(link);
    return /(^|[-_\\s])(active|current|playing)([-_\\s]|$)/i.test(cls);
  };
  const itemFor = (link) => {
    for (const selector of cfg.tocItemContainer) {
      let candidate = null;
      try { candidate = link.closest(selector); } catch (e) {}
      if (candidate) return candidate;
    }
    return link.parentElement || link;
  };
  const durationOf = (item) => {
    for (const selector of cfg.tocItemDuration) {
      try {
        const el = item.querySelector(selector);
        const value = text(el);
        if (value) return value;
      } catch (e) {}
    }
    return '';
  };
  const titleOf = (item, link) => {
    for (const selector of cfg.tocItemTitle) {
      try {
        const el = item.querySelector(selector);
        const value = text(el);
        if (value) return value;
      } catch (e) {}
    }
    return text(link);
  };

  let root = null;
  for (const selector of cfg.tocRoot) {
    try {
      const candidate = document.querySelector(selector);
      if (candidate && candidate.querySelector('a[href]')) { root = candidate; break; }
    } catch (e) {}
  }
  let fallback = false;
  if (!root) {
    root = document.body;
    fallback = true;
  }

  const seen = new Set();
  const sections = [];
  const hasLessonAnchor = (section) =>
    queryAll(section, ['a[href]']).some((a) => isLessonHref(a.getAttribute('href') || a.href));
  const sectionEls = queryAll(root, cfg.tocSection).filter(hasLessonAnchor);
  // Keep only the innermost sections so lessons are not collected under an
  // outer wrapper's title as well.
  const usableSections = sectionEls.filter(
    (section) => !sectionEls.some((other) => other !== section && section.contains(other))
  );

  const collectFromContainer = (container) => {
    const lessons = [];
    const anchors = queryAll(container, ['a[href]']).filter((a) => isLessonHref(a.getAttribute('href') || a.href));
    let indexInSection = 0;
    const seenLocal = new Set();
    for (const link of anchors) {
      const href = link.getAttribute('href') || link.href;
      const path = normPath(href);
      if (!path || seenLocal.has(path) || seen.has(path)) continue;
      seenLocal.add(path);
      seen.add(path);
      const item = itemFor(link);
      const durationText = durationOf(item);
      lessons.push({
        title: titleOf(item, link),
        href: href,
        duration_text: durationText,
        completed: isCompleted(item, link),
        locked: isLocked(item, link),
        active: isActive(item, link),
        index_in_section: indexInSection,
      });
      indexInSection += 1;
    }
    return lessons;
  };

  if (!fallback && usableSections.length > 0) {
    for (const sectionEl of usableSections) {
      const titleEl = firstOf(sectionEl, cfg.tocSectionTitle);
      const title = text(titleEl) || null;
      const lessons = collectFromContainer(sectionEl);
      if (lessons.length) sections.push({title, lessons});
    }
  }

  if (!sections.length) {
    const lessons = collectFromContainer(root);
    if (lessons.length) sections.push({title: null, lessons});
  }

  let title = '';
  for (const selector of cfg.courseTitle) {
    try {
      const el = document.querySelector(selector);
      const value = text(el);
      if (value) { title = value; break; }
    } catch (e) {}
  }

  return {
    title,
    document_title: document.title || '',
    url: location.href,
    fallback,
    sections,
  };
}
"""
)

_LESSON_STATUS_JS = (
    """
(payload) => {
  const cfg = payload.cfg;
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();
  const classText = (el) => {
    if (!el) return '';
    if (typeof el.className === 'string') return el.className;
    return (el.className && el.className.baseVal) || '';
  };
  const normPath = (href) => {
    if (!href) return '';
    try { return new URL(href, location.href).pathname.replace(/\\/+$/, ''); }
    catch (e) { return String(href).split('?')[0].replace(/\\/+$/, ''); }
  };
  const target = normPath(payload.href);
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  const prefix = target.slice(0, target.lastIndexOf('/') + 1);
  const targetSlug = target.split('/').filter(Boolean).pop() || '';
  let link = anchors.find((a) => normPath(a.getAttribute('href') || a.href) === target) || null;
  if (!link) {
    link = anchors.find((a) => {
      const path = normPath(a.getAttribute('href') || a.href);
      return path.startsWith(prefix) && path.split('/').filter(Boolean).pop() === targetSlug;
    }) || null;
  }
  if (!link) return {found: false, completed: false, locked: false, active: false};

  let item = link;
  for (const selector of cfg.tocItemContainer) {
    let candidate = null;
    try { candidate = link.closest(selector); } catch (e) {}
    if (candidate) { item = candidate; break; }
  }
  const firstOf = (root, selectors) => {
    for (const selector of selectors) {
      try { const el = root.querySelector(selector); if (el) return el; } catch (e) {}
    }
    return null;
  };
  const firstAnywhere = (selectors) => {
    for (const selector of selectors) {
      try { const el = document.querySelector(selector); if (el) return el; } catch (e) {}
    }
    return null;
  };

  const aria = ((item.getAttribute('aria-label')) || '') + ' ' + ((link.getAttribute('aria-label')) || '');
  const cls = classText(item) + ' ' + classText(link);
  const completed = !!(
    firstOf(item, cfg.tocCompletedMarkers) ||
    firstOf(link, cfg.tocCompletedMarkers) ||
    /completed/i.test(aria) ||
    /(^|[-_\\s])(completed|is-complete|complete)([-_\\s]|$)/i.test(cls) ||
    item.querySelector('[role="checkbox"][aria-checked="true"], input[type="checkbox"]:checked')
  );
  const locked = !!(
    firstOf(item, cfg.tocLockedMarkers) ||
    firstOf(link, cfg.tocLockedMarkers) ||
    item.getAttribute('aria-disabled') === 'true' ||
    link.getAttribute('aria-disabled') === 'true' ||
    /(^|[-_\\s])(locked|disabled|unavailable)([-_\\s]|$)/i.test(cls)
  );
  const active = !!(
    firstOf(item, cfg.tocActiveMarkers) ||
    firstOf(link, cfg.tocActiveMarkers) ||
    link.getAttribute('aria-current') ||
    item.getAttribute('aria-current') ||
    /(^|[-_\\s])(active|current|playing)([-_\\s]|$)/i.test(cls)
  );

  return {
    found: true,
    completed,
    locked,
    active,
    title: text(link),
    toc_present: !!firstAnywhere(cfg.tocRoot),
  };
}
"""
)


def parse_curriculum(
    raw: dict,
    *,
    page_url: str,
    page_title: str = "",
) -> Course:
    """Convert the browser extraction payload into the domain model."""

    if not isinstance(raw, dict):
        raise CurriculumError("The curriculum extractor returned an unexpected value.")

    sections: list[Section] = []
    seen: set[str] = set()
    position = 0
    accepted = 0

    for raw_section in raw.get("sections") or []:
        if not isinstance(raw_section, dict):
            continue
        section_title = clean_title(raw_section.get("title")) or None
        lessons: list[Lesson] = []
        for raw_lesson in raw_section.get("lessons") or []:
            if not isinstance(raw_lesson, dict):
                continue
            href = str(raw_lesson.get("href") or "").strip()
            title = clean_title(raw_lesson.get("title"))
            if not href or not title:
                continue
            url = normalize_url(href, page_url)
            key = make_lesson_key(url, title, position)
            if key in seen:
                continue
            seen.add(key)
            duration = parse_duration(raw_lesson.get("duration_text"))
            lessons.append(
                Lesson(
                    position=position,
                    title=title,
                    url=url,
                    section=section_title,
                    duration_seconds=duration,
                    completed=bool(raw_lesson.get("completed")),
                    locked=bool(raw_lesson.get("locked")),
                    active=bool(raw_lesson.get("active")),
                    index_in_section=raw_lesson.get("index_in_section"),
                )
            )
            position += 1
            accepted += 1
        if lessons:
            sections.append(Section(title=section_title, lessons=lessons))

    if accepted == 0:
        raise CurriculumError(
            "No lessons were found in the course curriculum.",
            hint=(
                "Open the course page in the browser and make sure the table of contents "
                "is visible. The layout may have changed; update utils/selectors.py."
            ),
        )

    resolved_title = clean_title(raw.get("title")) or clean_title(page_title) or "Untitled course"
    return Course(
        url=normalize_url(page_url),
        title=resolved_title,
        sections=sections,
    )


class CourseNavigator:
    """Page-level course operations: discovery, lesson opening, verification."""

    def __init__(self, page_provider, selectors: Selectors, settings) -> None:
        self._page_provider = page_provider
        self.selectors = selectors
        self.settings = settings

    @property
    def page(self):
        return self._page_provider()

    def _config(self) -> dict[str, object]:
        return self.selectors.as_js_dict()

    async def discover(self, page_url: str | None = None) -> Course:
        page = self.page
        url = page_url or page.url
        raw = await page.evaluate(_EXTRACT_JS, self._config())
        course = parse_curriculum(
            raw,
            page_url=url,
            page_title=(raw or {}).get("document_title", ""),
        )
        logger.info(
            "Course curriculum discovered",
            extra={
                "event": "curriculum_discovered",
                "course": course.title,
                "lessons": course.total_lessons,
                "sections": len(course.sections),
                "fallback": bool((raw or {}).get("fallback")),
            },
        )
        return course

    async def course_title(self) -> str:
        try:
            return str(await self.page.evaluate(
                """
                (selectors) => {
                  for (const selector of selectors) {
                    try {
                      const el = document.querySelector(selector);
                      const value = (el && el.innerText || '').trim();
                      if (value) return value;
                    } catch (e) {}
                  }
                  return document.title || '';
                }
                """,
                list(self.selectors.course_title),
            )).strip()
        except Exception:
            return ""

    async def lesson_status(self, lesson: Lesson):
        raw = await self.page.evaluate(
            _LESSON_STATUS_JS,
            {"cfg": self._config(), "href": lesson.url},
        )
        return raw or {"found": False, "completed": False, "locked": False, "active": False}

    async def active_lesson(self, course: Course) -> Lesson | None:
        """Match the active TOC entry, falling back to the URL path."""

        current = normalize_url(self.page.url)
        by_path = {normalize_url(lesson.url): lesson for lesson in course.lessons}
        if current in by_path:
            return by_path[current]

        if not await self._toc_present():
            return None

        for lesson in course.lessons:
            status = await self.lesson_status(lesson)
            if status.get("active"):
                return lesson
        return None

    async def _toc_present(self) -> bool:
        for selector in self.selectors.toc_root:
            try:
                if await self.page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def open_lesson(self, lesson: Lesson, *, course_url: str | None = None, retries: int = 1):
        """Navigate to a lesson by URL (never by fixed screen coordinates)."""

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await self.page.goto(lesson.url, wait_until="domcontentloaded", timeout=60_000)
                if response is not None and response.status >= 400:
                    raise LessonAccessError(
                        f"The lesson returned HTTP {response.status}.",
                        retryable=response.status >= 500,
                    )
                return self.page
            except LessonAccessError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(1.0 + attempt)

        if course_url:
            # Fallback: return to the curriculum and click the real TOC entry.
            try:
                await self.page.goto(course_url, wait_until="domcontentloaded", timeout=60_000)
                slug = lesson.key.rsplit("/", 1)[-1]
                locator = self.page.locator(f"a[href*='{slug}']").first
                if await locator.count() > 0:
                    await locator.click(timeout=10_000)
                    return self.page
            except Exception as exc:
                last_error = exc

        raise LessonAccessError(
            f"Could not open lesson '{lesson.title}': {last_error}",
            hint="Check that the lesson is still available in the course.",
            retryable=True,
        )

    async def wait_for_platform_completion(
        self,
        lesson: Lesson,
        *,
        timeout: float | None = None,
        course_url: str | None = None,
        should_stop=None,
    ) -> dict:
        """Wait until the platform itself shows the lesson as completed."""

        limit = float(timeout if timeout is not None else self.settings.completion_timeout)
        deadline = time.monotonic() + limit
        last: dict = {"found": False, "completed": False, "locked": False}
        toc_seen = False

        while time.monotonic() < deadline:
            if should_stop and should_stop():
                raise ShutdownRequested()
            try:
                last = await self.lesson_status(lesson)
            except Exception as exc:
                logger.debug("Completion check failed: %s", exc)
                last = {"found": False, "completed": False, "locked": False}
            toc_seen = toc_seen or bool(last.get("found"))
            if last.get("completed"):
                return {
                    "verified": True,
                    "found": True,
                    "completed": True,
                    "locked": bool(last.get("locked")),
                    "reason": None,
                }
            if last.get("locked"):
                return {
                    "verified": False,
                    "found": True,
                    "completed": False,
                    "locked": True,
                    "reason": "The platform marks this lesson as locked.",
                }
            await asyncio.sleep(max(1.0, self.settings.poll_interval))

        # Some layouts hide the TOC on the lesson page. Check the course page
        # once before giving up, but never fabricate completion.
        if not toc_seen and course_url and self.settings.verify_via_course_page:
            try:
                await self.page.goto(course_url, wait_until="domcontentloaded", timeout=60_000)
                await asyncio.sleep(1.0)
                status = await self.lesson_status(lesson)
                if status.get("completed"):
                    return {
                        "verified": True,
                        "found": True,
                        "completed": True,
                        "locked": bool(status.get("locked")),
                        "reason": None,
                    }
            except Exception as exc:
                logger.debug("Course-page completion check failed: %s", exc)

        return {
            "verified": False,
            "found": bool(last.get("found")),
            "completed": False,
            "locked": bool(last.get("locked")),
            "reason": (
                "The platform did not register completion within "
                f"{limit:.0f}s. The lesson was left as pending."
            ),
        }
