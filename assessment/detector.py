"""Read-only multiple-choice question detection.

The detector inspects native radio/checkbox groups, ARIA radio groups and
explicit quiz containers. It never clicks, selects, types or submits anything —
only ``querySelector``/``innerText`` style reads are performed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from playwright.async_api import Page

from assessment.extractor import Question, extract_questions
from config import Settings
from utils.logger import get_logger
from utils.selectors import Selectors

logger = get_logger(__name__)

MAX_QUESTIONS = 10

_DETECT_JS = (
    """
(cfg) => {
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();
  const isVisible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const isExcluded = (el) => {
    if (!el || !el.closest) return false;
    if (el.closest("nav, header, footer, form[action*='login'], [data-test-id*='login']")) return true;
    if (el.closest("[class*='filter'], [class*='search'], [class*='sort'], [class*='pagination'], [class*='cookie']")) return true;
    const cls = typeof el.className === 'string' ? el.className : '';
    return /(filter|search|sort|pagination|cookie|consent)/i.test(cls);
  };
  const isPrompt = (value) => {
    if (!value) return false;
    const v = value.trim();
    if (v.length < 8 || v.length > 800) return false;
    if (/^(submit|next|previous|skip|choose|select|answer|continue|start|begin)\\b/i.test(v)) return false;
    if (/^(filter|sort|search|show|refine)\\b/i.test(v)) return false;
    return true;
  };
  const promptFrom = (container, anchor) => {
    if (container) {
      for (const selector of cfg.assessmentPrompt) {
        let el = null;
        try { el = container.querySelector(selector); } catch (e) {}
        const value = text(el);
        if (isPrompt(value)) return value;
      }
    }
    let node = anchor || container;
    for (let depth = 0; depth < 5 && node; depth++) {
      let prev = node.previousElementSibling;
      while (prev) {
        const value = text(prev);
        if (isPrompt(value)) return value;
        prev = prev.previousElementSibling;
      }
      node = node.parentElement;
    }
    return '';
  };
  const contextFor = (container) => {
    let section = null;
    try { section = container.closest('section, [class*="section"]'); } catch (e) {}
    if (section) {
      const heading = section.querySelector('h2, h3, h4');
      const value = text(heading);
      if (value) return value;
    }
    const h1 = document.querySelector('h1');
    return text(h1) || '';
  };
  const labelFor = (input) => {
    if (!input) return '';
    if (input.id) {
      try {
        const label = document.querySelector("label[for='" + CSS.escape(input.id) + "']");
        const value = text(label);
        if (value) return value;
      } catch (e) {}
    }
    const wrapping = input.closest('label');
    if (wrapping) return text(wrapping);
    const ids = (input.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
    for (const id of ids) {
      try {
        const el = document.getElementById(id);
        const value = text(el);
        if (value) return value;
      } catch (e) {}
    }
    const described = text(input.parentElement);
    return described || input.value || '';
  };
  const uniqueQuestions = [];
  const seen = new Set();
  const push = (prompt, options, context, kind, detector) => {
    if (!prompt || options.length < 2) return;
    const cleaned = options.map((value) => (value || '').replace(/\\s+/g, ' ').trim()).filter(Boolean);
    if (cleaned.length < 2) return;
    const signature = prompt.trim().toLowerCase() + '|' + cleaned.join('|').toLowerCase();
    if (seen.has(signature)) return;
    seen.add(signature);
    uniqueQuestions.push({prompt, options: cleaned, context, kind, detector});
  };

  // 1) Native radio / checkbox inputs grouped by question.
  const inputs = Array.from(document.querySelectorAll("input[type='radio'], input[type='checkbox']"))
    .filter(isVisible)
    .filter((input) => !isExcluded(input));
  const groups = new Map();
  for (const input of inputs) {
    let container = null;
    try {
      container = input.closest(
        "fieldset, [data-test-id*='question'], [data-test-id*='assessment'], [class*='question'], [role='radiogroup'], [role='group']"
      );
    } catch (e) {}
    if (!container) {
      try { container = input.closest('form'); } catch (e) {}
    }
    if (!container) container = input.parentElement;
    const key = input.name ? 'name:' + input.name : container;
    if (!groups.has(key)) groups.set(key, {container, inputs: []});
    groups.get(key).inputs.push(input);
  }
  for (const group of groups.values()) {
    if (group.inputs.length < 2 || group.inputs.length > 10) continue;
    const first = group.inputs[0];
    const container = group.container;
    if (!container || isExcluded(container)) continue;
    const prompt = promptFrom(container, first);
    if (!prompt) continue;
    const options = group.inputs.map(labelFor);
    const kind = first.type === 'checkbox' ? 'multi_select' : 'multiple_choice';
    push(prompt, options, contextFor(container), kind, 'native');
  }

  // 2) ARIA radio / option widgets without native inputs.
  const roleEls = Array.from(document.querySelectorAll("[role='radio'], [role='option'], [role='checkbox']"))
    .filter(isVisible)
    .filter((el) => el.tagName !== 'INPUT')
    .filter((el) => !isExcluded(el));
  const roleGroups = new Map();
  for (const el of roleEls) {
    let container = null;
    try {
      container = el.closest(
        "[role='radiogroup'], [role='group'], fieldset, [data-test-id*='question'], [class*='question'], form"
      );
    } catch (e) {}
    if (!container) container = el.parentElement;
    if (!roleGroups.has(container)) roleGroups.set(container, []);
    roleGroups.get(container).push(el);
  }
  for (const [container, els] of roleGroups.entries()) {
    if (els.length < 2 || els.length > 10) continue;
    if (isExcluded(container)) continue;
    const prompt = promptFrom(container, els[0]);
    if (!prompt) continue;
    const options = els.map((el) => text(el)).filter(Boolean);
    push(prompt, options, contextFor(container), 'multiple_choice', 'role');
  }

  // 3) Explicit quiz containers whose options are plain buttons/items.
  if (uniqueQuestions.length === 0) {
    for (const selector of cfg.assessmentRoot) {
      let roots = [];
      try { roots = Array.from(document.querySelectorAll(selector)); } catch (e) { continue; }
      for (const root of roots) {
        if (!isVisible(root) || isExcluded(root)) continue;
        const prompt = promptFrom(root, root);
        if (!prompt) continue;
        const optionEls = Array.from(root.querySelectorAll(
          "button, [role='option'], [role='radio'], label, li[class*='option'], [class*='option-item']"
        )).filter(isVisible);
        const options = Array.from(new Set(optionEls.map((el) => text(el)).filter(Boolean)));
        if (options.length >= 2 && options.length <= 10) {
          push(prompt, options, contextFor(root), 'multiple_choice', 'container');
        }
      }
    }
  }

  return {url: location.href, questions: uniqueQuestions.slice(0, 10)};
}
"""
)


class AssessmentDetector:
    """Detects visible assessment questions in the page and its frames."""

    def __init__(
        self,
        page_provider: Callable[[], Page],
        selectors: Selectors,
        settings: Settings,
    ) -> None:
        self._page_provider = page_provider
        self.selectors = selectors
        self.settings = settings

    @property
    def page(self) -> Page:
        return self._page_provider()

    async def detect(self) -> list[Question]:
        if not self.settings.assessment_enabled:
            return []

        questions: list[Question] = []
        seen: set[str] = set()
        try:
            frames = list(self.page.frames)
        except Exception:
            return []

        for frame in frames:
            try:
                raw = await frame.evaluate(_DETECT_JS, self.selectors.as_js_dict())
            except Exception:
                continue
            for question in extract_questions(raw, source_url=frame.url):
                if question.signature in seen:
                    continue
                seen.add(question.signature)
                questions.append(question)
                if len(questions) >= MAX_QUESTIONS:
                    return questions

        if questions:
            logger.info(
                "Assessment detected",
                extra={"event": "assessment_detected", "questions": len(questions), "url": self.page.url},
            )
        return questions

    async def has_assessment(self) -> bool:
        return bool(await self.detect())

    async def wait_for_assessment(self, timeout: float = 10.0, poll_interval: float = 0.5) -> list[Question]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = await self.detect()
            if found:
                return found
            await asyncio.sleep(poll_interval)
        return []

    async def wait_until_gone(self, timeout: float, poll_interval: float = 1.0) -> bool:
        """Wait for the user to submit in the browser (never submits itself)."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not await self.detect():
                return True
            await asyncio.sleep(poll_interval)
        return False
