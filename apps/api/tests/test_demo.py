"""The demonstration corpus.

`mrip-admin seed-demo` is the first thing an evaluator runs, which makes it the
worst place for a quiet regression: a broken seed reads as a broken product. So
its two load-bearing properties are pinned here.

**It is idempotent.** Running it twice must not double the corpus. That is not
automatic — the documents are *regenerated* on each run, so the generator has to
be deterministic or the content hash changes and dedup cannot recognise a file it
has already ingested.

**It goes through the real pipeline.** The facts it produces carry evidence with
a page and a cell, because they were extracted rather than inserted. A seed that
wrote rows directly would demonstrate nothing.

These tests commit, unlike the rest of the suite: `seed_demo` opens its own
transactions, exactly as the worker does. They clean up after themselves.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from mrip.auth.scope import Scope
from mrip.db import store_session
from mrip.db.tables import METADATA
from mrip.demo import DEMO_ACCOUNTS, DEMO_PASSWORD, seed_demo
from mrip.schemas import DocumentState

SCOPE = Scope.unrestricted("demo corpus tests")


@pytest.fixture
def clean_database(database_url, monkeypatch, tmp_path):
    """An empty database and a throwaway blob store, restored afterwards.

    The blob store is redirected so a test run does not scatter bytes into the
    developer's real one.
    """
    from mrip import blobs
    from mrip.blobs import FilesystemBlobStore
    from mrip.ingest import pipeline

    store = FilesystemBlobStore(tmp_path / "blobs")
    monkeypatch.setattr(blobs, "get_blob_store", lambda: store)
    monkeypatch.setattr(pipeline, "get_blob_store", lambda: store)
    monkeypatch.setattr("mrip.demo.get_blob_store", lambda: store)

    def truncate() -> None:
        with store_session() as session:
            names = ", ".join(f'"{table}"' for table in reversed(METADATA.sorted_tables))
            session.connection.execute(
                sa.text("ALTER TABLE audit_log DISABLE TRIGGER USER")
            )
            session.connection.execute(sa.text(f"TRUNCATE {names} CASCADE"))
            session.connection.execute(
                sa.text("ALTER TABLE audit_log ENABLE TRIGGER USER")
            )

    truncate()
    yield
    truncate()


@pytest.mark.slow
def test_seeding_produces_a_corpus_a_reviewer_can_work_with(clean_database):
    """One command, and the system has something in it."""
    outcome = seed_demo()

    assert len(outcome["accounts"]) == len(DEMO_ACCOUNTS)
    assert len(outcome["documents"]) == 4
    counts = outcome["counts"]

    # Four subsidiaries across two fiscal years, twice (provisional and revised),
    # plus offtake and capacity — and *not* the total rows.
    assert counts["facts"] == 25
    assert counts["entities"] == 4
    assert counts["metrics"] == 3

    # The disagreement is real: the revision moves SECL's FY2024-25 figure.
    assert outcome["open_conflicts"] == 1


@pytest.mark.slow
def test_seeding_twice_does_not_duplicate_the_corpus(clean_database):
    """The property that a regenerated corpus threatens.

    Each run rebuilds the documents from scratch; if the generator emitted a
    fresh timestamp, every run would produce new bytes, a new content hash, and a
    second copy of the whole corpus.
    """
    first = seed_demo()
    second = seed_demo()

    assert second["documents"] == [], "the second run found everything already there"
    assert second["counts"]["documents"] == first["counts"]["documents"] == 4
    assert second["counts"]["facts"] == first["counts"]["facts"]


@pytest.mark.slow
def test_every_seeded_fact_carries_real_evidence(clean_database):
    """Proof that the seed extracted rather than inserted."""
    seed_demo()

    with store_session() as store:
        facts = store.query_facts(SCOPE, include_inactive=True, limit=100)

        assert facts
        for fact in facts:
            evidence = fact.evidence
            assert evidence.document_id
            assert evidence.cell_ref, "a figure must name the cell it came from"
            assert evidence.snippet, "and quote the source"
            # Canonical and raw are both kept: 193.00 printed, 193000000 t stored.
            assert fact.raw_value != fact.value or fact.unit == fact.raw_unit


@pytest.mark.slow
def test_every_seeded_document_is_flagged_synthetic(clean_database):
    """The one thing this platform must never do is let a made-up figure pass as
    a government source, and a seeded corpus is exactly where that could happen."""
    seed_demo()

    with store_session() as store:
        documents = store.list_documents(SCOPE)
        assert documents
        assert all(document.is_synthetic for document in documents)
        assert all(document.state is DocumentState.READY for document in documents)


@pytest.mark.slow
def test_the_demonstration_password_meets_the_real_policy():
    """It is published in the documentation, so it had better not be a weak one
    that the policy would have refused from a user."""
    from mrip.auth import passwords

    for username, *_ in DEMO_ACCOUNTS:
        passwords.check_password_policy(DEMO_PASSWORD, username=username)


@pytest.mark.slow
def test_seeded_accounts_can_actually_sign_in(clean_database):
    """The credentials printed by the command are the credentials that work."""
    from mrip.auth.service import authenticate

    seed_demo()

    with store_session() as store:
        for username, role, _entities, _description in DEMO_ACCOUNTS:
            outcome = authenticate(store, username, DEMO_PASSWORD)
            assert outcome.ok, f"{username} could not sign in"
            assert outcome.principal is not None
            assert outcome.principal.role is role
            assert not outcome.principal.must_change_password, (
                "a demonstration account must not demand a password change first"
            )


@pytest.mark.slow
def test_scope_is_visible_in_the_demo_corpus(clean_database):
    """`secl.officer` exists to make the access model checkable by signing in."""
    seed_demo()

    with store_session() as store:
        officer = store.users.by_username("secl.officer")
        assert officer is not None
        scope = store.users.scope_for(officer)

        facts = store.query_facts(scope, include_inactive=True, limit=100)
        assert facts, "the SECL officer sees SECL's figures"
        assert {fact.entity_id for fact in facts} == {"secl"}

        everything = store.query_facts(SCOPE, include_inactive=True, limit=100)
        assert len(everything) > len(facts), "and not the rest of the corpus"
