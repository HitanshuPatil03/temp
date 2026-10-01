"""System endpoints: liveness, readiness, pipeline health, dashboard counters.

Three probes, not one, because they answer different questions and a deployment
needs all three:

- ``/health`` — is this process alive? (liveness; never touches the database)
- ``/ready`` — can it serve? (readiness; requires the database)
- ``/health/pipeline`` — is *ingestion* working? ("the API is up" is not the same
  claim, and an operator watching only the first two would never notice a worker
  pool that died three hours ago.)
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from mrip.api.deps import ScopeDep, SettingsDep, StoreDep, require_role
from mrip.auth.principal import Principal
from mrip.db import ping
from mrip.jobs.queue import JobQueue
from mrip.jobs.registry import registered_kinds
from mrip.schemas import Role

router = APIRouter(tags=["system"])

AdminDep = Annotated[Principal, Depends(require_role(Role.ADMIN))]


@router.get("/health")
def health(settings: SettingsDep) -> dict[str, Any]:
    """Liveness probe, also used by the frontend to distinguish a cold backend
    from an empty warehouse. Deliberately does no I/O."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "problem_statement": settings.problem_statement,
        "profile": settings.profile.value,
    }


@router.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    """Readiness probe. Returns 503 when the database is unreachable, so an
    orchestrator stops routing traffic here rather than serving errors."""
    database_ok = ping()
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if database_ok else "degraded", "database": database_ok}


@router.get("/health/pipeline")
def pipeline_health(store: StoreDep, admin: AdminDep) -> dict[str, Any]:
    """Whether ingestion is actually progressing.

    **Admin only.** Corpus size, queue depth and failed-document counts are
    operational facts spanning every subsidiary, so a viewer scoped to one must
    not read them — a system-wide document count discloses the scale of other
    subsidiaries' holdings. An alerting system reads this with an admin service
    account; a human orchestrator deciding whether to route traffic uses
    ``/ready``, which needs no secrets.

    Queue depth alone is not enough — a depth of 40 is healthy if the oldest item
    is twenty seconds old and an outage if it is four hours old, so both are
    reported. Documents sitting in `failed` or `quarantined` need a human and are
    surfaced as a single number an alert rule can watch.
    """
    queue = JobQueue(store.connection).stats()
    states = store.documents.count_by_state()

    return {
        "queue": queue,
        "documents_by_state": states,
        "needs_attention": (
            states.get("failed", 0)
            + states.get("quarantined", 0)
            + queue["dead"]
            + queue["failed"]
        ),
        "registered_job_kinds": registered_kinds(),
    }


@router.get("/dashboard/summary")
def dashboard_summary(
    store: StoreDep, settings: SettingsDep, scope: ScopeDep
) -> dict[str, Any]:
    """Counts behind the dashboard tiles, plus the thresholds that produced them.

    The thresholds travel with the numbers so the UI can state *why* a fact is in
    the review queue rather than showing an unexplained badge. The counts are
    scoped: a subsidiary's dashboard counts that subsidiary's corpus.
    """
    return {
        "counts": store.summary(scope),
        "thresholds": {
            "review_confidence": settings.review_confidence_threshold,
            "conflict_material_spread": settings.conflict_material_spread,
        },
    }
