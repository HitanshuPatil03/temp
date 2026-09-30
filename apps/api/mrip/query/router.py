"""The query router: decides a question's shape, and whether a model is involved.

Routing is on the *shape* of the question, read from deterministic patterns over
the normalizers' vocabularies (ARCHITECTURE §13.1). It does not answer anything —
it classifies, so the service can dispatch to a path that either never touches a
model (exact figure, comparison, discovery) or does (narrative, draft).

When the signals are weak, it takes the more conservative branch: discovery with
citations, never prose. This module imports no model client, and does not need
to — classification is pattern-matching, not generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mrip.query.parse import Slots, extract_slots
from mrip.schemas import QueryIntent

__all__ = ["RoutedQuery", "classify"]

# Verbs that ask for prose about causes, not a figure. Kept deliberately small;
# a false positive here costs a model call, a false negative just returns facts.
_NARRATIVE = re.compile(
    r"\b(why|explain|reason|because|driver|cause[ds]?|due to|account for|"
    r"how come|what happened|attribut)\w*",
    re.IGNORECASE,
)
_DRAFT = re.compile(
    r"\b(draft|write|compose|prepare)\b.*\b(reply|response|answer|note|letter|"
    r"para|paragraph)\b|parliamentary\s+(question|answer)|\bpq\b|\bstarred\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"\b(compare|comparison|versus|vs\.?|trend|over time|year[- ]on[- ]year|yoy|"
    r"across|each year|by year|growth|change)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RoutedQuery:
    intent: QueryIntent
    question: str
    slots: Slots


def classify(question: str) -> RoutedQuery:
    """Classify a question into one :class:`QueryIntent`."""
    slots = extract_slots(question)
    text = question.strip()

    if _DRAFT.search(text):
        intent = QueryIntent.DRAFT
    elif _NARRATIVE.search(text):
        intent = QueryIntent.NARRATIVE
    elif slots.entity_id and slots.metric_key and slots.period is not None:
        # A named entity, metric and period is the one shape SQL can answer
        # exactly — one measurement, one figure.
        intent = QueryIntent.EXACT_FIGURE
    elif slots.metric_key and (_COMPARISON.search(text) or not slots.period):
        # A metric asked about across entities or years is a series, not a
        # single figure. A metric with no period defaults here rather than
        # guessing which year the asker meant.
        intent = QueryIntent.COMPARISON
    else:
        intent = QueryIntent.DISCOVERY

    return RoutedQuery(intent=intent, question=question, slots=slots)
