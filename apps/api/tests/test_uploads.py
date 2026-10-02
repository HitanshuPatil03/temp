"""Tests for the upload route's revision handling.

The boundary itself — magic numbers, size caps, zip bombs, encrypted PDFs — is
covered in ``test_intake.py``. What is under test here is the half only the route
can get wrong: what happens when an officer uploads a **correction**.

CIL reissues statements. A provisional monthly figure is replaced by the audited
annual one, and the second document is not a disagreement between two sources —
it is the organisation revising itself. Without naming what it replaces, both
versions sit active and the conflict radar flags the revision as a conflict, which
is precisely the false positive that would teach a reviewer to ignore the radar.
"""

from __future__ import annotations

import pymupdf
import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_read_only_store, provide_store
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db import Store
from mrip.main import create_app
from mrip.schemas import FactStatus, Role

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
