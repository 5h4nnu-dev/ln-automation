"""Multiple-choice assessment detection and answer assistance.

Public API: read-only detection/extraction plus explained suggestions. Nothing
in this package selects or submits an answer.
"""

from assessment.assistant import AssessmentAssistant, Confidence, Suggestion
from assessment.detector import AssessmentDetector
from assessment.extractor import (
    AnswerOption,
    Question,
    extract_questions,
    normalize_question,
    parse_options_text,
)

__all__ = [
    "AnswerOption",
    "AssessmentAssistant",
    "AssessmentDetector",
    "Confidence",
    "Question",
    "Suggestion",
    "extract_questions",
    "normalize_question",
    "parse_options_text",
]
