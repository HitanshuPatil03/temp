"""Deterministic slot extraction for the query router.

Pulls the entity, metric and period out of a free-text question using only the
normalizers' own vocabularies — no model, no fuzzy guessing across a sentence.
Every slot is optional: a question that names none of them is not an error, it is
a question for the discovery path (ARCHITECTURE §13.1).

This module is on the figure path. It must never import a model client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mrip.normalize.entities import ENTITIES
from mrip.normalize.metrics import resolve_metric
from mrip.normalize.periods import Period, UnknownPeriodError, normalize_period

__all__ = ["Slots", "extract_slots"]


@dataclass(frozen=True, slots=True)
class Slots:
    """What a question named, as far as the normalizers could resolve it."""

    entity_id: str | None = None
    metric_key: str | None = None
    period: Period | None = None


def _norm(text: str) -> str:
    """Lowercase and reduce to space-separated alphanumeric tokens."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# (needle, entity_id), longest needle first so "coal india" wins over "coal" and a
# multi-word alias is matched before a bare code. Built once at import.
def _entity_needles() -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for entity in ENTITIES:
        candidates = {entity.code, entity.name, *entity.aliases}
        for candidate in candidates:
            needle = _norm(candidate)
            if needle:
                seen.append((needle, entity.entity_id))
    # Longest first; stable for equal lengths.
    return sorted(seen, key=lambda pair: len(pair[0]), reverse=True)


_ENTITY_NEEDLES = _entity_needles()

# Period-shaped substrings, tried most specific first. normalize_period does the
# real work; these only decide *what* to hand it, so a stray year in a sentence
# does not get parsed as a period unless it looks like one.
_PERIOD_PATTERNS = (
    r"q[1-4]\s*(?:fy)?\s*\d{2,4}(?:\s*[-–]\s*\d{2,4})?",
    r"h[12]\s*(?:fy)?\s*\d{2,4}(?:\s*[-–]\s*\d{2,4})?",
    r"fy\s*\d{2,4}(?:\s*[-–]\s*\d{2,4})?",
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*'?\s*\d{2,4}",
    r"\b\d{4}\s*[-–]\s*\d{2,4}\b",
    r"\bas\s+(?:on|at|of)\b[^,.;]{0,20}",
    r"\b(?:19|20)\d{2}\b",
)


def _find_entity(question: str) -> str | None:
    padded = f" {_norm(question)} "
    for needle, entity_id in _ENTITY_NEEDLES:
        if f" {needle} " in padded:
            return entity_id
    return None


def _find_period(question: str) -> Period | None:
    """Extract the most specific period the question names.

    Tries each candidate substring against the normalizer; a fiscal period beats
    a bare calendar year, because "FY2024-25" and "2024" resolve differently and
    the fiscal reading is the one a coal report almost always means.
    """
    best: Period | None = None
    for pattern in _PERIOD_PATTERNS:
        for match in re.finditer(pattern, question, flags=re.IGNORECASE):
            try:
                period = normalize_period(match.group(0))
            except UnknownPeriodError:
                continue
            if period.is_fiscal:
                return period
            best = best or period
    return best


def extract_slots(question: str) -> Slots:
    """Resolve the entity, metric and period a question names, each or none."""
    metric = resolve_metric(question)
    return Slots(
        entity_id=_find_entity(question),
        metric_key=metric.key if metric else None,
        period=_find_period(question),
    )
