"""Tests for CSV/text course list input."""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.course_list import read_course_urls
from utils.exceptions import CourseListError


def test_read_text_file_ignores_comments_bullets_and_duplicates(tmp_path: Path) -> None:
    course_file = tmp_path / "courses.txt"
    course_file.write_text(
        """# one course per line
https://www.linkedin.com/learning/python

- https://www.linkedin.com/learning/sql  # optional note
* https://www.linkedin.com/learning/git
https://www.linkedin.com/learning/python/
""",
        encoding="utf-8",
    )

    assert read_course_urls(course_file) == [
        "https://www.linkedin.com/learning/python",
        "https://www.linkedin.com/learning/sql",
        "https://www.linkedin.com/learning/git",
    ]


def test_read_csv_uses_first_url_cell_and_skips_header(tmp_path: Path) -> None:
    course_file = tmp_path / "courses.csv"
    course_file.write_text(
        """course_url,title,note
https://www.linkedin.com/learning/python,Python,"start here"
notes only,No URL here,ignored
https://www.linkedin.com/learning/sql,SQL,next
""",
        encoding="utf-8",
    )

    with pytest.raises(CourseListError, match="no http"):
        read_course_urls(course_file)

    # A row without a URL is an error; a header using "Course" is still skipped.
    course_file.write_text(
        """Course,Title
https://www.linkedin.com/learning/python,Python
https://www.linkedin.com/learning/sql,SQL
""",
        encoding="utf-8",
    )
    assert read_course_urls(course_file) == [
        "https://www.linkedin.com/learning/python",
        "https://www.linkedin.com/learning/sql",
    ]


def test_invalid_scheme_reports_line(tmp_path: Path) -> None:
    course_file = tmp_path / "courses.txt"
    course_file.write_text(
        "https://www.linkedin.com/learning/ok\nftp://example.com/course\n",
        encoding="utf-8",
    )

    with pytest.raises(CourseListError, match="courses.txt:2"):
        read_course_urls(course_file)


def test_missing_and_empty_files_are_errors(tmp_path: Path) -> None:
    with pytest.raises(CourseListError, match="not found"):
        read_course_urls(tmp_path / "missing.txt")

    empty = tmp_path / "empty.csv"
    empty.write_text("url\n", encoding="utf-8")
    with pytest.raises(CourseListError, match="No course URLs"):
        read_course_urls(empty)
