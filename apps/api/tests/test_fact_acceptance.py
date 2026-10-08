"""A fact that passes every check has to be usable.

Demotion was implemented three times — at creation for a fuzzy entity or an
ambiguous unit, per document at the normalize stage, and corpus-wide from the
scheduler. Nothing promoted. So a fact that cleared every automated check sat in
``extracted`` for good, and ``extracted`` is a state nothing consumes:
``is_trustworthy`` admits only ``validated``, so the figure could not be pinned
in a report or charted, and it was not in the review queue either, because that
queue is ``needs_review``.

The result was backwards. A clean, unambiguous statement produced figures that
disappeared — the generator refused with ``no_validated_fact`` while a reviewer
saw an empty queue — and the only route to ``validated`` was to *disagree* with
another figure and have someone pick between them. On the live development
corpus this read as 23 of 25 facts stuck in ``extracted``, the single
``validated`` one having got there by winning a conflict.

So the tests here are mostly about what must **not** be promoted. Accepting a
figure automatically is the kind of thing that is correct until it is applied to
the one fact it should never touch.
"""

from __future__ import annotations

import pytest

from mrip.auth.scope import Scope
from mrip.db import Store
from mrip.schemas import Confidence, FactStatus

SCOPE = Scope.unrestricted("acceptance test")
THRESHOLD = 0.80


@pytest.fixture
def promote(store: Store, make_document):
    """Run the acceptance sweep over one document, returning how many it took."""
    document = make_document("acceptance-source.pdf")
    store.register_document(document)

    def _run() -> int:
        return store.facts.promote_high_confidence_for_document(
            document.document_id, document.version, THRESHOLD
        )

    _run.document = document  # type: ignore[attr-defined]
    return _run


def _fact(make_fact, document, **overrides):
    """A fact citing ``document``, extracted and unambiguous unless told otherwise."""
    defaults: dict[str, object] = {
        "document_id": document.document_id,
        "status": FactStatus.EXTRACTED,
        "unit_ambiguous": False,
        "confidence": Confidence(parse=0.95),
    }
    return make_fact(**{**defaults, **overrides})


def test_a_clean_figure_becomes_usable(store: Store, make_fact, promote) -> None:
    """The defect, stated as the behaviour that was missing.

    Before this, the assertion below was ``extracted`` — and a report generated
    from this document refused for want of a validated fact.
    """
    fact = _fact(make_fact, promote.document)
    store.insert_facts([fact])

    assert promote() == 1

    stored = store.facts.get(fact.fact_id, SCOPE)
    assert stored is not None
    assert stored.status is FactStatus.VALIDATED
    assert stored.status.is_trustworthy, "the whole point is that a report can pin it"


def test_a_low_confidence_figure_is_left_alone(store: Store, make_fact, promote) -> None:
    """The threshold is re-tested here rather than assumed from the normalize
    stage, so the sweep is safe whichever order the stages ran in."""
    fact = _fact(make_fact, promote.document, confidence=Confidence(parse=0.4))
    store.insert_facts([fact])

    assert promote() == 0

    stored = store.facts.get(fact.fact_id, SCOPE)
    assert stored is not None
    assert stored.status is FactStatus.EXTRACTED


def test_a_figure_with_an_ambiguous_unit_is_never_accepted(
    store: Store, make_fact, promote
) -> None:
    """``MT`` is million tonnes in CIL reporting and a metric tonne everywhere
    else — a factor of a million. Extraction already routes an ambiguous unit to
    review, so this is the second lock on the one error that would put a figure a
    million times too large into a Ministry report."""
    fact = _fact(make_fact, promote.document, unit_ambiguous=True)
    store.insert_facts([fact])

    assert promote() == 0

    stored = store.facts.get(fact.fact_id, SCOPE)
    assert stored is not None
    assert stored.status is FactStatus.EXTRACTED


@pytest.mark.parametrize(
    "untouchable",
    [
        FactStatus.NEEDS_REVIEW,
        FactStatus.CONFLICTED,
        FactStatus.REJECTED,
        FactStatus.SUPERSEDED,
    ],
)
def test_only_extracted_facts_are_considered(
    store: Store, make_fact, promote, untouchable: FactStatus
) -> None:
    """Every other state is somebody's decision, or a stronger flag.

    ``needs_review`` and ``rejected`` are human judgements and must not be
    overturned by a confidence number; ``conflicted`` outranks confidence,
    because two sources disagreeing is a better reason to doubt a figure than a
    parser's score is to trust it.
    """
    fact = _fact(make_fact, promote.document, status=untouchable)
    store.insert_facts([fact])

    assert promote() == 0

    stored = store.facts.get(fact.fact_id, SCOPE)
    assert stored is not None
    assert stored.status is untouchable


def test_a_figure_with_no_confidence_at_all_is_left_alone(
    store: Store, make_fact, promote
) -> None:
    """``least()`` over three NULLs is NULL, and NULL fails the comparison.

    Relying on that is subtle enough to be worth a test: a fact whose stages all
    declined to report a confidence has not passed the bar, it just never took
    the exam.
    """
    fact = _fact(make_fact, promote.document, confidence=Confidence())
    store.insert_facts([fact])

    assert promote() == 0

    stored = store.facts.get(fact.fact_id, SCOPE)
    assert stored is not None
    assert stored.status is FactStatus.EXTRACTED


def test_the_sweep_stays_inside_the_document_it_was_given(
    store: Store, make_fact, make_document, promote
) -> None:
    """Re-validating one filing must not accept another's figures.

    The stage is per document and idempotent by design; a sweep that reached
    across the corpus would make a retry of one upload silently change the status
    of facts from unrelated ones.
    """
    other = make_document("another-filing.pdf")
    store.register_document(other)
    mine = _fact(make_fact, promote.document)
    theirs = _fact(make_fact, other)
    store.insert_facts([mine, theirs])

    assert promote() == 1

    untouched = store.facts.get(theirs.fact_id, SCOPE)
    assert untouched is not None
    assert untouched.status is FactStatus.EXTRACTED


