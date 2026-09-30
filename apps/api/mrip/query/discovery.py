"""The discovery path: where in the corpus a question is answered.

Lexical retrieval over the evidence index (ARCHITECTURE §13.2). Vector fusion is
designed but not yet in the schema, so this is the lexical half today; when an
embedding column lands, the fusion happens here without the callers changing.

No model client: discovery returns the passages and their locators, never a
paraphrase. It is also the conservative fallback the service reaches for when a
narrative question cannot be answered with prose — retrieval with citations
beats generated text with none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mrip.schemas import (
    DiscoveryAnswer,
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

__all__ = ["search_passages"]


def search_passages(
    store: Store, scope: Scope, routed: RoutedQuery, *, limit: int = 10
) -> QueryResponse:
    """Answer a discovery question with ranked, cited passages from the corpus."""
    hits = store.evidence.search(routed.question, scope, limit=limit)

    if not hits:
        return QueryResponse(
            question=routed.question,
            intent=routed.intent,
            model_used=False,
            refusal=Refusal(
                reason=RefusalReason.OUT_OF_CORPUS,
                message="Nothing in your corpus matches this question.",
            ),
        )

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
    return QueryResponse(
        question=routed.question,
        intent=QueryIntent.DISCOVERY,
        model_used=False,
        discovery=DiscoveryAnswer(passages=passages),
    )
