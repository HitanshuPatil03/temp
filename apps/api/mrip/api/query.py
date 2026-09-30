"""The query endpoint (ARCHITECTURE §13).

One ``POST /api/query`` takes a natural-language question and returns an
evidence-backed :class:`~mrip.schemas.QueryResponse` — a figure, a comparison
series, cited passages, or a structured refusal. The figure and comparison paths
run against a **read-only** store (:data:`~mrip.api.deps.ReadOnlyStoreDep`), so
§7's "this route only reads" is enforced by the database, not by convention.

The query itself is recorded in the audit log. Read-auditing arrives with the
query system on purpose (§9): once questions can pull real figures, who asked
what is part of the trail.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from mrip.api.deps import PrincipalDep, ReadOnlyStoreDep, ScopeDep, SourceIpDep, StoreDep
from mrip.query.service import answer
from mrip.schemas import QueryRequest, QueryResponse

router = APIRouter(tags=["query"])


def _answer_kind(response: QueryResponse) -> str:
    if response.figure is not None:
        return "figure"
    if response.comparison is not None:
        return "comparison"
    if response.discovery is not None:
        return "discovery"
    return "refusal"


@router.post("/query", response_model=QueryResponse)
def post_query(
    body: QueryRequest,
    request: Request,
    principal: PrincipalDep,
    scope: ScopeDep,
    reader: ReadOnlyStoreDep,
    audit_store: StoreDep,
    caller_ip: SourceIpDep,
) -> QueryResponse:
    """Answer a question over the caller's corpus, scoped and audited."""
    response = answer(reader, scope, body.question)

    audit_store.audit.record(
        "query",
        actor_user_id=principal.user_id,
        actor_username=principal.username,
        subject_type="query",
        detail={
            "question": body.question,
            "intent": response.intent.value,
            "answer_kind": _answer_kind(response),
            "model_used": response.model_used,
            "refusal_reason": (
                response.refusal.reason.value if response.refusal else None
            ),
        },
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )
    return response
