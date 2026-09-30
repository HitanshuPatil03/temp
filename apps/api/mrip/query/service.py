"""The query service: classify a question, then dispatch to the right path.

Ties the router to the three deterministic paths. Narrative and draft intents —
the only ones a model would touch — currently fall through to discovery, which
answers with cited passages rather than prose. That is the honest degradation
ARCHITECTURE §7 asks for: when prose is not produced, the response says so
(``model_used`` stays ``False``) instead of returning an unsourced paraphrase.

The prose paths (a local model over retrieved passages, gated on
``settings.llm_enabled``) are a later increment. When they land they will live in
their own module and this dispatcher will branch to them — the figure paths below
will not change, because they never had a model to add.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mrip.query.compare import answer_series
from mrip.query.discovery import search_passages
from mrip.query.exact import answer_figure
from mrip.query.router import classify
from mrip.schemas import QueryIntent, QueryResponse

if TYPE_CHECKING:
    from mrip.auth.scope import Scope
    from mrip.db.store import Store

__all__ = ["answer"]


def answer(store: Store, scope: Scope, question: str) -> QueryResponse:
    """Route a natural-language question to an evidence-backed response."""
    routed = classify(question)

    if routed.intent is QueryIntent.EXACT_FIGURE:
        return answer_figure(store, scope, routed)
    if routed.intent is QueryIntent.COMPARISON:
        return answer_series(store, scope, routed)

    # DISCOVERY, plus NARRATIVE and DRAFT until the prose path exists: retrieval
    # with citations is the conservative branch (§13.1).
    return search_passages(store, scope, routed)
