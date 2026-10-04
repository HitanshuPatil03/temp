"""Tests for the corpus integrity sweep.

A check that reports "all clear" when something is broken is worse than no check,
because it converts an unknown into a false assurance. So every test here breaks
one thing deliberately and asserts the sweep notices — and the first one asserts a
healthy corpus comes back clean, because a check that always fails gets ignored
within a week.

The blob store is the subject. It lives outside the database, so no foreign key
protects the link from a document row to its bytes, and the failures below are the
ones a real deployment hits: a database restored to a different point than the
blob directory, a volume that lost data, an operator reclaiming disk.
"""

from __future__ import annotations

import pytest

from mrip.blobs import get_blob_store
from mrip.db import Store
from mrip.integrity import verify_corpus


@pytest.fixture(autouse=True)
def isolated_blobs(tmp_path, monkeypatch):
    """A blob store of this test's own.

    Every other fixture rolls its database writes back, but the blob store is a
    real directory and the project's default one accumulates: a shared
    ``data/blobs`` already held a hundred-odd blobs from previous runs and the demo
    corpus, every one of them unreferenced once the test transaction rolled back.
    Measuring orphans against that store reported 122 and told us nothing.

    Pointing ``MRIP_BLOB_DIR`` at ``tmp_path`` makes the store's contents exactly
    what the test put there, which is the only way an assertion about *absence*
    means anything.
    """
    from mrip.config import get_settings

    monkeypatch.setenv("MRIP_BLOB_DIR", str(tmp_path / "blobs"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    yield
    get_settings.cache_clear()


@pytest.fixture
def ingested(store: Store, make_document):
    """A document whose bytes are actually in the blob store.

    Registered *and* stored, which is the normal state and the one the sweep must
    call clean. The content hash is the blob's name, so the two halves are linked
    by the hash rather than by a column anyone could get wrong.
    """
    payload = b"%PDF-1.7\nCoal production 193.00 MT\n"
    blob = get_blob_store().put(payload)
    document = make_document("annual-report.pdf", content_hash=blob.content_hash)
    document = document.model_copy(update={"blob_key": blob.content_hash})
    store.register_document(document)
    return document


def test_a_healthy_corpus_reports_clean(store: Store, ingested) -> None:
    """The baseline. A sweep that cries wolf is a sweep nobody runs."""
    report = verify_corpus(store)

    assert report.ok, report.describe()
    assert report.documents_checked >= 1
    assert report.findings == []
    assert "No problems found." in report.describe()


def test_missing_bytes_are_reported_with_the_document_that_lost_them(
    store: Store, ingested
) -> None:
    """The failure with no foreign key behind it.

    A document row survives a restore; its bytes may not. Every figure extracted
    from it still answers queries perfectly, and every citation on those figures is
    a dead end — which is discovered by whoever clicks through, usually under a
    deadline.
    """
    blobs = get_blob_store()
    blobs.path_for(ingested.content_hash).unlink()

    report = verify_corpus(store)

    assert not report.ok
    finding = next(f for f in report.findings if f.check == "MISSING EVIDENCE")
    assert ingested.document_id in finding.examples
    assert finding.count == 1
    # The remedy has to say the two are one backup unit, because the usual cause
    # is restoring them separately.
    assert "backup" in finding.remedy.lower()


def test_altered_bytes_are_only_caught_by_the_deep_sweep(store: Store, ingested) -> None:
    """Content addressing is what makes tamper detection possible at all: the name
    *is* the checksum, so no second copy is needed to verify the first.

    It costs a full read of the corpus, so it is opt-in — and this asserts both
    halves of that trade, because a `--deep` flag that silently did nothing would
    be the worst of the three outcomes.
    """
    path = get_blob_store().path_for(ingested.content_hash)
    # The store writes blobs read-only, which is the store doing its job: nothing
    # in the application can rewrite one. Tampering therefore has to come from
    # outside it — a root process, a restored volume, a corrupted disk — so the
    # test has to reach outside it too. Needing this chmod is the evidence that
    # the ordinary path cannot produce this state.
    path.chmod(0o644)
    path.write_bytes(b"%PDF-1.7\nCoal production 999.00 MT\n")

    shallow = verify_corpus(store)
    assert shallow.ok, "presence alone cannot see a changed byte, and must not claim to"
    assert shallow.blobs_hashed == 0

    deep = verify_corpus(store, deep=True)
    assert not deep.ok
    finding = next(f for f in deep.errors if f.check == "ALTERED EVIDENCE")
    assert ingested.document_id in finding.examples
    assert deep.blobs_hashed >= 1


def test_a_report_pinning_a_vanished_fact_is_reported(
    store: Store, make_fact, ingested
) -> None:
    """§11.3 says re-opening an approved report reproduces the figures as approved.

    A manifest renders from its own stored copy, so a fact disappearing underneath
    it does not stop it rendering — the report simply can no longer be reconciled
    against the corpus it claims to come from. That silence is the defect, so the
    sweep speaks for it.
    """
    import sqlalchemy as sa

    from mrip.auth.scope import Scope
    from mrip.db.tables import facts as facts_table
    from mrip.reports.generate import generate
    from mrip.reports.template import default_template
    from mrip.schemas import FactStatus

    scope = Scope.unrestricted("test")
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    manifest = generate(
        store, scope, default_template(), entity="secl", period="FY2024-25"
    )
    store.reports.create(manifest, entity_id="secl", period_label="FY2024-25")
    assert manifest.figures, "the fixture must pin at least one figure to test this"

    # Nothing in the application deletes a fact; this is what a database restored
    # to a point before the report was generated looks like from the sweep's side.
    store.connection.execute(
        sa.delete(facts_table).where(facts_table.c.fact_id == manifest.figures[0].fact_id)
    )

    report = verify_corpus(store)

    assert not report.ok
    finding = next(f for f in report.findings if f.check == "UNRECONCILABLE REPORT")
    assert manifest.report_id in finding.examples[0]
    assert report.reports_checked >= 1
    assert report.figures_checked >= 1


def test_an_unreferenced_blob_is_reported_but_never_deleted(
    store: Store, ingested
) -> None:
    """Residue, not damage — but it is the fingerprint of a partial rollback.

    The sweep must not tidy it away. The store is append-only, and code that
    deletes evidence on its own authority would be the most dangerous thing in this
    repository; an operator deciding whether a restore finished needs the signal,
    not a cleanup.
    """
    orphan = get_blob_store().put(b"%PDF-1.7\nnothing references this\n")

    report = verify_corpus(store)

    # A warning, so the sweep still reports sound: nothing points at these bytes,
    # and failing a cron check on residue is how a check stops being read.
    assert report.ok, report.describe()
    finding = next(f for f in report.warnings if f.check == "UNREFERENCED BLOBS")
    assert finding.severity == "warning"
    assert orphan.content_hash in finding.examples
    assert report.orphan_blobs == 1
    # Still there afterwards. That is the assertion that matters.
    assert get_blob_store().exists(orphan.content_hash)


def test_every_problem_carries_something_to_do_about_it(store: Store, ingested) -> None:
    """Whoever runs this is usually mid-incident.

    "17 missing blobs" without a next step has made their evening worse rather than
    better, so the shape of a finding is part of the contract.
    """
    get_blob_store().path_for(ingested.content_hash).unlink()
    get_blob_store().put(b"%PDF-1.7\norphan\n")

    report = verify_corpus(store)

    assert len(report.findings) >= 2
    for finding in report.findings:
        assert finding.check.isupper(), "the heading should be scannable"
        assert len(finding.detail) > 40, f"{finding.check} does not explain itself"
        assert len(finding.remedy) > 40, f"{finding.check} does not say what to do"
        assert finding.count >= 1
    # And the whole thing reads as one report rather than a stack of exceptions.
    rendered = report.describe()
    for finding in report.findings:
        assert finding.check in rendered
        assert finding.remedy in rendered


def test_the_sweep_reports_everything_wrong_at_once(store: Store, ingested) -> None:
    """Not just the first thing.

    An operator after a restore needs the whole picture to decide whether to roll
    forward or back. A sweep that raised on the first missing blob would make them
    discover the rest one run at a time.
    """
    get_blob_store().path_for(ingested.content_hash).unlink()
    get_blob_store().put(b"%PDF-1.7\norphan one\n")
    get_blob_store().put(b"%PDF-1.7\norphan two\n")

    report = verify_corpus(store)

    checks = {finding.check for finding in report.findings}
    assert "MISSING EVIDENCE" in checks
    assert "UNREFERENCED BLOBS" in checks
    assert report.orphan_blobs == 2


def test_examples_are_capped_so_a_wholly_empty_store_stays_readable(
    store: Store, make_document
) -> None:
    """A blob directory that lost everything must not print a line per document
    before the summary an operator is looking for."""
    blobs = get_blob_store()
    for index in range(12):
        blob = blobs.put(f"%PDF-1.7\ndocument {index}\n".encode())
        document = make_document(f"report-{index}.pdf", content_hash=blob.content_hash)
        store.register_document(
            document.model_copy(update={"blob_key": blob.content_hash})
        )
        blobs.path_for(blob.content_hash).unlink()

    report = verify_corpus(store, examples=3)

    finding = next(f for f in report.findings if f.check == "MISSING EVIDENCE")
    assert len(finding.examples) == 3
    assert finding.count == 12
    assert "+9 more" in report.describe()
