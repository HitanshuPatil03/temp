"""The narrative and draft paths: prose written *around* figures, never over them.

This is the only query path that calls a model, and it does so under §13.4's
constraints: the model is handed retrieved passages and pinned facts and nothing
else — no fact store, no connection, no tool. Its output is then checked numeral
by numeral against those pinned numbers, and any sentence carrying a figure that
was not supplied is dropped and the answer flagged. A model cannot introduce a
citation or a number here; it can only arrange the ones it was given.

If the model runtime is unavailable, this raises :class:`~mrip.llm.LLMUnavailableError`
and the service falls back to cited passages — degraded, but never ungrounded.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mrip.llm import LLMClient
from mrip.schemas import (
    Fact,
    NarrativeAnswer,
    Passage,
    QueryIntent,
    QueryResponse,
    Refusal,
    RefusalReason,
)

if TYPE_CHECKING:
    from mrip.auth.scope import Scope
    from mrip.db.store import Store
    from mrip.query.router import RoutedQuery

__all__ = ["answer_narrative"]

_MAX_PASSAGES = 6
_MAX_FACTS = 12

_SYSTEM = (
    "You are a coal-sector reporting assistant. Answer only from the FACTS and "
    "PASSAGES provided. Every number you write must appear verbatim in the FACTS "
    "or PASSAGES — never estimate, convert, or introduce a figure. If the material "
    "does not support an answer, say so plainly. Be concise and factual; do not "
    "invent citations."
)

_DRAFT_SYSTEM = (
    "You are drafting an official reply for the Ministry of Coal. Use only the "
    "FACTS and PASSAGES provided. Keep every figure exactly as given — do not "
    "round, convert, or introduce numbers. Write in a formal, measured register "
    "suitable for a parliamentary answer."
)

# A numeric literal: digits with optional grouping commas, decimal, and percent.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _as_float(token: str) -> float | None:
    cleaned = token.replace(",", "").rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-6, abs(b) * 1e-6)


def _allowed_numbers(facts: list[Fact], passages: list[Passage]) -> list[float]:
    """Every number the model was given: pinned fact values and passage numerals.

    Both the FACTS and the PASSAGES are handed to the model, so a numeral drawn
    from either is grounded. Anything else in the output was invented. Period
    labels (``FY2024-25``) contribute their numbers too — a year or fiscal range
    the fact carries is context the model may legitimately restate.
    """
    allowed: list[float] = []
    for fact in facts:
        allowed.extend((fact.value, fact.raw_value))
        context = f"{fact.period_label} {fact.fiscal_year or ''}"
        allowed.extend(
            value
            for token in _NUMBER.findall(context)
            if (value := _as_float(token)) is not None
        )
    for passage in passages:
        allowed.extend(
            value
            for token in _NUMBER.findall(passage.snippet)
            if (value := _as_float(token)) is not None
        )
    return allowed


def _supported(token: str, allowed: list[float]) -> bool:
    value = _as_float(token)
    if value is None:
        return True
    # A bare four-digit year is context, not a reported figure.
    if token.isdigit() and len(token) == 4 and 1900 <= value <= 2099:
        return True
    return any(_close(value, candidate) for candidate in allowed)


def _verify(prose: str, allowed: list[float]) -> tuple[str, bool]:
    """Drop any sentence carrying a numeral not among the given numbers."""
    kept: list[str] = []
    flagged = False
    for sentence in _SENTENCE_SPLIT.split(prose.strip()):
        if not sentence:
            continue
        if all(_supported(token, allowed) for token in _NUMBER.findall(sentence)):
            kept.append(sentence)
        else:
            flagged = True
    return " ".join(kept).strip(), flagged


def _build_prompt(question: str, facts: list[Fact], passages: list[Passage]) -> str:
    lines = ["FACTS:"]
    if facts:
        for fact in facts:
            lines.append(
                f"- {fact.entity_id} {fact.metric} {fact.period_label} = "
                f"{fact.raw_value} {fact.raw_unit} [{fact.evidence.locator}]"
            )
    else:
        lines.append("- (none)")
    lines.append("\nPASSAGES:")
    if passages:
        for index, passage in enumerate(passages, start=1):
            where = f"p.{passage.page}" if passage.page is not None else ""
            title = passage.title or passage.filename or passage.document_id
            lines.append(f"[{index}] {title} {where}: {passage.snippet}")
    else:
        lines.append("(none)")
    lines.append(f"\nQUESTION: {question}")
    return "\n".join(lines)


def answer_narrative(
    store: Store, scope: Scope, routed: RoutedQuery, llm: LLMClient
) -> QueryResponse:
    """Write prose grounded in retrieved passages and pinned facts.

    Raises :class:`~mrip.llm.LLMUnavailableError` if the runtime is off or unreachable;
    the caller falls back to cited passages.
    """
    hits = store.evidence.search(routed.question, scope, limit=_MAX_PASSAGES)
    passages = [
        Passage(
            evidence_id=hit["evidence_id"],
            document_id=hit["document_id"],
            document_version=hit["document_version"],
            page=hit["page"],
            title=hit["title"],
            filename=hit["filename"],
            snippet=hit["snippet"],
            rank=float(hit["rank"]),
        )
        for hit in hits
    ]

    facts: list[Fact] = []
    if routed.slots.entity_id and routed.slots.metric_key:
        facts = store.facts.query(
            scope,
            entity_id=routed.slots.entity_id,
            metric=routed.slots.metric_key,
            limit=_MAX_FACTS,
        )

    if not passages and not facts:
        return QueryResponse(
            question=routed.question,
            intent=routed.intent,
            model_used=False,
            refusal=Refusal(
                reason=RefusalReason.OUT_OF_CORPUS,
                message="Nothing in your corpus grounds an answer to this question.",
            ),
        )

    system = _DRAFT_SYSTEM if routed.intent is QueryIntent.DRAFT else _SYSTEM
    prose = llm.generate(_build_prompt(routed.question, facts, passages), system=system)
    cleaned, flagged = _verify(prose, _allowed_numbers(facts, passages))

    return QueryResponse(
        question=routed.question,
        intent=routed.intent,
        model_used=True,
        narrative=NarrativeAnswer(
            prose=cleaned, passages=passages, facts=facts, flagged=flagged
        ),
    )
