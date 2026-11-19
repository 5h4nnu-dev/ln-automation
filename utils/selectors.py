"""Centralized DOM selectors.

LinkedIn Learning changes its markup regularly. Keeping every selector in one
place makes maintenance a data change instead of a code change. You can override
any group with a JSON file (``SELECTORS_PATH``) that maps field names to arrays
of CSS selectors, for example::

    {
      "toc_root": [".classroom-toc", "[data-test-id='course-toc']"],
      "toc_item": [".classroom-toc-item"]
    }

The browser automation uses the first selector that yields a usable element and
always combines multiple markers, so a cosmetic class rename does not break the
whole run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Selectors:
    # Course / curriculum
    course_title: tuple[str, ...] = (
        "h1",
        "[data-test-id='course-title']",
        ".classroom-course-title",
        ".course-title",
        "header h1",
    )
    toc_root: tuple[str, ...] = (
        ".classroom-toc",
        ".classroom-toc-list",
        "[data-test-id='course-toc']",
        "[data-test-id='toc']",
        "aside nav",
        "nav[aria-label*='ourse' i]",
        ".toc",
    )
    toc_section: tuple[str, ...] = (
        ".classroom-toc-section",
        "[data-test-id='toc-section']",
        ".toc-section",
        "section",
    )
    toc_section_title: tuple[str, ...] = (
        "[data-test-id='toc-section-title']",
        ".classroom-toc-section__title",
        "[class*='toc-section'][class*='title']",
        "h2",
        "h3",
    )
    toc_item: tuple[str, ...] = (
        ".classroom-toc-item",
        "[data-test-id='toc-item']",
        ".toc-item",
        "li",
    )
    toc_item_container: tuple[str, ...] = (
        "[data-test-id='toc-item']",
        ".classroom-toc-item",
        ".toc-item",
        "li",
    )
    toc_item_link: tuple[str, ...] = (
        "a[data-test-id='toc-item-link']",
        "a.classroom-toc-item__link",
        "a[href*='/learning/']",
        "a[href]",
    )
    toc_item_title: tuple[str, ...] = (
        ".classroom-toc-item__title",
        "[data-test-id='toc-item-title']",
        "[class*='toc-item'][class*='title']",
        "[class*='tocItem'][class*='title']",
    )
    toc_item_duration: tuple[str, ...] = (
        ".classroom-toc-item__duration",
        "[data-test-id='duration']",
        "[class*='duration']",
        "time",
    )
    toc_completed_markers: tuple[str, ...] = (
        "[data-test-id='completed-icon']",
        "[data-test-id='toc-item-completed']",
        "[class*='completed-icon']",
        "[class*='toc-item'][class*='completed']",
        "[class*='tocItem'][class*='completed']",
        "svg[class*='check']",
        "[aria-label*='ompleted' i]",
        "[role='checkbox'][aria-checked='true']",
        "input[type='checkbox'][checked]",
    )
    toc_locked_markers: tuple[str, ...] = (
        "[data-test-id='locked-icon']",
        "[class*='lock-icon']",
        "[class*='toc-item'][class*='locked']",
        "[class*='tocItem'][class*='locked']",
        "[aria-label*='ocked' i]",
        "[aria-disabled='true']",
    )
    toc_active_markers: tuple[str, ...] = (
        "[aria-current='page']",
        "[aria-current='true']",
        "[class*='active']",
        "[data-test-id*='active']",
    )
    # Video player
    video: tuple[str, ...] = (
        "video.vjs-tech",
        "video[class*='video']",
        "video",
    )
    play_button: tuple[str, ...] = (
        "[data-test-id='play-button']",
        "button[aria-label*='Play' i]",
        ".vjs-big-play-button",
        "[class*='play-button']",
        "[class*='playButton']",
    )
    next_button: tuple[str, ...] = (
        "[data-test-id='next-button']",
        "button[aria-label*='Next' i]",
        "a[aria-label*='Next' i]",
        "[class*='next-button']",
        "[class*='nextButton']",
    )
    # Authentication
    auth_login_form: tuple[str, ...] = (
        "form[action*='login']",
        "input[name='session_key']",
        "input[name='session_password']",
        "[data-test-id='login-form']",
        "#login-form",
    )
    auth_signin_markers: tuple[str, ...] = (
        "a[href*='/login']",
        "[data-test-id='sign-in']",
        "button[data-tracking-control-name*='signin' i]",
    )
    auth_verified_markers: tuple[str, ...] = (
        "img.global-nav__me-photo",
        ".global-nav__me-photo",
        "[data-test-id='nav-settings']",
        "button.global-nav__primary-link",
        ".global-nav__me",
    )
    auth_login_url_fragments: tuple[str, ...] = (
        "/login",
        "/authwall",
        "/checkpoint",
        "/uas/login",
        "/signup",
    )
    # Assessments
    assessment_root: tuple[str, ...] = (
        "[data-test-id*='quiz']",
        "[data-test-id*='assessment']",
        "[class*='quiz']",
        "[class*='assessment']",
        "[role='radiogroup']",
    )
    assessment_prompt: tuple[str, ...] = (
        "legend",
        "[data-test-id*='question']",
        "[class*='question']",
        "h2",
        "h3",
        "p",
    )
    assessment_option: tuple[str, ...] = (
        "label",
        "[role='radio']",
        "[role='option']",
        "input[type='radio']",
        "input[type='checkbox']",
    )
    assessment_option_text: tuple[str, ...] = (
        "[class*='option']",
        "[class*='answer']",
        "label",
        "span",
    )

    # ------------------------------------------------------------------ #
    def as_dict(self) -> dict[str, list[str]]:
        return {field.name: list(getattr(self, field.name)) for field in fields(self)}

    def as_js_dict(self) -> dict[str, list[str]]:
        """Selector groups keyed in camelCase for JavaScript configuration."""

        result: dict[str, list[str]] = {}
        for field in fields(self):
            head, *rest = field.name.split("_")
            key = head + "".join(part.capitalize() for part in rest)
            result[key] = list(getattr(self, field.name))
        return result

    def union(self, name: str) -> str:
        """Return a comma-separated selector usable in ``querySelector``."""

        return ", ".join(getattr(self, name))


def load_selectors(path: Path | str | None = None) -> Selectors:
    """Load default selectors, optionally overridden by a JSON file."""

    if path is None:
        return Selectors()

    data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Selector overrides must be a JSON object: {path}")

    known = {field.name for field in fields(Selectors)}
    overrides: dict[str, tuple[str, ...]] = {}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"Unknown selector group '{key}' in {path}")
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise ValueError(f"Selector group '{key}' must be a non-empty array of strings")
        overrides[key] = tuple(value)
    return Selectors(**overrides)
