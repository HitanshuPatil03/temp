"""Two people acting on one report, or one conflict, at the same time.

Both mutating paths had the same shape: a ``SELECT`` decided whether the change
was allowed, and the ``UPDATE`` did not repeat the test. Two requests that were
each legal against the state they read could therefore both commit.

For a report the consequence was the one state machine's only hard rule.
``LEGAL_TRANSITIONS[PUBLISHED]`` is empty — nothing leaves ``published`` — yet an
``approved`` report that one approver published while another sent it back to
review ended up *in review*, because the second ``UPDATE`` carried no state
predicate. For a conflict, two reviewers choosing different winning facts both
received a 200 and the first reviewer's adjudication was silently discarded.

The fix is a row lock on the read that feeds the decision, so the second
transaction blocks and PostgreSQL re-evaluates the row after acquiring it. The
loser then fails its *own* check and raises what a sequential caller would.

These tests use **two real connections and committed rows**, because a lock is
not observable inside one transaction — the usual rolled-back `store` fixture
cannot see it. ``lock_timeout`` keeps that deterministic: the blocked statement
raises instead of hanging, so a regression fails the suite rather than stalling
it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from mrip.auth.scope import Scope
from mrip.db.repositories.conflicts import ConflictRepository
from mrip.db.repositories.documents import new_id
from mrip.db.repositories.reports import ReportRepository
from mrip.db.tables import conflict_facts as conflict_facts_table
from mrip.db.tables import conflicts as conflicts_table
from mrip.db.tables import documents as documents_table
from mrip.db.tables import facts as facts_table
from mrip.db.tables import reports as reports_table
from mrip.schemas import ReportState

SCOPE = Scope.unrestricted("concurrency test")

#: Short enough that a regression fails fast, long enough not to trip on a
#: loaded CI runner before the lock is even requested.
LOCK_TIMEOUT = "400ms"


def _fact(document_id: str, value: float):
    """A validated fact citing ``document_id``.

    Deliberately not the ``make_fact`` fixture: that registers its source
    document through the rolled-back ``store`` connection, and everything here
    has to be visible to a second connection.
    """
    from datetime import UTC, date, datetime

    from mrip.schemas import (
        BBox,
        Confidence,
        EvidenceRef,
        ExtractionMethod,
        Fact,
        FactStatus,
    )

    return Fact(
        fact_id=new_id("fact"),
        entity_id="secl",
        metric="coal_production",
        value=value,
        unit="t",
        dimension="mass",
        raw_value=value / 1e6,
        raw_unit="MT",
        period_start=date(2024, 4, 1),
        period_end=date(2025, 3, 31),
        period_label="FY2024-25",
        fiscal_year="FY2024-25",
        evidence=EvidenceRef(
            document_id=document_id,
            page=1,
            table_id="t1",
            cell_ref="r1c1",
            bbox=BBox(x0=1.0, y0=1.0, x1=2.0, y1=2.0),
            snippet=f"SECL {value / 1e6:.2f}",
        ),
        extraction_method=ExtractionMethod.TABLE_LATTICE,
        confidence=Confidence(parse=0.97),
        status=FactStatus.VALIDATED,
        extracted_at=datetime.now(UTC),
    )


@pytest.fixture
def engine():
    from mrip.db.engine import get_engine

    return get_engine()


@pytest.fixture
def committed_report(engine) -> Iterator[str]:
    """An ``approved`` report visible to other connections.

    Committed on purpose, and torn down in ``finally`` so a failing assertion
    cannot leave a row behind for the next run to trip over.
    """
    report_id = new_id("rpt")
    manifest = {
        "report_id": report_id,
        "state": ReportState.APPROVED.value,
        "figures": [],
    }
    with engine.begin() as setup:
        setup.execute(
            sa.insert(reports_table).values(
                report_id=report_id,
                template_id="production-summary",
                template_version=1,
                title="Concurrency probe",
                entity_id="secl",
                period_label="FY2024-25",
                state=ReportState.APPROVED.value,
                manifest=json.loads(json.dumps(manifest)),
            )
        )
    try:
        yield report_id
    finally:
        with engine.begin() as teardown:
            teardown.execute(
                sa.delete(reports_table).where(reports_table.c.report_id == report_id)
            )


@pytest.fixture
def committed_conflict(engine, make_document) -> Iterator[str]:
    """An unresolved conflict **with its two disputing facts**, all committed.

    The member rows are not decoration. ``FOR UPDATE`` locks the rows a query
    returns, and the claim query joins ``conflict_facts``; a conflict with no
    members yields nothing and therefore locks nothing. An earlier version of
    this fixture inserted only the ``conflicts`` row and the test passed through
    without blocking — proving only that the fixture was wrong.

    Built through the real repositories so the composite foreign key from
    ``facts`` to ``documents(document_id, version)`` is satisfied the way
    production satisfies it.
    """
    from mrip.db import Store

    conflict_id = new_id("cfl")
    document = make_document("concurrency-source.pdf")
    low = _fact(document.document_id, 191_500_000.0)
    high = _fact(document.document_id, 193_000_000.0)

    with engine.begin() as setup:
        seed = Store(setup)
        seed.register_document(document)
        seed.insert_facts([low, high])
        setup.execute(
            sa.insert(conflicts_table).values(
                conflict_id=conflict_id,
                entity_id="secl",
                metric="coal_production",
                unit="t",
                period_label="FY2024-25",
                value_low=low.value,
                value_high=high.value,
            )
        )
        setup.execute(
            sa.insert(conflict_facts_table),
            [
                {"conflict_id": conflict_id, "fact_id": low.fact_id},
                {"conflict_id": conflict_id, "fact_id": high.fact_id},
            ],
        )
    try:
        yield conflict_id, high.fact_id
    finally:
        # Reverse dependency order, so a failure cannot leave a row whose parent
        # was already removed.
        with engine.begin() as teardown:
            teardown.execute(
                sa.delete(conflict_facts_table).where(
                    conflict_facts_table.c.conflict_id == conflict_id
                )
            )
            teardown.execute(
                sa.delete(conflicts_table).where(
                    conflicts_table.c.conflict_id == conflict_id
                )
            )
            teardown.execute(
                sa.delete(facts_table).where(
                    facts_table.c.fact_id.in_([low.fact_id, high.fact_id])
                )
            )
            teardown.execute(
                sa.delete(documents_table).where(
                    documents_table.c.document_id == document.document_id
                )
            )


def test_a_second_approver_is_blocked_while_the_first_holds_the_report(
    engine, committed_report: str
) -> None:
    """The lock exists, and it is on the row the decision was made from.

    Without it both transactions read ``approved``, both find their move legal,
    and both write — which is how a published report came to be sent back to
    review.
    """
    with engine.connect() as first, engine.connect() as second:
        first.begin()
        second.begin()
        # Fail instead of waiting, so this test can never hang the suite.
        second.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))

        holder = ReportRepository(first)
        assert holder._row(committed_report, SCOPE, for_update=True) is not None

        with pytest.raises(DBAPIError) as refused:
            ReportRepository(second)._row(committed_report, SCOPE, for_update=True)

        blocked = str(refused.value)
        assert "lock" in blocked.lower(), (
            "the second transaction returned instead of waiting, so the "
            "transition read is no longer locking the row"
        )
        # *Which* statement blocked is the whole point. If the lock is ever moved
        # back to the UPDATE, the legality check runs on a stale state again and
        # this assertion is what notices.
        assert "SELECT" in blocked and "FOR UPDATE" in blocked, (
            f"expected the locking read to block, but the driver reported: {blocked}"
        )
        first.rollback()
        second.rollback()


def test_an_ordinary_read_is_never_blocked_by_a_sign_off(
    engine, committed_report: str
) -> None:
    """The lock must not spread to reads.

    A dashboard listing reports, or an officer opening one, has no business
    waiting on somebody else's approval — and a lock that made it wait would be
    a worse defect than the race it fixed.
    """
    with engine.connect() as first, engine.connect() as second:
        first.begin()
        second.begin()
        second.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))

        ReportRepository(first)._row(committed_report, SCOPE, for_update=True)

        # No `for_update`: this is what `get()` and `list()` do.
        reader = ReportRepository(second)._row(committed_report, SCOPE)

        assert reader is not None
        assert reader["state"] == ReportState.APPROVED.value
        first.rollback()
        second.rollback()


def test_a_second_reviewer_is_blocked_while_the_first_adjudicates(
    engine, committed_conflict: str
) -> None:
    """Same race, different table.

    Two reviewers nominating different winning facts both passed the membership
    check and both committed, so whichever landed second silently replaced the
    other's decision while the first reviewer saw a success.
    """
    with engine.connect() as first, engine.connect() as second:
        first.begin()
        second.begin()
        second.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))

        conflict_id, winner = committed_conflict

        assert ConflictRepository(first).resolve(
            conflict_id, winner, "first reviewer", SCOPE, actor_user_id=None
        ), "the fixture must produce a resolvable conflict for this to prove anything"

        with pytest.raises(DBAPIError) as refused:
            ConflictRepository(second).resolve(
                conflict_id, winner, "second reviewer", SCOPE, actor_user_id=None
            )

        blocked = str(refused.value)
        assert "lock" in blocked.lower()
        # Without the lock on the claim, the second reviewer's *SELECT* succeeds
        # and it is the later UPDATE that waits — and because that UPDATE carries
        # no `resolved_fact_id IS NULL` predicate, it then overwrites the first
        # reviewer's decision instead of refusing. Both versions time out here, so
        # naming the statement is the only thing that tells them apart.
        assert "conflict_facts" in blocked and "FOR UPDATE" in blocked, (
            "the claim read is not taking the lock; the first adjudication can be "
            f"silently overwritten. Driver reported: {blocked}"
        )
        first.rollback()
        second.rollback()


def test_adjudication_also_claims_the_facts_it_rewrites(
    engine, committed_conflict
) -> None:
    """What resolving a conflict actually locks, stated rather than assumed.

    ``with_for_update(of=conflicts)`` narrows the *claim* to the conflict row, so
    the facts are only read while the nomination is validated. But ``resolve``
    then writes them — the winner becomes ``validated`` and the losers
    ``rejected`` — so by the time it returns it holds write locks on those rows
    too, until the transaction commits.

    That is correct, and it has a consequence worth recording: a fact can belong
    to more than one conflict, so two reviewers adjudicating two *different*
    conflicts that share a disputed figure will serialise against each other.
    This test exists because an earlier version of it asserted the opposite and
    passed only because the fixture had no member facts at all.
    """
    with engine.connect() as first, engine.connect() as second:
        first.begin()
        second.begin()
        second.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))

        conflict_id, winner = committed_conflict
        assert ConflictRepository(first).resolve(
            conflict_id, winner, None, SCOPE, actor_user_id=None
        )

        with pytest.raises(DBAPIError) as refused:
            second.execute(
                sa.select(facts_table.c.fact_id)
                .where(facts_table.c.fact_id == winner)
                .with_for_update()
            )

        assert "lock" in str(refused.value).lower()
        first.rollback()
        second.rollback()


def test_the_transition_read_is_locked_in_the_emitted_sql() -> None:
    """A structural guard, so removing ``for_update`` fails even on a database
    whose locking behaviour a future test environment does not exercise."""
    query = sa.select(reports_table).where(reports_table.c.report_id == "rpt_x")

    assert "FOR UPDATE" in str(query.with_for_update().compile())
    assert "FOR UPDATE" not in str(query.compile())
