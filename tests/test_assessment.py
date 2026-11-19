"""Assessment extraction, detection and suggestion tests."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import assessment.assistant as assistant_module
import assessment.detector as detector_module
import assessment.extractor as extractor_module
from assessment.assistant import AssessmentAssistant, Confidence
from assessment.detector import AssessmentDetector
from assessment.extractor import (
    AnswerOption,
    Question,
    extract_questions,
    normalize_question,
    parse_options_text,
)
from config import Settings
from utils.selectors import Selectors

QUIZ_HTML = """
<!doctype html><html><head><title>Quiz</title></head><body>
<section>
  <h2>Secure Web</h2>
  <form>
    <fieldset>
      <legend>Which protocol is commonly used for secure web communication?</legend>
      <label><input type="radio" name="q1" value="http"> HTTP</label>
      <label><input type="radio" name="q1" value="https"> HTTPS</label>
      <label><input type="radio" name="q1" value="ftp"> FTP</label>
    </fieldset>
    <fieldset>
      <legend>Which keyword defines a function in Python?</legend>
      <label><input type="radio" name="q2" value="def"> def</label>
      <label><input type="radio" name="q2" value="func"> func</label>
      <label><input type="radio" name="q2" value="lambda"> lambda</label>
    </fieldset>
  </form>
</section>
</body></html>
"""

ROLE_QUIZ_HTML = """
<!doctype html><html><body>
<div class="quiz-question" data-test-id="quiz-question-1">
  <h3>What does HTML stand for?</h3>
  <div role="radiogroup" aria-label="Answers">
    <button role="radio">HyperText Markup Language</button>
    <button role="radio">Home Tool Markup Language</button>
    <button role="radio">Hyperlinks and Text Markup Language</button>
  </div>
</div>
</body></html>
"""

CONTAINER_QUIZ_HTML = """
<!doctype html><html><body>
<div class="quiz">
  <h2>Which of these is a Python data type?</h2>
  <ul>
    <li class="option-item">Integer</li>
    <li class="option-item">String</li>
    <li class="option-item">List</li>
  </ul>
</div>
</body></html>
"""

NOISE_HTML = """
<!doctype html><html><body>
<nav>
  <form class="filter-panel">
    <fieldset>
      <legend>Filter by language</legend>
      <label><input type="radio" name="lang"> English</label>
      <label><input type="radio" name="lang"> Spanish</label>
    </fieldset>
  </form>
</nav>
<main>
  <form action="/login"><input name="session_key" type="text"><input type="password"></form>
