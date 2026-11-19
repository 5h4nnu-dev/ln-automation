"""Read course URLs from a CSV or plain-text file.

This module is deliberately read-only and dependency-free. It only parses input:

* ``.txt``: one URL per line; blank lines, ``#`` comments, bullets and trailing
  ``#`` notes are ignored.
* ``.csv``: a URL may be in any column; the first URL-like cell in each row wins.
  A header row using a common name such as ``url``, ``course_url`` or ``link``
  is ignored automatically.

The first URL-like value per row/line is used so a CSV can also contain course
titles, notes or progress columns without a rigid schema.
"""

from __future__ import annotations

import csv
from pathlib import Path

from config import is_supported_url
from utils.exceptions import CourseListError

CSV_HEADER_HINTS = {
    "url",
    "course_url",
    "course url",
    "course-url",
    "link",
    "course link",
    "course",
}
BULLETS = ("- ", "* ", "• ")


def _clean(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _strip_inline_comment(value: str) -> str:
    """Remove a trailing ``# note`` while preserving URL fragments without spaces."""

    marker = " #"
    if marker in value:
        return value.split(marker, 1)[0].strip()
    return value


def _is_header(row: list[str]) -> bool:
    return any(_clean(cell).lower() in CSV_HEADER_HINTS for cell in row)


def _first_url(row: list[str]) -> str | None:
    for cell in row:
        candidate = _clean(cell)
        if is_supported_url(candidate):
            return candidate
    return None


def _text_rows(path: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw in enumerate(handle, start=1):
            value = _strip_inline_comment(raw.strip())
            for bullet in BULLETS:
                if value.startswith(bullet):
                    value = value[len(bullet) :].strip()
                    break
            rows.append((line_no, value))
    return rows


def _csv_rows(path: Path) -> list[tuple[int, list[str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [(line_no, row) for line_no, row in enumerate(csv.reader(handle), start=1)]


def read_course_urls(path: Path | str) -> list[str]:
    """Return validated, de-duplicated course URLs from ``path``.

    Raises :class:`CourseListError` with file/line context for malformed input.
    """

    course_file = Path(path).expanduser()
    if not course_file.is_file():
        raise CourseListError(f"Course list file not found: {course_file}")

    is_csv = course_file.suffix.lower() == ".csv"
    candidates: list[tuple[int, str]] = []

    try:
        if is_csv:
            for line_no, row in _csv_rows(course_file):
                if not any(_clean(cell) for cell in row):
                    continue
                if line_no == 1 and _is_header(row):
                    continue
                if all(_clean(cell).startswith("#") for cell in row if _clean(cell)):
                    continue
                url = _first_url(row)
                if url is None:
                    raise CourseListError(
                        f"{course_file}:{line_no}: no http://, https:// or file:// URL found in row"
                    )
                candidates.append((line_no, url))
        else:
            for line_no, value in _text_rows(course_file):
                if not value or value.startswith("#"):
                    continue
                if not is_supported_url(value):
                    raise CourseListError(
                        f"{course_file}:{line_no}: expected a course URL, got {value!r}"
                    )
                candidates.append((line_no, value))
    except CourseListError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CourseListError(f"Could not read course list {course_file}: {exc}") from exc

    urls: list[str] = []
    seen: set[str] = set()
    for _line_no, url in candidates:
        normalized = url.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)

    if not urls:
        raise CourseListError(f"No course URLs found in {course_file}")
    return urls
