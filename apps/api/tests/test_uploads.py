"""Tests for the upload route's revision handling and retry.

The boundary itself — magic numbers, size caps, zip bombs, encrypted PDFs — is
covered in ``test_intake.py``. What is under test here is the half only the route
can get wrong.

**Corrections.** CIL reissues statements. A provisional monthly figure is replaced
by the audited annual one, and the second document is not a disagreement between
two sources — it is the organisation revising itself. Without naming what it
replaces, both versions sit active and the conflict radar flags the revision as a
conflict, which is precisely the false positive that would teach a reviewer to
ignore the radar.

**Retry.** A failed document has to be recoverable from any stage, because the
cause decides where to restart: a transient OOM means re-run the stage that died,
a misclassified spreadsheet means go back to ``classify`` and do it all again.
The web UI offers all six, so all six have to work.
"""

from __future__ import annotations

import pymupdf
import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_read_only_store, provide_store
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db import Store
from mrip.ingest.lifecycle import STAGES
from mrip.main import create_app
from mrip.schemas import DocumentState, FactStatus, Role

SCOPE = Scope.unrestricted("test suite")


@pytest.fixture
def client(store: Store, make_user, bearer) -> TestClient:
    """An officer with an HQ-wide grant — the role uploading requires."""
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store
    operator = make_user("upload.tests", role=Role.OFFICER, entities=(SCOPE_ALL,))
    with TestClient(app, headers=bearer(operator)) as test_client:
        yield test_client


def _pdf_bytes(tmp_path, text: str) -> bytes:
    """A real one-page PDF. The route sniffs magic numbers, so this cannot be
    a stub — the bytes have to actually be a PDF."""
    path = tmp_path / "upload.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), text)
    document.save(path)
    document.close()
    return path.read_bytes()


def _upload(client: TestClient, payload: bytes, **form: str):
    return client.post(
        "/api/documents",
        files={"file": ("statement.pdf", payload, "application/pdf")},
        data=form,
    )


