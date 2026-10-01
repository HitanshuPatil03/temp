"""Word cloud and topic endpoints (ARCHITECTURE §12).

The cloud is built **from the caller's scope**, not filtered after rendering. An
SECL officer's cloud is SECL's corpus — a term appearing only in MCL's
unpublished filings does not appear, because its existence is itself a
disclosure. That is why every read here takes a scope and the extraction write
takes a role.

Every term reaches a document (`/topics/{term}/documents`), and the document's
own evidence route takes it the rest of the way to a page. A term that is a dead
end fails the §12 gate, so the drill-through ships with the cloud rather than
after it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from mrip.api.deps import ScopeDep, SourceIpDep, StoreDep, require_role
from mrip.auth.principal import Principal
from mrip.schemas import CloudTerm, Role, TermDocument
from mrip.topics.service import recompute_corpus

router = APIRouter(prefix="/topics", tags=["topics"])

OfficerDep = Annotated[Principal, Depends(require_role(Role.OFFICER))]


@router.get("/cloud", response_model=list[CloudTerm])
def word_cloud(
    store: StoreDep,
    scope: ScopeDep,
    fiscal_year: str | None = Query(default=None, description="Restrict to one FY"),
    entity_id: str | None = Query(default=None, description="Restrict to one entity"),
    doc_class: str | None = Query(default=None, description="Restrict to one class"),
    limit: int = Query(default=120, ge=1, le=500),
) -> list[CloudTerm]:
    """The word cloud, sized by document frequency.

    Filters are part of the query rather than a post-filter on a rendered image
    (§12.3): narrowing to a fiscal year *re-weights* the cloud, because document
    frequency within that year is a different number from document frequency
    across the corpus. Filtering a picture could not do that.
    """
    return store.keyphrases.cloud(
        scope,
        fiscal_year=fiscal_year,
        entity_id=entity_id,
        doc_class=doc_class,
        limit=limit,
    )


@router.get("/{term}/documents", response_model=list[TermDocument])
def term_documents(
    term: str,
    store: StoreDep,
    scope: ScopeDep,
    fiscal_year: str | None = None,
    entity_id: str | None = None,
    doc_class: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TermDocument]:
    """Documents that used a term — the click-through that makes it an index.

    An empty list is a valid answer and not a 404: the term may exist in the
    corpus but in documents this caller cannot see, and the two cases must look
    identical from outside or the cloud leaks what it filtered.
    """
    return store.keyphrases.documents_for_term(
        term.lower(),
        scope,
        fiscal_year=fiscal_year,
        entity_id=entity_id,
        doc_class=doc_class,
        limit=limit,
    )


@router.get("/{term}/prevalence")
def term_prevalence(
    term: str, store: StoreDep, scope: ScopeDep, entity_id: str | None = None
) -> list[dict[str, Any]]:
    """How many documents used a term in each fiscal year (§12.7).

    The shape that answers "is this subject getting more attention or less",
    which a single cloud cannot show.
    """
    return store.keyphrases.prevalence(term.lower(), scope, entity_id=entity_id)


@router.post("/extract")
def extract_topics(
    store: StoreDep,
    scope: ScopeDep,
    officer: OfficerDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> dict[str, Any]:
    """Re-extract keyphrases across the corpus, with IDF over the whole of it.

    A write, so it takes a role rather than being a read anyone may trigger. The
    recompute is corpus-wide and therefore unscoped — IDF is a property of the
    corpus, not of the reader — but it returns only counts, and the cloud built
    from it is scoped on every read.
    """
    written = recompute_corpus(store)
    store.audit.record(
        "topics.extracted",
        actor_user_id=officer.user_id,
        actor_username=officer.username,
        subject_type="corpus",
        detail={
            "documents": len(written),
            "terms_written": sum(written.values()),
        },
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )
    return {
        "documents": len(written),
        "terms_written": sum(written.values()),
        "terms_in_scope": store.keyphrases.count_terms(scope),
    }
