"""Query endpoints (ARCHITECTURE 13).

POST /api/query       — blocking JSON response (all intents).
POST /api/query/stream — SSE stream for narrative/draft; falls back to JSON for
other intents or when the model is unavailable. Prototype-only: no auth change,
no new deps.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mrip.api.deps import PrincipalDep, ReadOnlyStoreDep, ScopeDep, SourceIpDep, StoreDep
from mrip.llm import LLMUnavailableError, client_from_settings
from mrip.normalize.entities import entity_by_id
from mrip.normalize.metrics import metric_by_key
from mrip.query.narrative import answer_narrative_stream, verify_prose
from mrip.query.router import classify
from mrip.query.service import answer
from mrip.schemas import QueryIntent, QueryRequest, QueryResponse

router = APIRouter(tags=["query"])


class Suggestion(BaseModel):
    """A question this corpus can answer, with the reason it is offered."""

    question: str
    intent: QueryIntent
    why: str = Field(description="What in the corpus makes this answerable")


@router.get("/query/suggestions", response_model=list[Suggestion])
def query_suggestions(
    reader: ReadOnlyStoreDep,
    scope: ScopeDep,
    limit: int = Query(default=5, ge=1, le=12),
) -> list[Suggestion]:
    """Questions derived from what is actually in the caller's corpus.

    This exists because the alternative does not work. A hand-written list of
    example questions is correct on the day it is written and wrong afterwards:
    the corpus changes, the examples do not, and a user who clicks one gets
    ``out_of_corpus``. They cannot tell that refusal apart from a broken
    product — and on a thin or freshly-seeded corpus most of the examples fail
    at once.

    So the suggestions are built from validated facts, from metrics enough
    entities report to make a comparison meaningful, and from keyphrases
    actually extracted. They are scope-aware for the same reason everything
    else is: suggesting a question about MCL to an SECL officer would disclose
    that MCL's filings exist.

    Prose intents are offered only when the model is reachable. Suggesting
    "why did X fall" with the runtime down sends the user to a fallback that
    looks like a failure.
    """
    suggestions: list[Suggestion] = []

    measurements = reader.facts.answerable_measurements(scope, limit=6)
    if measurements:
        top = measurements[0]
        metric = metric_by_key(str(top["metric"]))
        entity = entity_by_id(str(top["entity_id"]))
        label = metric.label.lower() if metric else str(top["metric"]).replace("_", " ")
        name = entity.code if entity else str(top["entity_id"]).upper()
        suggestions.append(
            Suggestion(
                question=f"{name} {label} {top['period_label']}",
                intent=QueryIntent.EXACT_FIGURE,
                why=f"{top['fact_count']} validated fact(s) in the corpus",
            )
        )

    for metric_key in reader.facts.comparable_metrics(scope)[:1]:
        metric = metric_by_key(metric_key)
        label = metric.label.lower() if metric else metric_key.replace("_", " ")
        suggestions.append(
            Suggestion(
                question=f"compare {label} across subsidiaries",
                intent=QueryIntent.COMPARISON,
                why="more than one entity reports this metric",
            )
        )

    for term in reader.keyphrases.cloud(scope, limit=1):
        suggestions.append(
            Suggestion(
                question=f"documents mentioning {term.term}",
                intent=QueryIntent.DISCOVERY,
                why=f"appears in {term.document_count} document(s)",
            )
        )

    # Prose intents last, and only if a model is actually reachable.
    if measurements and client_from_settings(reader.settings).available:
        top = measurements[0]
        metric = metric_by_key(str(top["metric"]))
        entity = entity_by_id(str(top["entity_id"]))
        label = metric.label.lower() if metric else str(top["metric"]).replace("_", " ")
        name = entity.code if entity else str(top["entity_id"]).upper()
        suggestions.append(
            Suggestion(
                question=f"explain {name} {label} in {top['period_label']}",
                intent=QueryIntent.NARRATIVE,
                why="the local model is available and these figures are pinned",
            )
        )
        suggestions.append(
            Suggestion(
                question=(
                    f"draft a reply to a parliamentary question on {name} "
                    f"{label} {top['period_label']}"
                ),
                intent=QueryIntent.DRAFT,
                why="the local model is available and these figures are pinned",
            )
        )

    return suggestions[:limit]


def _answer_kind(response: QueryResponse) -> str:
    if response.figure is not None:
        return "figure"
    if response.comparison is not None:
        return "comparison"
    if response.narrative is not None:
        return "narrative"
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


@router.post("/query/stream", response_model=None)
def post_query_stream(
    body: QueryRequest,
    request: Request,
    principal: PrincipalDep,
    scope: ScopeDep,
    reader: ReadOnlyStoreDep,
    audit_store: StoreDep,
    caller_ip: SourceIpDep,
) -> StreamingResponse | QueryResponse:
    """SSE stream for narrative/draft. Non-prose intents return JSON directly.

    Events:
      meta    — {intent, facts, passages} so the client can render citations immediately
      token   — {t: "..."} incremental text
      done    — {prose, flagged, facts, passages} final verified prose
      error   — {reason, message} when falling back to discovery
    """
    routed = classify(body.question)
    from mrip.schemas import QueryIntent

    # Non-prose: answer synchronously (no model to stream)
    if routed.intent not in (QueryIntent.NARRATIVE, QueryIntent.DRAFT):
        return post_query(body, request, principal, scope, reader, audit_store, caller_ip)

    # Try streaming; on LLM unavailability fall through to discovery JSON
    client = client_from_settings(reader.settings)
    if not client.available:
        return answer(reader, scope, body.question)

    try:
        facts, passages, refusal_msg, token_iter = answer_narrative_stream(
            reader, scope, routed, client
        )
    except LLMUnavailableError:
        return answer(reader, scope, body.question)

    if refusal_msg is not None:
        # Nothing grounds the question — return refusal JSON, not SSE
        from mrip.schemas import Refusal, RefusalReason

        resp = QueryResponse(
            question=body.question,
            intent=routed.intent,
            model_used=False,
            refusal=Refusal(reason=RefusalReason.OUT_OF_CORPUS, message=refusal_msg),
        )
        audit_store.audit.record(
            "query",
            actor_user_id=principal.user_id,
            actor_username=principal.username,
            subject_type="query",
            detail={
                "question": body.question,
                "intent": resp.intent.value,
                "answer_kind": "refusal",
                "model_used": False,
                "refusal_reason": resp.refusal.reason.value if resp.refusal else None,
            },
            request_id=getattr(request.state, "request_id", None),
            source_ip=caller_ip,
        )
        return resp

    # Snapshot for audit (filled after stream in finally via closure would be ideal;
    # for prototype we audit as narrative with model_used=true upfront)
    audit_store.audit.record(
        "query",
        actor_user_id=principal.user_id,
        actor_username=principal.username,
        subject_type="query",
        detail={
            "question": body.question,
            "intent": routed.intent.value,
            "answer_kind": "narrative",
            "model_used": True,
            "stream": True,
            "refusal_reason": None,
        },
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )

    def event_stream() -> Iterator[str]:
        # meta first so UI can show citations before tokens arrive
        meta = {
            "intent": routed.intent.value,
            "facts": [f.model_dump(mode="json") for f in facts],
            "passages": [p.model_dump(mode="json") for p in passages],
        }
        yield f"event: meta\ndata: {json.dumps(meta)}\n\n"

        collected: list[str] = []
        try:
            for token in token_iter:
                collected.append(token)
                yield f"event: token\ndata: {json.dumps({'t': token})}\n\n"
        except LLMUnavailableError as exc:
            err = json.dumps({"reason": "llm_unavailable", "message": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
            return

        raw = "".join(collected)
        cleaned, flagged = verify_prose(raw, facts, passages)
        done = {"prose": cleaned, "flagged": flagged}
        yield f"event: done\ndata: {json.dumps(done)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