def test_a_correction_retires_the_version_it_replaces(
    client, store: Store, tmp_path, make_fact
) -> None:
    """Naming the replaced document moves its facts to ``superseded``.

    Superseded rather than deleted: "what did the earlier version say?" stays
    answerable, which is what the revision chain is for — it just stops being
    offered as the answer.
    """
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    original = store.query_facts(SCOPE, status=FactStatus.VALIDATED)[0]
    source_document_id = original.evidence.document_id

    response = _upload(
        client,
        _pdf_bytes(tmp_path, "Coal production 198.00 MT audited"),
        supersedes=source_document_id,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["supersedes"] == source_document_id
    assert body["facts_superseded"] == 1

    # The old figure is retired but still there.
    assert store.query_facts(SCOPE, status=FactStatus.VALIDATED) == []
    retired = store.query_facts(
        SCOPE, status=FactStatus.SUPERSEDED, include_inactive=True
    )
    assert [fact.fact_id for fact in retired] == [original.fact_id]

    # And the chain records which version replaced which.
    replacement = store.get_document(body["document_id"], SCOPE)
    assert replacement is not None
    assert replacement.supersedes == source_document_id


def test_superseding_a_document_that_does_not_exist_is_refused(client, tmp_path) -> None:
    """The upload is refused outright rather than succeeding with the
    supersession silently dropped — which would leave two active versions and no
    record that anyone intended otherwise."""
    response = _upload(
        client,
        _pdf_bytes(tmp_path, "Coal production 198.00 MT"),
        supersedes="doc_nope",
    )

    assert response.status_code == 404
    assert "doc_nope" in response.json()["detail"]


def test_superseding_a_document_outside_your_scope_is_a_404_not_a_403(
    store: Store, make_user, bearer, tmp_path, make_document
) -> None:
    """A 403 would confirm the document exists. Scope refusals are 404 here for
    the same reason they are everywhere else in this API."""
    hidden = make_document("mcl-statement.pdf", publisher_entity_id="mcl")
    store.register_document(hidden)

    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store
    secl_only = make_user("secl.officer", role=Role.OFFICER, entities=("secl",))
    with TestClient(app, headers=bearer(secl_only)) as scoped:
        response = _upload(
            scoped,
            _pdf_bytes(tmp_path, "Coal production 198.00 MT"),
            supersedes=hidden.document_id,
        )

    assert response.status_code == 404


def test_an_ordinary_upload_supersedes_nothing(client, tmp_path) -> None:
    """The common case stays the common case: no form field, no revision."""
    response = _upload(client, _pdf_bytes(tmp_path, "Coal production 193.00 MT"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["supersedes"] is None
    assert body["facts_superseded"] == 0


# ------------------------------------------------------------------- retry


def _failed_document(store: Store, make_document, *, at: str = "digitize"):
    """A document that failed at a named stage, as a dead job would leave it."""
    document = make_document(f"failed-at-{at}.pdf")
    store.register_document(document)
    store.documents.set_state(
        document.document_id,
        DocumentState.FAILED,
        failed_stage=at,
        failed_reason="Worker ran out of memory on page 312.",
    )
    return document


@pytest.mark.parametrize("stage", [item.name for item in STAGES])
def test_a_failed_document_can_be_re_run_from_any_stage(
    client, store: Store, make_document, stage: str
) -> None:
    """Every one of the six stages has to be a legal restart point.

    The cause of the failure decides where to resume: a transient OOM means
    re-run the stage that died, but a spreadsheet the classifier read as a PDF
    has to go back to ``classify`` — restarting at the extractor would just read
    the wrong shape again. The web UI offers all six, so all six must work;
    parametrised rather than written once because a lifecycle table that forbids
    one of them should fail here and not in front of an officer.
    """
    document = _failed_document(store, make_document)

    response = client.post(
        f"/api/documents/{document.document_id}/retry", json={"stage": stage}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stage"] == stage
    assert body["job_id"]
    # The document is moved to the state *before* the named stage, so the normal
    # chain carries it forward from there.
    assert body["state"] != DocumentState.FAILED.value

    reloaded = store.get_document(document.document_id, SCOPE)
    assert reloaded is not None
    assert reloaded.state.value == body["state"]
    # Advancing past a failure clears it: a retried document must not keep
    # showing the error that is no longer true of it.
    assert reloaded.failed_stage is None
    assert reloaded.failed_reason is None


def test_a_retry_names_the_stages_when_the_one_asked_for_is_unknown(
    client, store: Store, make_document
) -> None:
    """A typo is answered with the list, not with 'invalid stage'."""
    document = _failed_document(store, make_document)

    response = client.post(
        f"/api/documents/{document.document_id}/retry", json={"stage": "digitise"}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_stage"
    assert "digitize" in detail["stages"]


def test_a_quarantined_document_cannot_be_re_run(
    client, store: Store, make_document
) -> None:
    """Whatever made the bytes unsafe is still true of them.

    The UI does not offer the button for a quarantined document; this is the
    backstop that makes that a guarantee rather than a UI convention.
    """
    document = make_document("bomb.zip")
    store.register_document(document)
    store.documents.set_state(document.document_id, DocumentState.QUARANTINED)

    response = client.post(
        f"/api/documents/{document.document_id}/retry", json={"stage": "classify"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "illegal_transition"


def test_retrying_a_document_outside_your_scope_is_a_404(
    store: Store, make_user, bearer, make_document
) -> None:
    """Same rule as everywhere else: a 403 would confirm it exists."""
    hidden = _failed_document(store, make_document)
    store.documents.set_publisher(hidden.document_id, "mcl")

    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store
    secl_only = make_user("secl.only", role=Role.OFFICER, entities=("secl",))
    with TestClient(app, headers=bearer(secl_only)) as scoped:
        response = scoped.post(
            f"/api/documents/{hidden.document_id}/retry", json={"stage": "classify"}
        )

    assert response.status_code == 404


# ------------------------------------------------- the extraction summary


def test_the_progress_endpoint_explains_a_thin_extraction(
    client, store: Store, make_document
) -> None:
    """ "180 pages, four figures — why?" is the question this answers.

    The extractor already records what it refused, a few verbatim examples, and
    which reasons a person can act on. Before this the endpoint returned the raw
    tally and nothing read it, so the distinction between a refusal a reviewer can
    fix and ordinary table furniture was computed and discarded.
    """
    document = make_document("thin-report.pdf")
    store.register_document(document)
    store.documents.set_state(
        document.document_id,
        DocumentState.EXTRACTED,
        progress={
            "extract": {
                "done": 4,
                "total": 56,
                "tables": 7,
                "skipped": {
                    "no_unit": 37,
                    "ambiguous_unit": 3,
                    "total_row": 12,
                },
                "examples": {
                    "no_unit": ["Coal production | 193.00", "Offtake | 188.40"],
                    "total_row": ["Total"],
                },
            }
        },
    )

    body = client.get(f"/api/documents/{document.document_id}/progress").json()
    extraction = body["extraction"]

    assert extraction["facts"] == 4
    assert extraction["candidates"] == 56
    assert extraction["tables"] == 7

    # Biggest refusal first, labelled for a human, with the cells themselves.
    attention = extraction["needs_attention"]
    assert [item["reason"] for item in attention] == ["no_unit", "ambiguous_unit"]
    assert attention[0]["count"] == 37
    assert "unit" in attention[0]["label"].lower()
    assert "Coal production | 193.00" in attention[0]["examples"]

    # A refused total row is the extractor working correctly, so it is reported
    # separately and must not read as a problem.
    assert [item["reason"] for item in extraction["ignored"]] == ["total_row"]
    assert extraction["ignored"][0]["examples"] == []
    # Validation has not run on this document, and "not validated yet" is a
    # different answer from "nothing was accepted". Only one of them is a
    # problem, so the field is null rather than 0.
    assert extraction["accepted"] is None


def test_the_progress_endpoint_says_how_many_figures_are_usable(
    client, store: Store, make_document
) -> None:
    """ "22 of 26 cells became figures" still leaves the officer's real question.

    Extraction count and acceptance count answer different things: how much the
    reader found, against how much a report may pin. They diverge whenever a
    figure was flagged for review or is in conflict — and while acceptance did
    not exist at all, the answer was always "none" with nothing saying so, which
    is most of why that defect stayed invisible.
    """
    document = make_document("validated-report.pdf")
    store.register_document(document)
    store.documents.set_state(
        document.document_id,
        DocumentState.EXTRACTED,
        progress={"extract": {"done": 6, "total": 9, "tables": 2}},
    )
    store.documents.set_state(
        document.document_id,
        DocumentState.VALIDATED,
        progress={"validate": {"done": 0, "total": 0, "accepted": 4}},
    )

    extraction = client.get(f"/api/documents/{document.document_id}/progress").json()[
        "extraction"
    ]

    assert extraction["facts"] == 6
    assert extraction["accepted"] == 4


def test_a_clean_extraction_reports_nothing_to_attend_to(
    client, store: Store, make_document
) -> None:
    """An empty list and "has not run yet" are different answers.

    "Extraction refused nothing" is a real and reassuring statement about a clean
    document. Collapsing it into the same shape as "extraction has not happened"
    would hide it.
    """
    document = make_document("clean-report.pdf")
    store.register_document(document)

    before = client.get(f"/api/documents/{document.document_id}/progress").json()
    assert before["extraction"] is None

    store.documents.set_state(
        document.document_id,
        DocumentState.EXTRACTED,
        progress={"extract": {"done": 149, "total": 149, "tables": 7, "skipped": {}}},
    )

    after = client.get(f"/api/documents/{document.document_id}/progress").json()
    assert after["extraction"]["needs_attention"] == []
    assert after["extraction"]["ignored"] == []
    assert after["extraction"]["facts"] == 149


def test_progress_marks_which_stages_are_behind_the_current_state(
    client, store: Store, make_document
) -> None:
    """The checklist an officer reads while waiting."""
    document = make_document("midway.pdf")
    store.register_document(document)
    store.documents.set_state(document.document_id, DocumentState.DIGITIZED)

    body = client.get(f"/api/documents/{document.document_id}/progress").json()
    done = {stage["name"]: stage["completed"] for stage in body["stages"]}

    assert done["classify"] is True
    assert done["digitize"] is True
    assert done["extract"] is False
    assert done["index"] is False
    # Every stage carries its own description, so the checklist reads without a
    # glossary.
    assert all(stage["description"] for stage in body["stages"])
