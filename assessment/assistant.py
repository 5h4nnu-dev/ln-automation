"""Local, explainable answer suggestions for multiple-choice questions.

Design constraints:

* This is a transparent keyword/context heuristic — no network calls, no LLM,
  no scraping of answer keys.
* It only returns a suggestion. It has no browser access and cannot select or
  submit an answer.
* Low-confidence cases are reported honestly instead of inventing certainty.

The user always reviews the question and submits the answer manually in the
browser, preserving the assessment's integrity.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, Field

from assessment.extractor import AnswerOption, Question

STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those of to in on at by for from with
    without about into over under above below is are was were be been being am do does did
    doing have has had having it its it's as which what who whom whose when where why how
    commonly used use uses using following option options answer question select choose
    most more less best correct incorrect true false not no yes can could should would will
    may might must shall you your we our they their he she his her them one two three
    """.split()
)

_NEGATIONS = {"not", "except", "false", "incorrect", "never", "least", "without", "invalid"}
_ALL_OF = {"all of the above", "all the above", "all of these", "both a and b"}
_NONE_OF = {"none of the above", "neither of the above", "none of these", "no answer"}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#._-]*")

DISCLAIMER = (
    "Heuristic suggestion only. It is not an authoritative answer and nothing is "
    "submitted automatically — verify against the lesson and submit manually."
)


class Confidence(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Suggestion(BaseModel):
    option_key: str | None = None
    option_text: str | None = None
    explanation: str
    confidence: Confidence = Confidence.LOW
    matched_question_terms: list[str] = Field(default_factory=list)
    matched_context_terms: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER
    notes: list[str] = Field(default_factory=list)

    @property
    def confident(self) -> bool:
        return self.option_key is not None and self.confidence in {Confidence.HIGH, Confidence.MEDIUM}


class _OptionScore:
    def __init__(self, option: AnswerOption) -> None:
        self.option = option
        self.score = 0.0
        self.question_terms: list[str] = []
        self.context_terms: list[str] = []
        self.special: str | None = None


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN.findall((text or "").lower()):
        token = match.strip("._-")
        if len(token) < 2 or token in STOPWORDS:
            continue
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.append(token)
    return tokens


def _term_list(terms: set[str], limit: int = 5) -> list[str]:
    return sorted(terms)[:limit]


class AssessmentAssistant:
    """Produces an explained suggestion; never interacts with the page."""

    def suggest(self, question: Question, context: Iterable[str] = ()) -> Suggestion:
        context_text = " ".join(part for part in context if part)
        question_tokens = set(tokenize(question.prompt))
        context_tokens = set(tokenize(context_text)) - question_tokens
        prompt_lower = question.prompt.lower().strip()
        raw_words = set(re.findall(r"[a-z']+", prompt_lower))
        negation = bool(raw_words & _NEGATIONS)

        scored: list[_OptionScore] = []
        for option in question.options:
            item = _OptionScore(option)
            option_tokens = set(tokenize(option.text))
            q_hits = question_tokens & option_tokens
            c_hits = context_tokens & option_tokens
            item.question_terms = _term_list(q_hits)
            item.context_terms = _term_list(c_hits)
            item.score = len(q_hits) + 0.35 * len(c_hits)

            normalized = option.normalized
            if normalized and len(normalized) >= 3 and normalized in prompt_lower:
                item.score += 1.5
            if prompt_lower and len(prompt_lower) >= 8 and prompt_lower in normalized:
                item.score += 0.5

            if normalized in _ALL_OF:
                item.special = "all"
            elif normalized in _NONE_OF:
                item.special = "none"
            scored.append(item)

        any_other_signal = any(
            item.question_terms or item.context_terms
            for item in scored
            if item.special is None
        )
        for item in scored:
            if item.special == "all" and any_other_signal:
                item.score += 1.25
            elif item.special == "none" and not any_other_signal:
                item.score += 1.0
            elif item.special == "none" and any_other_signal:
                item.score -= 1.0

        scored.sort(key=lambda item: item.score, reverse=True)
        top = scored[0]
        second = scored[1] if len(scored) > 1 else None
        margin = top.score - (second.score if second else 0.0)
        notes: list[str] = []

        if top.score <= 0.0 or not (top.question_terms or top.context_terms or top.special):
            explanation = (
                "No option has a meaningful term overlap with the question or the lesson "
                "context, so no confident answer can be suggested. Review the lesson "
                "material and choose manually."
            )
            return Suggestion(explanation=explanation, confidence=Confidence.LOW, notes=notes)

        if top.special == "all":
            notes.append("'All of the above' fits because several other options match the question terms.")
        elif top.special == "none":
            notes.append("'None of the above' is favoured because no other option matches the question terms.")

        if question.kind == "multi_select":
            notes.append("This question may allow multiple selections; only one option is suggested.")

        if second is not None and abs(top.score - second.score) < 0.25:
            confidence = Confidence.LOW
            notes.append(
                f"Options {top.option.key} and {second.option.key} fit equally well, so the signal is ambiguous."
            )
        elif margin >= 1.25 and top.score >= 1.5:
            confidence = Confidence.HIGH
        elif margin >= 0.5 and top.score >= 0.5:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW
            notes.append("The keyword signal is weak; treat this as a guess rather than a fact.")

        if negation:
            notes.append("The question contains a negation ('not'/'except'/...), which this heuristic does not invert.")
            if confidence is Confidence.HIGH:
                confidence = Confidence.MEDIUM
            elif confidence is Confidence.MEDIUM:
                confidence = Confidence.LOW

        if question.kind == "multi_select" and confidence is Confidence.HIGH:
            confidence = Confidence.MEDIUM

        reasons: list[str] = []
        if top.question_terms:
            reasons.append("shares question terms: " + ", ".join(top.question_terms))
        if top.context_terms:
            reasons.append("matches lesson context: " + ", ".join(top.context_terms))
        if top.special == "all":
            reasons.append("aggregates the other matched options")
        if top.special == "none":
            reasons.append("no other option matches the question")

        explanation = f"Option {top.option.key} ({top.option.text}) is suggested because it "
        explanation += "; ".join(reasons) if reasons else "has the strongest term overlap"
        explanation += "."
        if confidence is Confidence.LOW:
            explanation += " Confidence is low; please verify in the lesson before answering."
        elif confidence is Confidence.MEDIUM:
            explanation += " Verify this against the lesson material."
        else:
            explanation += " The match is distinctly stronger than the alternatives."

        return Suggestion(
            option_key=top.option.key,
            option_text=top.option.text,
            explanation=explanation,
            confidence=confidence,
            matched_question_terms=top.question_terms,
            matched_context_terms=top.context_terms,
            notes=notes,
        )

    def suggest_many(
        self,
        questions: Iterable[Question],
        context: Iterable[str] = (),
    ) -> list[tuple[Question, Suggestion]]:
        context_list = list(context)
        return [(question, self.suggest(question, context_list)) for question in questions]
