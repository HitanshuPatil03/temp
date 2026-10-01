"""Tests for the reports and topics HTTP layer.

What these add over ``test_reports_store.py`` and ``test_topics.py`` is the part
only the route can get wrong: who is allowed to call it, what status code a
refusal carries, and whether the response still says *why* rather than merely
*no*.

The three that matter most are refusals. Generating a report for an entity the
caller cannot see is a 404 and not a 403, because confirming the entity exists
is itself a disclosure. Downloading an incomplete report is a 409 naming the
missing field, because §11.2 forbids emitting a blank. And skipping review is a
409 naming what *is* allowed, because an approval step that can be stepped
around is not an approval step.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_read_only_store, provide_store
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db import Store
from mrip.main import create_app
from mrip.schemas import ExtractionMethod, FactStatus, Role

SCOPE = Scope.unrestricted("test suite")


@pytest.fixture
def client(store: Store, make_user, bearer) -> TestClient:
    """An approver with an HQ-wide grant — enough for every route here."""
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store
    operator = make_user("reports.tests", role=Role.APPROVER, entities=(SCOPE_ALL,))
    with TestClient(app, headers=bearer(operator)) as test_client:
        yield test_client


@pytest.fixture
def viewer_client(store: Store, make_user, bearer) -> TestClient:
    """A viewer: may read, may not generate or approve."""
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store
    operator = make_user("viewer.tests", role=Role.VIEWER, entities=(SCOPE_ALL,))
    with TestClient(app, headers=bearer(operator)) as test_client:
        yield test_client


def _generate(client: TestClient, entity: str = "SECL", period: str = "FY2024-25"):
    return client.post("/api/reports", json={"entity": entity, "period": period})


# ------------------------------------------------------------------- reports


def test_generating_a_report_stores_and_returns_the_manifest(
    client, store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])

    response = _generate(client)

    assert response.status_code == 201
    body = response.json()
    assert body["figures"][0]["metric"] == "coal_production"
    assert body["state"] == "draft"
    # And it is listed afterwards, which is the whole point of storing it.
    listed = client.get("/api/reports").json()
    assert [item["report_id"] for item in listed] == [body["report_id"]]


def test_an_incomplete_report_is_still_created_with_its_reasons(client) -> None:
    """A manifest naming what it could not pin is an answer, not an error."""
    response = _generate(client)

    assert response.status_code == 201
    body = response.json()
    assert body["figures"] == []
    assert body["missing_required"][0]["metric"] == "coal_production"
    assert body["missing_required"][0]["reason"] == "out_of_corpus"


def test_an_unknown_entity_is_refused_with_the_reason(client) -> None:
    response = _generate(client, entity="atlantis")

    assert response.status_code == 422
    assert "atlantis" in response.json()["detail"]


def test_a_viewer_cannot_generate_a_report(viewer_client) -> None:
    assert _generate(viewer_client).status_code == 403


def test_a_viewer_cannot_move_a_report_through_the_lifecycle(
    client, viewer_client, store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    report_id = _generate(client).json()["report_id"]

    refused = viewer_client.post(
        f"/api/reports/{report_id}/transition", json={"state": "in_review"}
    )
    assert refused.status_code == 403


def test_skipping_review_is_a_409_naming_what_is_allowed(
    client, store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    report_id = _generate(client).json()["report_id"]

    response = client.post(
        f"/api/reports/{report_id}/transition", json={"state": "approved"}
    )

    assert response.status_code == 409
    assert "in_review" in response.json()["detail"]


def test_a_report_runs_the_full_lifecycle_over_http(
    client, store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    report_id = _generate(client).json()["report_id"]

    for state in ("in_review", "approved", "published"):
        response = client.post(
            f"/api/reports/{report_id}/transition", json={"state": state}
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == state

    # Published is terminal.
    frozen = client.post(
        f"/api/reports/{report_id}/transition", json={"state": "in_review"}
    )
    assert frozen.status_code == 409


def test_an_unknown_report_is_a_404(client) -> None:
    assert client.get("/api/reports/rpt_nope").status_code == 404
    assert client.get("/api/reports/rpt_nope/diff").status_code == 404


@pytest.mark.parametrize(
    ("fmt", "member"),
    [
        ("docx", "word/document.xml"),
        ("xlsx", "xl/workbook.xml"),
        ("pptx", "ppt/presentation.xml"),
    ],
)
def test_download_returns_a_real_office_file(
    client, store: Store, make_fact, fmt: str, member: str
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    report_id = _generate(client).json()["report_id"]

    response = client.get(f"/api/reports/{report_id}/download", params={"fmt": fmt})

    assert response.status_code == 200
    assert f".{fmt}" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert member in archive.namelist()


def test_downloading_an_incomplete_report_is_refused(client) -> None:
    """§11.2 — the render fails loudly and names the field."""
    report_id = _generate(client).json()["report_id"]

    response = client.get(f"/api/reports/{report_id}/download", params={"fmt": "docx"})

    assert response.status_code == 409
    assert "coal_production" in response.json()["detail"]


def test_diff_reports_nothing_moved_for_a_fresh_report(
    client, store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    report_id = _generate(client).json()["report_id"]

    deltas = client.get(f"/api/reports/{report_id}/diff").json()

    assert deltas
    assert all(delta["changed"] is False for delta in deltas)


# -------------------------------------------------------------------- topics


def _ingest(store: Store, make_document, filename: str, entity: str, *texts: str):
    document = make_document(filename, publisher_entity_id=entity)
    store.register_document(document)
    store.insert_evidence(
        [
            {
                "evidence_id": f"ev_{document.document_id}_{index}",
                "document_id": document.document_id,
                "document_version": 1,
                "page": 1,
                "kind": "line",
                "text": text,
                "extraction_method": ExtractionMethod.PDF_TEXT_LAYER.value,
            }
            for index, text in enumerate(texts)
        ]
    )
    return document


def test_extraction_then_cloud_then_drill_through(
    client, store: Store, make_document
) -> None:
    """The §12 gate, over HTTP: every term in the cloud reaches a document."""
    document = _ingest(
        store, make_document, "gevra.pdf", "secl", "Gevra Gevra dragline dragline"
    )

    extracted = client.post("/api/topics/extract")
    assert extracted.status_code == 200
    assert extracted.json()["terms_written"] > 0

    cloud = client.get("/api/topics/cloud").json()
    assert cloud
    assert {term["term"] for term in cloud} >= {"gevra", "dragline"}

    for term in cloud:
        reached = client.get(f"/api/topics/{term['term']}/documents").json()
        assert reached, f"{term['term']} is a dead end"
        assert reached[0]["document_id"] == document.document_id


def test_a_viewer_cannot_trigger_extraction(viewer_client) -> None:
    assert viewer_client.post("/api/topics/extract").status_code == 403


def test_the_cloud_is_empty_before_extraction_runs(client) -> None:
    assert client.get("/api/topics/cloud").json() == []


def test_a_term_nobody_in_scope_used_returns_an_empty_list_not_a_404(
    client,
) -> None:
    """An empty list and a 404 must look the same from outside, or the cloud
    leaks which terms exist in corpora the caller cannot read."""
    response = client.get("/api/topics/talcher/documents")
    assert response.status_code == 200
    assert response.json() == []


def test_prevalence_is_served_per_fiscal_year(
    client, store: Store, make_document
) -> None:
    _ingest(store, make_document, "fy24.pdf", "secl", "Korba Korba Raigarh Raigarh")
    client.post("/api/topics/extract")

    series = client.get("/api/topics/korba/prevalence").json()

    assert series == [{"fiscal_year": "FY2024-25", "document_count": 1}]