</main>
</body></html>
"""


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "headless": True,
        "assessment_enabled": True,
        "database_path": tmp_path / "progress.db",
        "log_dir": tmp_path / "logs",
        "browser_profile": tmp_path / "profile",
    }
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# Extraction / normalization
# --------------------------------------------------------------------------- #
def test_normalize_question_strips_prefixes_and_relabels() -> None:
    question = normalize_question(
        {
            "prompt": "Question 1: Which protocol is secure?",
            "options": ["A. HTTP", "(B) HTTPS", "C) FTP", "3. SMTP"],
            "context": "Security",
        },
        source_url="https://example.test/quiz",
    )
    assert question is not None
    assert question.prompt == "Which protocol is secure?"
    assert [option.key for option in question.options] == ["A", "B", "C", "D"]
    assert [option.text for option in question.options] == ["HTTP", "HTTPS", "FTP", "SMTP"]
    assert question.context == "Security"
    assert question.option("b") is not None
    assert question.option_by_text("ftp").key == "C"


def test_single_blob_options_are_split() -> None:
    question = normalize_question(
        {
            "prompt": "Pick one",
            "options": ["A. Alpha\nB. Beta\nC. Gamma"],
        }
    )
    assert question is not None
    assert [option.text for option in question.options] == ["Alpha", "Beta", "Gamma"]


def test_parse_options_text_bullets() -> None:
    assert parse_options_text("• Alpha\n• Beta\n• Gamma") == ["Alpha", "Beta", "Gamma"]
    assert parse_options_text("just one line") == []


@pytest.mark.parametrize(
    "raw",
    [
        {"prompt": "x", "options": ["A", "B"]},
        {"prompt": "Which one is right?", "options": ["Only one"]},
        {"prompt": "Which one is right?", "options": []},
        {"prompt": "", "options": ["A", "B"]},
    ],
)
def test_invalid_questions_are_rejected(raw: dict) -> None:
    assert normalize_question(raw) is None


def test_extract_questions_deduplicates() -> None:
    payload = {
        "url": "https://example.test/quiz",
        "questions": [
            {"prompt": "Which one is right?", "options": ["A", "B"]},
            {"prompt": "Which  one  is right?", "options": ["A", "B"]},
            {"prompt": "What does HTML stand for?", "options": ["HyperText", "Home"]},
        ],
    }
    questions = extract_questions(payload)
    assert len(questions) == 2
    assert [question.number for question in questions] == [1, 2]


# --------------------------------------------------------------------------- #
# Detector (real browser, read-only)
# --------------------------------------------------------------------------- #
async def _detect(page, site, tmp_path: Path, html: str):
    url = site.write("learning/test-course/quiz.html", html)
    await page.goto(url)
    detector = AssessmentDetector(lambda: page, Selectors(), _settings(tmp_path))
    return await detector.detect()


async def test_detects_native_radio_questions(page, site, tmp_path: Path) -> None:
    questions = await _detect(page, site, tmp_path, QUIZ_HTML)
    assert len(questions) == 2

    first = questions[0]
    assert first.prompt == "Which protocol is commonly used for secure web communication?"
    assert [option.text for option in first.options] == ["HTTP", "HTTPS", "FTP"]
    assert [option.key for option in first.options] == ["A", "B", "C"]
    assert first.context == "Secure Web"
    assert first.source_url and first.source_url.endswith("quiz.html")

    second = questions[1]
    assert second.prompt == "Which keyword defines a function in Python?"
    assert second.option_by_text("def") is not None


async def test_detects_aria_radio_question(page, site, tmp_path: Path) -> None:
    questions = await _detect(page, site, tmp_path, ROLE_QUIZ_HTML)
    assert len(questions) == 1
    question = questions[0]
    assert question.prompt == "What does HTML stand for?"
    assert [option.text for option in question.options] == [
        "HyperText Markup Language",
        "Home Tool Markup Language",
        "Hyperlinks and Text Markup Language",
    ]


async def test_detects_container_quiz(page, site, tmp_path: Path) -> None:
    questions = await _detect(page, site, tmp_path, CONTAINER_QUIZ_HTML)
    assert len(questions) == 1
    assert questions[0].prompt == "Which of these is a Python data type?"
    assert [option.text for option in questions[0].options] == ["Integer", "String", "List"]


async def test_ignores_filters_and_login_forms(page, site, tmp_path: Path) -> None:
    questions = await _detect(page, site, tmp_path, NOISE_HTML)
    assert questions == []


async def test_assessment_disabled(page, site, tmp_path: Path) -> None:
    url = site.write("learning/test-course/quiz.html", QUIZ_HTML)
    await page.goto(url)
    detector = AssessmentDetector(
        lambda: page,
        Selectors(),
        _settings(tmp_path, assessment_enabled=False),
    )
    assert await detector.detect() == []


async def test_wait_until_gone_sees_manual_submission(page, site, tmp_path: Path) -> None:
    url = site.write("learning/test-course/quiz.html", QUIZ_HTML)
    await page.goto(url)
    detector = AssessmentDetector(lambda: page, Selectors(), _settings(tmp_path))
    assert await detector.detect()

    # The test simulates the *user* answering in the browser; the tool itself
    # never clicks or submits.
    await page.evaluate("document.querySelectorAll('fieldset').forEach((el) => el.remove())")
    assert await detector.wait_until_gone(timeout=2, poll_interval=0.2) is True


# --------------------------------------------------------------------------- #
# Assistant
# --------------------------------------------------------------------------- #
def _question(prompt: str, options: list[str], kind: str = "multiple_choice") -> Question:
    return Question(
        prompt=prompt,
        options=[AnswerOption(key=chr(65 + index), text=text) for index, text in enumerate(options)],
        kind=kind,
    )


def test_assistant_suggests_when_terms_match_strongly() -> None:
    question = _question(
        "Which Python library provides dataframes and series for data analysis?",
        [
            "pandas dataframes series",
            "requests http client",
            "flask web framework",
        ],
    )
    suggestion = AssessmentAssistant().suggest(
        question,
        context=["pandas provides dataframes and series for data analysis."],
    )
    assert suggestion.option_key == "A"
    assert suggestion.option_text == "pandas dataframes series"
    assert suggestion.confidence is Confidence.HIGH
    assert suggestion.matched_question_terms
    assert "dataframes" in suggestion.explanation
    assert "submitted automatically" in suggestion.disclaimer


def test_assistant_is_honest_when_no_signal_exists() -> None:
    question = _question(
        "Which of the following is the best choice?",
        ["Alpha", "Beta", "Gamma"],
    )
    suggestion = AssessmentAssistant().suggest(question)
    assert suggestion.option_key is None
    assert suggestion.confidence is Confidence.LOW
    assert "No option has a meaningful term overlap" in suggestion.explanation


def test_assistant_flags_ambiguous_ties() -> None:
    question = _question(
        "Is the network device a router or a switch?",
        ["Router", "Switch"],
    )
    suggestion = AssessmentAssistant().suggest(question)
    assert suggestion.confidence is Confidence.LOW
    assert any("equally well" in note for note in suggestion.notes)


def test_assistant_handles_all_of_the_above() -> None:
    question = _question(
        "Which of these are Python data types?",
        ["int", "str", "All of the above"],
    )
    suggestion = AssessmentAssistant().suggest(question, context=["int str list"])
    assert suggestion.option_key == "C"
    assert suggestion.confidence in {Confidence.MEDIUM, Confidence.HIGH}
    assert any("All of the above" in note for note in suggestion.notes)


def test_assistant_warns_about_negation() -> None:
    question = _question(
        "Which of these is NOT a secure protocol?",
        ["HTTP", "HTTPS"],
    )
    suggestion = AssessmentAssistant().suggest(question, context=["HTTP protocol"])
    assert suggestion.confidence is Confidence.LOW
    assert any("negation" in note.lower() for note in suggestion.notes)


def test_assistant_uses_lesson_context() -> None:
    question = _question(
        "Which tool builds container images?",
        ["Docker", "Paint"],
    )
    suggestion = AssessmentAssistant().suggest(question, context=["Docker builds container images."])
    assert suggestion.option_key == "A"
    assert suggestion.matched_context_terms


def test_assistant_never_submits_or_clicks() -> None:
    forbidden = (".click(", ".submit(", "select_option(", ".press(", "page.evaluate(", "locator(")
    for module in (assistant_module, detector_module, extractor_module):
        source = inspect.getsource(module)
        for pattern in forbidden:
            assert pattern not in source, f"{module.__name__} contains {pattern}"

    instance = AssessmentAssistant()
    assert not hasattr(instance, "submit")
    assert not hasattr(instance, "click")
    assert not hasattr(instance, "select")
