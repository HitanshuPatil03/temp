"""The query service: classify a question, then dispatch to the right path.

Ties the router to the four paths. Exact figures and comparisons are answered by
SQL over facts with no model in scope; discovery returns cited passages. Only
narrative and draft reach the model, and even then it writes prose *around*
figures the deterministic layer pinned (see :mod:`mrip.query.narrative`).

When the model runtime is disabled or unreachable, the prose intents fall back to
cited passages rather than an ungrounded paraphrase — the honest degradation
ARCHITECTURE §7 asks for, visible to the caller as ``model_used == False``. The
figure paths never had a model to lose, so they are unaffected either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mrip.llm import LLMClient, LLMUnavailableError, client_from_settings
from mrip.query.compare import answer_series
from mrip.query.discovery import search_passages
from mrip.query.exact import answer_figure
from mrip.query.narrative import answer_narrative, figures_without_prose
from mrip.query.router import classify
from mrip.schemas import QueryIntent, QueryResponse

if TYPE_CHECKING:
    from mrip.auth.scope import Scope
    from mrip.db.store import Store

__all__ = ["answer"]

_PROSE_INTENTS = (QueryIntent.NARRATIVE, QueryIntent.DRAFT)


def answer(
    store: Store, scope: Scope, question: str, *, llm: LLMClient | None = None
) -> QueryResponse:
    """Route a natural-language question to an evidence-backed response.

    ``llm`` is injectable for testing; in production it is built from settings.
    """
    routed = classify(question)

    if routed.intent is QueryIntent.EXACT_FIGURE:
        return answer_figure(store, scope, routed)
    if routed.intent is QueryIntent.COMPARISON:
        return answer_series(store, scope, routed)

    if routed.intent in _PROSE_INTENTS:
        client = llm or client_from_settings(store.settings)
        if client.available:
            try:
                return answer_narrative(store, scope, routed, client)
            except LLMUnavailableError:
                # The runtime is off, unreachable, or slower than the generation
                # ceiling. Fall through to retrieval: cited passages, never an
                # ungrounded paraphrase (§7).
                pass

        # A prose question degrades in steps, and each one has to be tried before
        # the next. Cited passages come first.
        passages = search_passages(store, scope, routed)
        if passages.refusal is None:
            return passages
        # Then the figures. Without this rung a corpus holding the right *facts*
        # but no matching *text* answered "nothing in your corpus matches this
        # question" — blaming the data for a model outage, which is the silent
        # degradation §13.1 and the README both say does not happen here.
        return figures_without_prose(store, scope, routed, passages)

    # DISCOVERY.
    return search_passages(store, scope, routed)
