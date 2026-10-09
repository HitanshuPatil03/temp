"""The narrative and draft paths: prose written *around* figures, never over them.

This is the only query path that calls a model, and it does so under 13.4's
constraints: the model is handed retrieved passages and pinned facts and nothing
else. Its output is checked numeral by numeral; any sentence with an unsupplied
figure is dropped and flagged.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
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

__all__ = [
    "answer_narrative",
    "answer_narrative_stream",
    "figures_without_prose",
    "verify_numbers",
    "verify_prose",
]

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
    if token.isdigit() and len(token) == 4 and 1900 <= value <= 2099:
        return True
    return any(_close(value, candidate) for candidate in allowed)


def _verify(prose: str, allowed: list[float]) -> tuple[str, bool]:
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


def _gather_evidence(
    store: Store, scope: Scope, routed: RoutedQuery
) -> tuple[list[Fact], list[Passage]] | None:
    """Retrieve evidence or return None when nothing grounds the question."""
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
        return None
    return facts, passages


def figures_without_prose(
    store: Store, scope: Scope, routed: RoutedQuery, fallback: QueryResponse
) -> QueryResponse:
    """The last rung before refusing: the figures, and why there is no prose.

    A prose question degrades in four steps — generated prose, then cited
    passages, then the figures alone, then a refusal. The third step did not
    exist. When the model was off or too slow the prose path fell through to a
    lexical passage search, and a corpus with matching *facts* but no matching
    *text* produced "Nothing in your corpus matches this question" with an empty
    fact list. The data was there; the officer was told it was not.

    ``_gather_evidence`` runs the same passage search as that fallback and adds a
    fact lookup, so it can only ever know more — which is why this reuses it
    rather than searching again. If there are genuinely no facts either, the
    caller's refusal was right and is returned unchanged.
    """
    hits = _gather_evidence(store, scope, routed)
    facts = hits[0] if hits else []
    if not facts:
        return fallback

    return QueryResponse(
        question=routed.question,
        intent=routed.intent,
        model_used=False,
        refusal=Refusal(
            reason=RefusalReason.MODEL_UNAVAILABLE,
            message=(
                "The local model is unavailable, so this question cannot be "
                "answered in prose. The figures it would have been written "
                "around are below, with their sources — they come from the "
                "deterministic path and do not need the model."
            ),
            facts=facts,
        ),
    )


def answer_narrative(
    store: Store, scope: Scope, routed: RoutedQuery, llm: LLMClient
) -> QueryResponse:
    hits = _gather_evidence(store, scope, routed)
    if hits is None:
        return QueryResponse(
            question=routed.question,
            intent=routed.intent,
            model_used=False,
            refusal=Refusal(
                reason=RefusalReason.OUT_OF_CORPUS,
                message="Nothing in your corpus grounds an answer to this question.",
            ),
        )
    facts, passages = hits
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


def answer_narrative_stream(
    store: Store, scope: Scope, routed: RoutedQuery, llm: LLMClient
) -> tuple[list[Fact], list[Passage], str | None, Iterator[str]]:
    """Prepare evidence and return (facts, passages, refusal_reason, token_iter).

    If nothing grounds the question, token_iter is empty and refusal_reason is set.
    The caller yields tokens and runs _verify after — keeps numeral checking intact.
    """
    hits = _gather_evidence(store, scope, routed)
    if hits is None:
        return (
            [],
            [],
            "Nothing in your corpus grounds an answer to this question.",
            iter(()),
        )
    facts, passages = hits
    system = _DRAFT_SYSTEM if routed.intent is QueryIntent.DRAFT else _SYSTEM
    prompt = _build_prompt(routed.question, facts, passages)
    return facts, passages, None, llm.generate_stream(prompt, system=system)


def verify_prose(
    prose: str, facts: list[Fact], passages: list[Passage]
) -> tuple[str, bool]:
    """Public verifier for the streaming path."""
    return _verify(prose, _allowed_numbers(facts, passages))


def verify_numbers(prose: str, allowed: Sequence[float]) -> tuple[str, bool]:
    """Verify prose against an explicit set of supported numbers.

    The same check as :func:`verify_prose`, for callers whose pinned figures are
    not :class:`Fact` objects — the report generator hands over
    :class:`~mrip.schemas.PinnedFigure` rows. Exposed rather than reimplemented
    so that "is this numeral supported?" has exactly one definition in the
    codebase: a second copy would drift, and the copy that drifted would be the
    one standing between a model and a parliamentary answer.
    """
    return _verify(prose, list(allowed))
