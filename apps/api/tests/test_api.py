"""Tests for the HTTP layer.

The store is replaced with the transactional test store through
``app.dependency_overrides``, so these exercise routing, serialization and error
handling against a real schema.

Requests are **authenticated the real way**: the client fixture creates an admin
account and presents a genuine bearer token, so every test here also proves the
dependency chain (token → user row → grants → scope) holds. Only the password
verification is skipped, and tests/test_auth.py covers that directly.

The assertions that matter most check the API's *refusals*: an unresolvable unit
returns 422, an unknown entity returns 404, and there is no route that merges a
conflict. An API that guesses would pass a laxer version of this file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_store
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db import Store
from mrip.main import create_app
from mrip.schemas import FactStatus, Role

#: These suites exercise storage and routing, not access control; the
#: scope-enforcement assertions live in tests/test_scope.py.
SCOPE = Scope.unrestricted("test suite")


@pytest.fixture
def client(store: Store, make_user, bearer) -> TestClient:
    """A test client whose backend is the transactional ``store`` fixture.

    The account is an admin with an HQ-wide grant: these tests are about routing
    and serialization, so the role and scope are deliberately not the variable
    under test. Role refusals are asserted in tests/test_auth.py.
    """
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    operator = make_user("api.tests", role=Role.ADMIN, entities=(SCOPE_ALL,))
    with TestClient(app, headers=bearer(operator)) as test_client:
        yield test_client


# --------------------------------------------------------------------------- system


def test_health_reports_the_problem_statement(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["problem_statement"] == "SIH26023"


def test_dashboard_summary_ships_its_thresholds(client):
    """A review badge means nothing without the threshold that produced it."""
    body = client.get("/api/dashboard/summary").json()
    assert body["counts"]["facts"] == 0
    assert body["thresholds"]["review_confidence"] == pytest.approx(0.80)
    assert body["thresholds"]["conflict_material_spread"] == pytest.approx(0.005)


def test_openapi_schema_builds(client):
    """Catches response_model mistakes that only surface at schema generation."""
    schema = client.get("/openapi.json").json()
    assert "/api/facts" in schema["paths"]
    assert "/api/conflicts/{conflict_id}/resolve" in schema["paths"]


# ------------------------------------------------------------------------ documents


def test_documents_round_trip_over_http(client, store, make_document):
    document = store.register_document(make_document())

    listed = client.get("/api/documents").json()
    assert len(listed) == 1
    assert listed[0]["document_id"] == document.document_id

    fetched = client.get(f"/api/documents/{document.document_id}").json()
    assert fetched["title"] == "Coal India Limited Annual Report FY2024-25"
    assert fetched["doc_class"] == "text_pdf"


def test_unknown_document_is_404(client):
    assert client.get("/api/documents/doc_nope").status_code == 404


def test_page_evidence_requires_a_known_document(client):
    assert client.get("/api/documents/doc_nope/pages/1/evidence").status_code == 404


def test_page_evidence_returns_bounding_boxes(client, store, make_document):
    document = store.register_document(make_document())
    store.insert_evidence(
        [
            {
                "document_id": document.document_id,
                "page": 47,
                "kind": "table_cell",
                "cell_ref": "r3c2",
                "text": "193.00",
                "ocr_confidence": 0.99,
                "bbox_x0": 72.0,
                "bbox_y0": 310.5,
                "bbox_x1": 148.25,
                "bbox_y1": 324.0,
                "extraction_method": "table_lattice",
            }
        ]
    )
    spans = client.get(f"/api/documents/{document.document_id}/pages/47/evidence").json()

    assert len(spans) == 1
    assert spans[0]["bbox_x1"] == 148.25
    assert spans[0]["text"] == "193.00"


# ---------------------------------------------------------------------------- facts


def test_fact_is_served_with_its_evidence(client, store, make_fact):
    fact = store.insert_facts([make_fact()]) and store.query_facts(SCOPE)[0]

    body = client.get(f"/api/facts/{fact.fact_id}").json()

    assert body["value"] == 193.0e6
    assert body["raw_unit"] == "MT"
    assert body["unit_ambiguous"] is True
    assert body["evidence"]["page"] == 47
    assert body["evidence"]["cell_ref"] == "r3c2"
    assert body["evidence"]["snippet"] == "SECL 193.00"


def test_fact_confidence_is_served_as_three_fields(client, store, make_fact):
    store.insert_facts([make_fact(confidence={"ocr": 0.72, "parse": 0.95})])
    fact = client.get("/api/facts").json()[0]
    assert fact["confidence"] == {"ocr": 0.72, "parse": 0.95, "answer": None}


def test_fact_filters_are_exposed_as_query_params(client, store, make_fact):
    store.insert_facts([make_fact(entity_id="secl"), make_fact(entity_id="mcl")])
    assert len(client.get("/api/facts?entity_id=secl").json()) == 1
    assert len(client.get("/api/facts?fiscal_year=FY2024-25").json()) == 2
    assert client.get("/api/facts?fiscal_year=FY2019-20").json() == []


def test_unknown_fact_is_404(client):
    assert client.get("/api/facts/fact_nope").status_code == 404


def test_series_endpoint_shapes_data_for_charts(client, store, make_fact):
    store.insert_facts(
        [
            make_fact(entity_id="secl", status=FactStatus.VALIDATED),
            make_fact(entity_id="mcl", value=201.0e6, status=FactStatus.VALIDATED),
        ]
    )
    series = client.get("/api/series/coal_production?unit=t").json()

    assert {row["entity_id"] for row in series} == {"secl", "mcl"}
    assert all(row["fiscal_year"] == "FY2024-25" for row in series)


# ------------------------------------------------------------------------ conflicts


def test_conflicts_are_listed_with_every_disagreeing_fact(client, store, make_fact):
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=191.5e6)])
    client.post("/api/conflicts/detect")

    conflicts = client.get("/api/conflicts").json()

    assert len(conflicts) == 1
    assert len(conflicts[0]["facts"]) == 2
    assert sorted(f["value"] for f in conflicts[0]["facts"]) == [191.5e6, 193.0e6]
    assert conflicts[0]["resolved_fact_id"] is None


def test_resolution_requires_a_fact_from_the_conflict(client, store, make_fact):
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=150.0e6)])
    conflict = client.post("/api/conflicts/detect").json()[0]

    response = client.post(
        f"/api/conflicts/{conflict['conflict_id']}/resolve",
        json={"winning_fact_id": "fact_unrelated"},
    )
    assert response.status_code == 404


def test_resolution_records_the_reviewers_reason(client, store, make_fact):
    audited, provisional = make_fact(value=193.0e6), make_fact(value=191.5e6)
    store.insert_facts([audited, provisional])
    conflict = client.post("/api/conflicts/detect").json()[0]

    response = client.post(
        f"/api/conflicts/{conflict['conflict_id']}/resolve",
        json={
            "winning_fact_id": audited.fact_id,
            "note": "Audited annual report supersedes the provisional release",
        },
    )

    assert response.status_code == 200
    assert response.json()["resolved_fact_id"] == audited.fact_id
    assert client.get("/api/conflicts").json() == []
    assert client.get(f"/api/facts/{audited.fact_id}").json()["status"] == "validated"
    assert (
        client.get(f"/api/facts/{provisional.fact_id}?include_inactive=true").json()[
            "status"
        ]
        == "rejected"
    )


# ------------------------------------------------------------------------ normalize


@pytest.mark.parametrize(
    ("value", "unit", "expected_value", "expected_unit"),
    [
        (3.2, "lakh tonnes", 3.2e5, "t"),
        (1234.5, "Rs crore", 1234.5e7, "inr"),
        (2.5, "M.Cum", 2.5e6, "m3"),
        (4500.0, "kcal/kg", 4500.0, "kcal/kg"),
        (160.0, "MTPA", 160e6, "t/yr"),
    ],
)
def test_quantity_endpoint_converts(client, value, unit, expected_value, expected_unit):
    body = client.get(
        "/api/normalize/quantity", params={"value": value, "unit": unit}
    ).json()
    assert body["unit"] == expected_unit
    assert body["value"] == pytest.approx(expected_value, rel=1e-6)
    assert body["raw_unit"] == unit


def test_quantity_endpoint_surfaces_mt_ambiguity(client):
    body = client.get(
        "/api/normalize/quantity", params={"value": 704.2, "unit": "MT"}
    ).json()
    assert body["value"] == pytest.approx(704.2e6)
    assert body["ambiguous"] is True
    assert "million tonnes" in body["note"]


def test_quantity_endpoint_honours_the_mt_convention_override(client):
    body = client.get(
        "/api/normalize/quantity",
        params={"value": 250, "unit": "MT", "mt_convention": "metric_tonne"},
    ).json()
    assert body["value"] == pytest.approx(250.0)
    assert body["ambiguous"] is True


def test_unresolvable_unit_is_422_with_a_reason(client):
    response = client.get(
        "/api/normalize/quantity", params={"value": 1, "unit": "wibbles"}
    )
    assert response.status_code == 422
    assert "wibbles" in response.json()["detail"]


def test_period_endpoint_resolves_a_fiscal_year(client):
    body = client.get("/api/normalize/period", params={"raw": "Q3 FY2024-25"}).json()
    assert body["start"] == "2024-10-01"
    assert body["end"] == "2024-12-31"
    assert body["fiscal_year"] == "FY2024-25"
    assert body["is_fiscal"] is True


def test_period_endpoint_keeps_a_calendar_year_non_fiscal(client):
    body = client.get("/api/normalize/period", params={"raw": "2024"}).json()
    assert body["kind"] == "calendar_year"
    assert body["fiscal_year"] is None
    assert body["is_fiscal"] is False


def test_unresolvable_period_is_422(client):
    assert (
        client.get("/api/normalize/period", params={"raw": "sometime"}).status_code == 422
    )


def test_comparability_endpoint_blocks_fiscal_against_calendar(client):
    body = client.get(
        "/api/normalize/period/comparable",
        params={"left": "FY2024-25", "right": "2024"},
    ).json()
    assert body["comparable"] is False
    assert "fiscal and calendar" in body["reason"]


def test_comparability_endpoint_allows_two_fiscal_years(client):
    body = client.get(
        "/api/normalize/period/comparable",
        params={"left": "FY2023-24", "right": "FY2024-25"},
    ).json()
    assert body["comparable"] is True
    assert body["reason"] is None


def test_entity_endpoint_resolves_an_alias(client):
    body = client.get(
        "/api/normalize/entity", params={"q": "South Eastern Coalfields Ltd."}
    ).json()
    assert body["entity_id"] == "secl"
    assert body["is_cil_group"] is True
    assert body["exact"] is True
    assert body["needs_review"] is False


def test_entity_endpoint_flags_a_damaged_name_for_review(client):
    body = client.get(
        "/api/normalize/entity", params={"q": "Mahanadi Coalfeilds Limited"}
    ).json()
    assert body["entity_id"] == "mcl"
    assert body["exact"] is False
    assert body["needs_review"] is True


def test_entity_endpoint_marks_sccl_outside_the_cil_group(client):
    body = client.get(
        "/api/normalize/entity", params={"q": "Singareni Collieries"}
    ).json()
    assert body["is_cil_group"] is False
    assert "NOT a CIL subsidiary" in body["note"]


def test_unknown_organisation_is_404_not_a_guess(client):
    response = client.get("/api/normalize/entity", params={"q": "Acme Mining Inc"})
    assert response.status_code == 404
