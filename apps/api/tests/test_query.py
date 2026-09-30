"""Tests for the AI query and response system (ARCHITECTURE §13).

Two things are asserted here above all: a numeric answer is a stored, validated
fact carrying its own evidence — never a value the router computed — and every
way of not answering is a *structured refusal* with a reason the client can act
on, not a bare error. The figure paths run with ``model_used=False``, which is
§7's "no model on the figure path" made checkable at the response boundary.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_read_only_store, provide_store
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db import Store
from mrip.main import create_app
from mrip.query.router import classify
from mrip.query.service import answer
from mrip.schemas import FactStatus, QueryIntent, RefusalReason, Role

SCOPE = Scope.unrestricted("test suite")


# --------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("SECL coal production FY2024-25", QueryIntent.EXACT_FIGURE),
        ("compare coal production across subsidiaries", QueryIntent.COMPARISON),
        ("coal production", QueryIntent.COMPARISON),
        ("why did SECL offtake fall in Q2", QueryIntent.NARRATIVE),
        ("draft a reply to PQ 1247", QueryIntent.DRAFT),
        ("documents mentioning Gevra", QueryIntent.DISCOVERY),
    ],
)
def test_router_classifies_on_shape(question: str, intent: QueryIntent) -> None:
    assert classify(question).intent is intent


def test_router_extracts_entity_metric_and_period() -> None:
    slots = classify("SECL coal production FY2024-25").slots
    assert slots.entity_id == "secl"
    assert slots.metric_key == "coal_production"
    assert slots.period is not None
    assert slots.period.label == "FY2024-25"


# ---------------------------------------------------------------- exact figure


def test_exact_figure_returns_the_validated_fact(store: Store, make_fact) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    response = answer(store, SCOPE, "SECL coal production FY2024-25")
    assert response.model_used is False
    assert response.figure is not None
    assert response.figure.fact.value == 193.0e6
    assert response.figure.fact.evidence.document_id  # carries its own evidence


def test_exact_figure_out_of_corpus_when_no_such_fact(store: Store) -> None:
    response = answer(store, SCOPE, "MCL coal production FY2024-25")
    assert response.figure is None
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.OUT_OF_CORPUS


def test_exact_figure_refuses_unvalidated(store: Store, make_fact) -> None:
    store.insert_facts([make_fact(status=FactStatus.EXTRACTED, unit_ambiguous=False)])
    response = answer(store, SCOPE, "SECL coal production FY2024-25")
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.NO_VALIDATED_FACT


def test_exact_figure_refuses_ambiguous_unit(store: Store, make_fact) -> None:
    # The default fixture fact is flagged unit_ambiguous.
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    response = answer(store, SCOPE, "SECL coal production FY2024-25")
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.AMBIGUOUS_UNIT
    assert response.refusal.facts  # the offending facts travel with the refusal


# ------------------------------------------------------------------ comparison


def test_comparison_returns_a_series_across_entities(store: Store, make_fact) -> None:
    store.insert_facts(
        [
            make_fact(entity_id="secl", value=193.0e6, unit_ambiguous=False),
            make_fact(entity_id="mcl", value=201.0e6, unit_ambiguous=False),
        ]
    )
    response = answer(store, SCOPE, "compare coal production across subsidiaries")
    assert response.model_used is False
    assert response.comparison is not None
    entities = {point.entity_id for point in response.comparison.points}
    assert {"secl", "mcl"} <= entities


def test_comparison_out_of_corpus_when_metric_absent(store: Store) -> None:
    response = answer(store, SCOPE, "compare coal production across subsidiaries")
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.OUT_OF_CORPUS


# ------------------------------------------------------------------- discovery


def test_discovery_refuses_when_nothing_matches(store: Store) -> None:
    response = answer(store, SCOPE, "documents mentioning Gevra")
    assert response.intent is QueryIntent.DISCOVERY
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.OUT_OF_CORPUS


# ----------------------------------------------------------------------- route


@pytest.fixture
def client(store: Store, make_user, bearer) -> TestClient:
    """A client whose read-write *and* read-only stores are the test store.

    Both are overridden onto the one transactional fixture: the figure path opens
    a separate read-only connection in production, but a test's uncommitted rows
    live in a single transaction, so the query path has to share it to see them.
    """
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store
    operator = make_user("query.tests", role=Role.ADMIN, entities=(SCOPE_ALL,))
    with TestClient(app, headers=bearer(operator)) as test_client:
        yield test_client


def test_query_route_answers_and_audits(
    client: TestClient, store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    reply = client.post("/api/query", json={"question": "SECL coal production FY2024-25"})
    assert reply.status_code == 200
    body = reply.json()
    assert body["intent"] == "exact_figure"
    assert body["model_used"] is False
    assert body["figure"]["fact"]["value"] == 193.0e6

    # The question is on the audit trail (§9).
    assert any(entry.action == "query" for entry in store.audit.recent(action="query"))
