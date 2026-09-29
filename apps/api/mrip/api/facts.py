"""Fact and conflict endpoints.

Conflict resolution is a ``POST`` with an explicit winning fact id. There is no
endpoint that merges conflicting facts or picks a value automatically, because
that decision belongs to a reviewer — see
:meth:`mrip.db.repositories.conflicts.ConflictRepository.resolve`.

Reads need only a valid session; the scope dependency filters them to the
caller's subsidiaries. **Adjudication needs the reviewer role**, and the
resolution is recorded in the audit log with the reviewer's id — an accepted
figure that nobody is named against is not an adjudication, it is an anonymous
edit to the record.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mrip.api.deps import ScopeDep, SourceIpDep, StoreDep, require_role
from mrip.auth.principal import Principal
from mrip.schemas import ConflictGroup, Fact, FactStatus, Role

router = APIRouter(tags=["facts"])

ReviewerDep = Annotated[Principal, Depends(require_role(Role.REVIEWER))]


class ResolutionRequest(BaseModel):
    """A reviewer's adjudication of a conflict."""

    winning_fact_id: str
    note: str | None = Field(
        default=None,
        description="Why this figure was chosen. Stored with the decision.",
    )


@router.get("/facts", response_model=list[Fact])
def list_facts(
    store: StoreDep,
    scope: ScopeDep,
    entity_id: str | None = None,
    metric: str | None = None,
    fiscal_year: str | None = None,
    document_id: str | None = None,
    status: FactStatus | None = None,
    include_inactive: bool = False,
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[Fact]:
    """Query facts. Rejected and superseded facts are hidden by default."""
    return store.query_facts(
        scope,
        entity_id=entity_id,
        metric=metric,
        fiscal_year=fiscal_year,
        document_id=document_id,
        status=status,
        # Asking for an inactive status implies wanting to see inactive rows;
        # otherwise the filter would silently return nothing.
        include_inactive=include_inactive
        or status in (FactStatus.SUPERSEDED, FactStatus.REJECTED),
        limit=limit,
    )


@router.get("/facts/{fact_id}", response_model=Fact)
def get_fact(fact_id: str, store: StoreDep, scope: ScopeDep) -> Fact:
    """One fact with its full evidence ref — the target of a citation click."""
    fact = store.get_fact(fact_id, scope)
    if fact is None:
        raise HTTPException(status_code=404, detail=f"No fact {fact_id!r}")
    return fact


@router.get("/series/{metric}")
def metric_series(
    metric: str, store: StoreDep, scope: ScopeDep, unit: str | None = None
) -> list[dict[str, Any]]:
    """Per-entity fiscal-year series for one metric, shaped for the charts.

    Calendar-year facts are excluded rather than silently mixed in.
    """
    return store.entity_metric_series(metric, scope, unit=unit)


@router.get("/conflicts", response_model=list[ConflictGroup])
def list_conflicts(store: StoreDep, scope: ScopeDep) -> list[ConflictGroup]:
    """Conflicts awaiting adjudication, each carrying every disagreeing fact."""
    return store.open_conflicts(scope)


@router.post("/conflicts/detect", response_model=list[ConflictGroup])
def detect_conflicts(
    store: StoreDep,
    scope: ScopeDep,
    reviewer: ReviewerDep,
    material_spread: float | None = Query(default=None, ge=0.0, le=1.0),
) -> list[ConflictGroup]:
    """Re-run the conflict radar over the caller's facts.

    A write (it creates and reconciles conflict rows), so it takes the reviewer
    role rather than being a read anyone may trigger. The scheduled sweep does
    the same work corpus-wide every 23 minutes; this exists for the reviewer who
    has just corrected something and wants the radar refreshed now.
    """
    return store.detect_conflicts(scope, material_spread=material_spread)


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(
    conflict_id: str,
    body: ResolutionRequest,
    store: StoreDep,
    scope: ScopeDep,
    reviewer: ReviewerDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> dict[str, Any]:
    """Record which figure a reviewer accepted, and reject the others.

    The reviewer's id travels into the stored decision *and* the audit log. The
    losing facts become ``rejected`` — a human judged them wrong — which is a
    different and stronger claim than ``superseded``, and the trail has to say
    who made it.
    """
    ok = store.resolve_conflict(
        conflict_id,
        body.winning_fact_id,
        scope,
        note=body.note,
        actor_user_id=reviewer.user_id,
    )
    if ok:
        store.audit.record(
            "conflict.resolved",
            actor_user_id=reviewer.user_id,
            actor_username=reviewer.username,
            subject_type="conflict",
            subject_id=conflict_id,
            detail={"winning_fact_id": body.winning_fact_id, "note": body.note},
            request_id=getattr(request.state, "request_id", None),
            source_ip=caller_ip,
        )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Conflict {conflict_id!r} not found, already resolved, or fact "
                f"{body.winning_fact_id!r} is not part of it"
            ),
        )
    return {"conflict_id": conflict_id, "resolved_fact_id": body.winning_fact_id}
