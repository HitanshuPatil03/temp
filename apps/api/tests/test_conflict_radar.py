"""The conflict radar cannot be cleared quietly.

Detection is not a read. It keeps the groups whose relative spread reaches the
materiality threshold, deletes every *unresolved* conflict that is no longer in
that set, and returns the facts behind them from ``conflicted`` to
``extracted``. That is correct when a disagreement has genuinely been fixed —
and it is a way to erase an unadjudicated disagreement if the threshold can be
loosened on request.

It matters because the radar is what stops two contradictory figures from both
being usable. The project's headline refusal is exactly this: two statements
claim 191.5 and 193 Mt, no reviewer has chosen, so the report declines to pin
the figure. Clear the radar and that refusal does not happen.

So: the threshold may be tightened, never loosened, and every detection run
leaves a row in the audit log.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mrip.api.deps import provide_store
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db import Store
from mrip.db.tables import conflicts as conflicts_table
from mrip.main import create_app
from mrip.schemas import FactStatus, Role

SCOPE = Scope.unrestricted("test suite")


@pytest.fixture
def client(store: Store, make_user, bearer) -> TestClient:
    """A reviewer, because that is the lowest role the endpoint accepts."""
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    reviewer = make_user("radar.reviewer", role=Role.REVIEWER, entities=(SCOPE_ALL,))
    with TestClient(app, headers=bearer(reviewer)) as test_client:
        yield test_client


@pytest.fixture
def disagreement(store: Store, make_fact):
    """Two validated facts for one entity/metric/period that do not agree.

    191.5 against 193.0 Mt — the real SECL production discrepancy the README
    describes, and a 0.8% spread, comfortably above the 0.5% default.
    """
    store.insert_facts(
        [
            make_fact(value=191_500_000.0, status=FactStatus.VALIDATED),
            make_fact(value=193_000_000.0, status=FactStatus.VALIDATED),
        ]
    )
    groups = store.detect_conflicts(SCOPE)
    assert len(groups) == 1, "the fixture must produce exactly one open conflict"
    return groups[0]


def _open_conflict_count(store: Store) -> int:
    return int(
        store.connection.execute(
            sa.select(sa.func.count())
            .select_from(conflicts_table)
            .where(conflicts_table.c.resolved_fact_id.is_(None))
        ).scalar_one()
    )


def test_a_loose_threshold_really_would_delete_the_radar(
    store: Store, disagreement
) -> None:
    """The reason the guard exists, asserted at the layer below the route.

    Without this, the refusal in the next test would be guarding a behaviour
    nobody had demonstrated — and a future contributor removing the check would
    see every other test still pass.
    """
    assert _open_conflict_count(store) == 1

    # 100% spread: nothing short of a doubling qualifies, so the group drops out.
    store.detect_conflicts(SCOPE, material_spread=1.0)

    assert _open_conflict_count(store) == 0, (
        "detection deletes unresolved conflicts that no longer qualify; if this "
        "ever stops being true the route guard can be relaxed"
    )
    # And the figures are no longer flagged, so nothing downstream knows they
    # disagree.
    statuses = {
        fact.status
        for fact in store.query_facts(
            SCOPE, metric="coal_production", include_inactive=True
        )
    }
    assert FactStatus.CONFLICTED not in statuses


def test_the_route_refuses_a_threshold_looser_than_policy(client, disagreement) -> None:
    """422, and the radar is untouched."""
    response = client.post("/api/conflicts/detect?material_spread=1.0")

    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["error"] == "spread_too_loose"
    assert body["requested"] == 1.0
    assert body["configured"] == pytest.approx(0.005)
    # The message has to say which direction is allowed, or a reviewer will just
    # try again with another number.
    assert "tighten" in body["message"]


def test_the_refusal_leaves_the_conflict_open(client, store: Store, disagreement) -> None:
    """A refused request must not be a partially-applied one.

    The check runs before the detection call, so this would only fail if someone
    moved it after — which is the mistake worth catching.
    """
    client.post("/api/conflicts/detect?material_spread=1.0")

    assert _open_conflict_count(store) == 1
    still_open = client.get("/api/conflicts").json()
    assert len(still_open) == 1


def test_tightening_the_threshold_is_allowed(client, disagreement) -> None:
    """Tighter means *more* disagreements surface, which harms nothing, and the
    next scheduled sweep reconciles back to policy."""
    response = client.post("/api/conflicts/detect?material_spread=0.0")

    assert response.status_code == 200
    assert len(response.json()) >= 1


def test_omitting_the_threshold_uses_deployment_policy(client, disagreement) -> None:
    response = client.post("/api/conflicts/detect")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_every_detection_run_is_audited(client, store: Store, disagreement) -> None:
    """Audited because it is destructive, not because it is a decision.

    The row is what separates "the radar emptied because a reviewer re-ran it"
    from "the radar emptied and nobody knows why".
    """
    client.post("/api/conflicts/detect")

    entries = store.audit.recent(limit=5, action="conflicts.detected")
    assert entries, "a destructive recompute left no trace"
    entry = entries[0]
    assert entry.actor_username == "radar.reviewer"
    assert entry.detail["material_spread"] == pytest.approx(0.005)
    assert entry.detail["open_conflicts"] == 1
    assert entry.detail["tightened"] is False


def test_the_audit_row_records_a_tightened_threshold(
    client, store: Store, disagreement
) -> None:
    """Which threshold ran is the part worth keeping: two runs minutes apart can
    legitimately disagree about how many conflicts are open, and the trail should
    explain why rather than look inconsistent."""
    client.post("/api/conflicts/detect?material_spread=0.0")

    entry = store.audit.recent(limit=5, action="conflicts.detected")[0]
    assert entry.detail["material_spread"] == pytest.approx(0.0)
    assert entry.detail["tightened"] is True


def test_a_refused_request_is_not_audited_as_a_detection(
    client, store: Store, disagreement
) -> None:
    """Nothing happened, so the trail must not suggest something did."""
    client.post("/api/conflicts/detect?material_spread=1.0")

    assert store.audit.recent(limit=5, action="conflicts.detected") == []
