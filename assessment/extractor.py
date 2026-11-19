"""Normalization of multiple-choice questions extracted from the page.

The browser-side detector is deliberately generic; this module turns its raw
payload into a stable question/option model. It is also usable with questions
pasted as plain text, so new course layouts only require detector tweaks.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

from pydantic import BaseModel, Field

_OPTION_PREFIX = re.compile(
    r"^\s*(?:\(([A-Ha-h])\)|([A-Ha-h])[.):\-]|(\d{1,2})[.):\-])\s*(.+)$",
    re.DOTALL,
)
_QUESTION_PREFIX = re.compile(r"^\s*(?:question\s*)?\d{1,2}\s*[.):\-]\s*", re.I)
_WHITESPACE = re.compile(r"\s+")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class AnswerOption(BaseModel):
    key: str
    text: str

    @property
    def normalized(self) -> str:
        return normalize_text(self.text)


class Question(BaseModel):
    prompt: str
    options: list[AnswerOption] = Field(default_factory=list)
    context: str | None = None
    source_url: str | None = None
    kind: str = "multiple_choice"
    number: int | None = None

    @property
    def signature(self) -> str:
        raw = self.prompt.lower() + "|" + "|".join(option.normalized for option in self.options)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def option(self, key: str) -> AnswerOption | None:
        key = key.strip().upper()
        return next((option for option in self.options if option.key.upper() == key), None)

    def option_by_text(self, text: str) -> AnswerOption | None:
        target = normalize_text(text)
        return next((option for option in self.options if option.normalized == target), None)

    def lettered_options(self) -> list[str]:
        return [f"{option.key}. {option.text}" for option in self.options]


def normalize_text(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip()).strip().lower()


def clean_prompt(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _WHITESPACE.sub(" ", text).strip()
    cleaned = _QUESTION_PREFIX.sub("", cleaned).strip()
    return cleaned


def strip_option_prefix(text: str) -> tuple[str | None, str]:
    """Return ``(explicit_key, text)`` for labels like ``B. HTTPS``."""

    value = _WHITESPACE.sub(" ", (text or "").strip())
    match = _OPTION_PREFIX.match(value)
    if not match:
        return None, value
    letter, single, number, remainder = match.groups()
    key = (letter or single or "").upper() or None
    if not key and number:
        index = int(number) - 1
        if 0 <= index < len(_LETTERS):
            key = _LETTERS[index]
    return key, remainder.strip()


def parse_options_text(blob: str) -> list[str]:
    """Split a single text blob with lettered/numbered lines into options."""

    if not blob:
        return []
    lines = [line.strip() for line in re.split(r"[\r\n]+", blob) if line.strip()]
    prefixed = [strip_option_prefix(line) for line in lines]
    if len([item for item in prefixed if item[0]]) >= 2:
        return [text for _, text in prefixed if text]
    bullets = [re.sub(r"^[•\-\*]\s*", "", line) for line in lines if re.match(r"^\s*[•\-\*]\s+\S", line)]
    return bullets if len(bullets) >= 2 else []


def _raw_option_values(raw_options: Iterable[Any]) -> list[str]:
    values: list[str] = []
    for raw in raw_options:
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, dict):
            text = str(raw.get("text") or raw.get("label") or raw.get("value") or "")
        else:
            text = str(raw or "")
        text = text.strip()
        if text:
            values.append(text)
    return values


def normalize_question(
    raw: dict[str, Any],
    *,
    source_url: str | None = None,
    number: int | None = None,
) -> Question | None:
    """Validate and normalize one raw detector payload."""

    prompt = clean_prompt(str(raw.get("prompt") or ""))
    if len(prompt) < 5 or len(prompt) > 2000:
        return None

    option_texts = _raw_option_values(raw.get("options") or [])
    if len(option_texts) == 1:
        expanded = parse_options_text(option_texts[0])
        if len(expanded) >= 2:
            option_texts = expanded

    cleaned: list[tuple[str | None, str]] = []
    for value in option_texts:
        key, text = strip_option_prefix(value)
        if not text or text.lower() == prompt.lower():
            continue
        if text in [existing for _, existing in cleaned]:
            continue
        cleaned.append((key, text))

    if len(cleaned) < 2 or len(cleaned) > 10:
        return None

    options = [
        AnswerOption(key=_LETTERS[index] if index < len(_LETTERS) else str(index + 1), text=text)
        for index, (_, text) in enumerate(cleaned)
    ]

    return Question(
        prompt=prompt,
        options=options,
        context=(str(raw.get("context") or "").strip() or None),
        source_url=str(raw.get("source_url") or source_url or "") or None,
        kind=str(raw.get("kind") or "multiple_choice"),
        number=number,
    )


def extract_questions(raw_payload: Any, *, source_url: str | None = None) -> list[Question]:
    """Normalize a detector payload into unique questions (DOM order)."""

    if isinstance(raw_payload, dict):
        items = raw_payload.get("questions") or []
        url = raw_payload.get("url") or source_url
    elif isinstance(raw_payload, list):
        items = raw_payload
        url = source_url
    else:
        return []

    questions: list[Question] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        question = normalize_question(item, source_url=url, number=len(questions) + 1)
        if question is None or question.signature in seen:
            continue
        seen.add(question.signature)
        questions.append(question)
    return questions