def test_running_it_twice_changes_nothing_the_second_time(
    store: Store, make_fact, promote
) -> None:
    """Every stage in this pipeline is idempotent; this one has to be too."""
    store.insert_facts([_fact(make_fact, promote.document)])

    assert promote() == 1
    assert promote() == 0


def test_the_validate_stage_accepts_what_it_validated(
    store: Store, make_fact, make_document
) -> None:
    """End to end through the real stage, because a correct sweep nobody calls
    is exactly the state this started in.

    Also asserts the count reaches the stage progress. That number is what the
    document page shows an officer, and its absence is part of why the old
    behaviour was invisible: the pipeline reported six green stages and the
    figures were still unusable.
    """
    from mrip.ingest.pipeline import validate_document
    from mrip.jobs.queue import JobQueue
    from mrip.jobs.registry import JobContext
    from mrip.schemas import DocumentState

    document = make_document("validated-filing.pdf")
    store.register_document(document)
    store.documents.set_state(document.document_id, DocumentState.NORMALIZED)
    store.insert_facts([_fact(make_fact, document)])

    job = (
        JobQueue(store.connection)
        .enqueue("document.validate", {"document_id": document.document_id})
        .job
    )
    validate_document(
        JobContext(job=job, store=store, worker_id="w-test", lease_seconds=60)
    )

    stored = store.get_document(document.document_id, SCOPE)
    assert stored is not None
    assert stored.stage_progress["validate"]["accepted"] == 1

    facts = store.query_facts(SCOPE, document_id=document.document_id)
    assert [fact.status for fact in facts] == [FactStatus.VALIDATED]


# ------------------------------------------------- the standing corpus-wide sweep


def test_a_figure_left_behind_by_a_cleared_conflict_is_accepted_again(
    store: Store, make_fact, make_document
) -> None:
    """The path the per-document sweep cannot reach.

    When a disagreement clears, ``_reconcile_flags`` returns the surviving fact
    to ``extracted`` rather than to ``validated`` — deliberately, because
    clearing a conflict is not a vouch for the figure. Its comment says "the
    confidence sweep decides whether it still needs review", and the sweep only
    ever demoted, so the survivor dropped back into limbo and only a re-run of
    its document's validate stage could get it out.

    Here the rival is superseded, which is what a corrected filing does, so the
    group stops disagreeing and the radar drops it.
    """
    document = make_document("cleared-conflict.pdf")
    store.register_document(document)
    low = _fact(make_fact, document, value=191_500_000.0)
    high = _fact(make_fact, document, value=193_000_000.0)
    store.insert_facts([low, high])

    # Two distinct values for one entity/metric/period: a conflict.
    assert len(store.detect_conflicts(SCOPE)) == 1
    assert store.facts.get(high.fact_id, SCOPE).status is FactStatus.CONFLICTED  # type: ignore[union-attr]

    # A corrected filing retires the rival, so the disagreement stops existing.
    store.facts.set_status([low.fact_id], FactStatus.SUPERSEDED)
    assert store.detect_conflicts(SCOPE) == []

    survivor = store.facts.get(high.fact_id, SCOPE)
    assert survivor is not None
    assert survivor.status is FactStatus.EXTRACTED, (
        "the radar is expected to hand the survivor back to the pipeline; if this "
        "changes, the standing sweep below may no longer be the thing that rescues it"
    )

    assert store.promote_high_confidence(THRESHOLD) == 1

    accepted = store.facts.get(high.fact_id, SCOPE)
    assert accepted is not None
    assert accepted.status is FactStatus.VALIDATED


def test_the_corpus_sweep_respects_the_same_three_conditions(
    store: Store, make_fact, make_document
) -> None:
    """One test for all three, because the corpus-wide version is the one that
    could quietly accept something nobody looked at."""
    document = make_document("corpus-sweep.pdf")
    store.register_document(document)
    clean = _fact(make_fact, document)
    doubtful = _fact(make_fact, document, confidence=Confidence(parse=0.3))
    ambiguous = _fact(make_fact, document, unit_ambiguous=True)
    reviewed = _fact(make_fact, document, status=FactStatus.NEEDS_REVIEW)
    store.insert_facts([clean, doubtful, ambiguous, reviewed])

    assert store.promote_high_confidence(THRESHOLD) == 1

    statuses = {
        fact.fact_id: fact.status
        for fact in store.query_facts(SCOPE, document_id=document.document_id)
    }
    assert statuses[clean.fact_id] is FactStatus.VALIDATED
    assert statuses[doubtful.fact_id] is FactStatus.EXTRACTED
    assert statuses[ambiguous.fact_id] is FactStatus.EXTRACTED
    assert statuses[reviewed.fact_id] is FactStatus.NEEDS_REVIEW


def test_the_scheduler_runs_the_acceptance_sweep() -> None:
    """A sweep nobody schedules is the shape the original defect had."""
    from mrip.jobs.scheduler import SCHEDULE

    kinds = {task.kind for task in SCHEDULE}
    assert "maintenance.accept_high_confidence" in kinds
    assert "maintenance.flag_low_confidence" in kinds, "both halves, or neither works"

    # Coprime intervals, so the sweeps do not pile onto the same workers.
    intervals = sorted(
        task.interval_seconds // 60
        for task in SCHEDULE
        if task.kind.startswith("maintenance.")
    )
    assert len(set(intervals)) == len(intervals), f"duplicate cadences: {intervals}"
