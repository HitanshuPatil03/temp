"""Tests for the job queue.

The queue's docstring makes four claims. This file exists to make them true
rather than aspirational, and each is written so that the obvious wrong
implementation fails it:

1. two workers never claim the same job (``SKIP LOCKED``);
2. attempts are counted at **claim** time, so a poison pill dead-letters;
3. a worker that lost its lease cannot report an outcome;
4. an expired lease returns the job to the queue.

Several tests here need **two concurrent transactions**, so they take their own
connections from the engine instead of using the ``store`` fixture's single
rolled-back one — a queue property about concurrency cannot be tested inside one
transaction. Those tests clean up after themselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa

from mrip.db.tables import jobs
from mrip.jobs.queue import JobQueue, PermanentJobError, backoff_seconds
from mrip.jobs.registry import get_handler, handler, registered_kinds
from mrip.schemas import JobState

WORKER_A = "host-a:1001"
WORKER_B = "host-b:2002"


@pytest.fixture
def queue(store) -> JobQueue:
    """A queue over the test's rolled-back transaction."""
    return JobQueue(store.connection)


@pytest.fixture
def isolated(database_url: str) -> Iterator[sa.Engine]:
    """An engine for tests that genuinely need concurrent transactions.

    Rows written through this fixture are committed, so it truncates `jobs` on
    the way out. Nothing else in the suite depends on queue rows surviving.
    """
    from mrip.db.engine import get_engine

    engine = get_engine()
    yield engine
    with engine.begin() as conn:
        conn.execute(sa.delete(jobs))


# --------------------------------------------------------------------- enqueue


def test_enqueue_returns_a_pending_job(queue):
    outcome = queue.enqueue("document.digitize", {"document_id": "doc_1"})

    assert outcome.created is True
    assert outcome.job.state is JobState.PENDING
    assert outcome.job.kind == "document.digitize"
    assert outcome.job.payload == {"document_id": "doc_1"}
    assert outcome.job.attempts == 0


def test_an_idempotency_key_collapses_duplicate_requests(queue):
    """Re-requesting the same work must not duplicate it.

    The upload path retries on a network failure; without this, one document
    would be digitized twice and every fact in it counted twice.
    """
    first = queue.enqueue("document.digitize", {"v": 1}, idempotency_key="digest:doc_1")
    second = queue.enqueue("document.digitize", {"v": 2}, idempotency_key="digest:doc_1")

    assert first.created is True
    assert second.created is False
    assert second.job.job_id == first.job.job_id
    # The original payload wins — the second request did not overwrite it.
    assert second.job.payload == {"v": 1}
    assert queue.depth()[JobState.PENDING.value] == 1


def test_jobs_without_a_key_are_never_collapsed(queue):
    """A nullable unique column permits many NULLs, which is what we rely on."""
    a = queue.enqueue("document.digitize", {"page": 1})
    b = queue.enqueue("document.digitize", {"page": 1})

    assert a.job.job_id != b.job.job_id
    assert queue.depth()[JobState.PENDING.value] == 2


def test_a_delayed_job_is_not_yet_claimable(queue):
    queue.enqueue("maintenance.prune_jobs", delay_seconds=3600)
    assert queue.claim(WORKER_A) == []


# ----------------------------------------------------------------------- claim


def test_claim_marks_the_job_and_counts_the_attempt(queue):
    enqueued = queue.enqueue("document.digitize", {"document_id": "doc_1"})

    [claimed] = queue.claim(WORKER_A, lease_seconds=60)

    assert claimed.job_id == enqueued.job.job_id
    assert claimed.state is JobState.CLAIMED
    assert claimed.claimed_by == WORKER_A
    assert claimed.lease_expires_at is not None
    # Counted at claim, not at failure — a job that kills its worker silently
    # still burns an attempt, so a poison pill cannot loop forever.
    assert claimed.attempts == 1


def test_higher_priority_is_claimed_first(queue):
    queue.enqueue("a", priority=0)
    urgent = queue.enqueue("b", priority=10)

    [claimed] = queue.claim(WORKER_A)
    assert claimed.job_id == urgent.job.job_id


def test_claim_respects_the_queue_filter(queue):
    queue.enqueue("ocr", queue="heavy")
    assert queue.claim(WORKER_A, queues=["default"]) == []
    assert len(queue.claim(WORKER_A, queues=["heavy"])) == 1


def test_two_workers_never_claim_the_same_job(isolated):
    """The property that removes the need for a broker.

    Both transactions run the claim statement before either commits. With
    ``SKIP LOCKED`` the second steps over the row the first locked; without it,
    the second would either block or hand out the same job twice.
    """
    with isolated.begin() as setup:
        JobQueue(setup).enqueue("document.digitize", {"document_id": "doc_1"})

    conn_a = isolated.connect()
    conn_b = isolated.connect()
    try:
        tx_a = conn_a.begin()
        tx_b = conn_b.begin()

        claimed_a = JobQueue(conn_a).claim(WORKER_A)
        # Still inside A's open transaction: the row is locked but uncommitted.
        claimed_b = JobQueue(conn_b).claim(WORKER_B)

        tx_a.commit()
        tx_b.commit()
    finally:
        conn_a.close()
        conn_b.close()

    assert len(claimed_a) == 1, "the first worker should get the job"
    assert claimed_b == [], "the second worker must skip the locked row, not wait"


def test_claim_is_bounded_by_limit(queue):
    for index in range(5):
        queue.enqueue("document.digitize", {"page": index})

    assert len(queue.claim(WORKER_A, limit=2)) == 2
    assert len(queue.claim(WORKER_A, limit=10)) == 3


# ------------------------------------------------------------------- lifecycle


def test_complete_marks_the_job_succeeded(queue):
    enqueued = queue.enqueue("document.digitize")
    queue.claim(WORKER_A)

    assert queue.complete(enqueued.job.job_id, WORKER_A) is True

    job = queue.get(enqueued.job.job_id)
    assert job.state is JobState.SUCCEEDED
    assert job.error is None


def test_a_worker_that_lost_its_lease_cannot_report_success(queue):
    """Otherwise a slow worker could overwrite the outcome of the one that won.

    This is the scenario: A claims, stalls past its lease, the scheduler reclaims
    the job, B claims and finishes it. A then wakes up and calls ``complete``. If
    that were allowed, A's stale view would mark a job succeeded whose work it
    never finished.
    """
    enqueued = queue.enqueue("document.digitize")
    queue.claim(WORKER_A)

    assert queue.complete(enqueued.job.job_id, WORKER_B) is False
    assert queue.get(enqueued.job.job_id).state is JobState.CLAIMED

    assert queue.fail(enqueued.job.job_id, WORKER_B, "not mine") is None
    assert queue.get(enqueued.job.job_id).state is JobState.CLAIMED


def test_heartbeat_extends_the_lease_and_reports_loss(queue, store):
    enqueued = queue.enqueue("document.digitize")
    [claimed] = queue.claim(WORKER_A, lease_seconds=60)
    first_expiry = claimed.lease_expires_at

    assert queue.heartbeat(enqueued.job.job_id, WORKER_A, lease_seconds=600) is True
    assert queue.get(enqueued.job.job_id).lease_expires_at > first_expiry

    assert queue.heartbeat(enqueued.job.job_id, WORKER_B) is False


def test_a_retryable_failure_returns_to_pending_with_a_delay(queue):
    enqueued = queue.enqueue("document.digitize", max_attempts=3)
    queue.claim(WORKER_A)

    state = queue.fail(enqueued.job.job_id, WORKER_A, "OCR timed out")

    assert state is JobState.PENDING
    job = queue.get(enqueued.job.job_id)
    assert job.error == "OCR timed out"
    assert job.claimed_by is None
    # Backed off, so it is not immediately re-claimable.
    assert queue.claim(WORKER_A) == []


def test_exhausted_attempts_dead_letter_rather_than_looping(queue):
    """A job that always fails must stop, and must be kept for inspection."""
    enqueued = queue.enqueue("document.digitize", max_attempts=2)
    job_id = enqueued.job.job_id

    queue.claim(WORKER_A)
    assert queue.fail(job_id, WORKER_A, "attempt 1") is JobState.PENDING

    # Clear the backoff so the second attempt is claimable now.
    queue._conn.execute(
        sa.update(jobs).where(jobs.c.job_id == job_id).values(available_at=sa.func.now())
    )
    queue.claim(WORKER_A)
    assert queue.fail(job_id, WORKER_A, "attempt 2") is JobState.DEAD

    dead = queue.get(job_id)
    assert dead.state is JobState.DEAD
    assert dead.error == "attempt 2"
    assert dead is not None, "a dead job is retained, never deleted"


def test_a_permanent_failure_skips_the_remaining_attempts(queue):
    """Retrying a malformed payload three times buries the real failures."""
    enqueued = queue.enqueue("document.digitize", max_attempts=5)
    queue.claim(WORKER_A)

    state = queue.fail(
        enqueued.job.job_id, WORKER_A, "payload has no document_id", permanent=True
    )

    assert state is JobState.FAILED
    job = queue.get(enqueued.job.job_id)
    assert job.attempts == 1, "attempts were not exhausted; the job was abandoned"
    assert job.state is JobState.FAILED


# ---------------------------------------------------------------- failure hooks


def test_terminal_failure_marks_document_failed(store, make_document):
    """When a document.* job dead-letters, the document must move to FAILED.

    Before this fix: a dead ingest job left the document in whatever intermediate
    state the pipeline last wrote (e.g. ``classified``), indistinguishable from
    one that is merely running slowly. An officer composing a parliamentary answer
    had no signal that the source document was stuck.
    """
    from mrip.auth.scope import Scope
    from mrip.ingest import pipeline  # noqa: F401 — registers failure hooks
    from mrip.schemas import DocumentState

    scope = Scope.unrestricted("test")
    document = make_document("annual_report.pdf")
    store.register_document(document)
    store.documents.set_state(document.document_id, DocumentState.CLASSIFIED)

    queue = JobQueue(store.connection)
    enqueued = queue.enqueue(
        "document.digitize",
        {"document_id": document.document_id},
        max_attempts=1,
    )
    queue.claim(WORKER_A)
    state = queue.fail(enqueued.job.job_id, WORKER_A, "out of memory", permanent=False)

    assert state is JobState.DEAD
    reloaded = store.get_document(document.document_id, scope)
    assert reloaded.state is DocumentState.FAILED
    assert reloaded.failed_stage == "digitize"
    assert reloaded.failed_reason is not None
    assert "out of memory" in reloaded.failed_reason


def test_a_retry_does_not_prematurely_mark_document_failed(store, make_document):
    """A job going back to PENDING still has attempts left — the document must
    not be marked FAILED until those are exhausted, or an officer would see a
    failure message for a document that is about to succeed on the next attempt."""
    from mrip.auth.scope import Scope
    from mrip.ingest import pipeline  # noqa: F401 — registers failure hooks
    from mrip.schemas import DocumentState

    scope = Scope.unrestricted("test")
    document = make_document("extract_fail.pdf")
    store.register_document(document)
    store.documents.set_state(document.document_id, DocumentState.CLASSIFIED)

    queue = JobQueue(store.connection)
    enqueued = queue.enqueue(
        "document.digitize",
        {"document_id": document.document_id},
        max_attempts=3,
    )
    queue.claim(WORKER_A)
    state = queue.fail(enqueued.job.job_id, WORKER_A, "transient network error")

    assert state is JobState.PENDING
    reloaded = store.get_document(document.document_id, scope)
    assert reloaded.state is DocumentState.CLASSIFIED, (
        "document must not be marked FAILED when a retry is still coming"
    )


# ----------------------------------------------------------------- reclamation


def test_an_expired_lease_returns_the_job_to_the_queue(queue):
    """The safety net behind "a worker is killable at any instant"."""
    enqueued = queue.enqueue("document.digitize", max_attempts=3)
    queue.claim(WORKER_A, lease_seconds=300)

    # Simulate a worker that died without reporting: age the lease past now.
    queue._conn.execute(
        sa.update(jobs)
        .where(jobs.c.job_id == enqueued.job.job_id)
        .values(lease_expires_at=sa.func.now() - timedelta(minutes=1))
    )

    assert queue.reclaim_expired() == 1

    job = queue.get(enqueued.job.job_id)
    assert job.state is JobState.PENDING
    assert job.claimed_by is None
    assert "Lease expired" in job.error
    assert len(queue.claim(WORKER_B)) == 1, "another worker can now take it"


def test_reclaiming_an_exhausted_job_dead_letters_it(queue):
    """A job whose worker dies on every attempt must still stop eventually."""
    enqueued = queue.enqueue("document.digitize", max_attempts=1)
    queue.claim(WORKER_A)

    queue._conn.execute(
        sa.update(jobs)
        .where(jobs.c.job_id == enqueued.job.job_id)
        .values(lease_expires_at=sa.func.now() - timedelta(minutes=1))
    )
    queue.reclaim_expired()

    assert queue.get(enqueued.job.job_id).state is JobState.DEAD


def test_reclaiming_an_exhausted_job_reconciles_its_document(store, make_document):
    """A worker killed mid-OCR is the common way a document half-processes.

    The job is reclaimed, not failed by a handler, so without the hook on this
    path the document stays in its intermediate state forever — no officer sees
    it, the dashboard counts it as in-flight, and the retry button never appears.
    """
    from mrip.auth.scope import Scope
    from mrip.ingest import pipeline  # noqa: F401 — registers the failure hooks
    from mrip.schemas import DocumentState

    scope = Scope.unrestricted("test")
    document = make_document("killed_mid_ocr.pdf")
    store.register_document(document)
    store.documents.set_state(document.document_id, DocumentState.DIGITIZED)

    queue = JobQueue(store.connection)
    enqueued = queue.enqueue(
        "document.digitize", {"document_id": document.document_id}, max_attempts=1
    )
    queue.claim(WORKER_A)
    queue._conn.execute(
        sa.update(jobs)
        .where(jobs.c.job_id == enqueued.job.job_id)
        .values(lease_expires_at=sa.func.now() - timedelta(minutes=1))
    )
    queue.reclaim_expired()

    reloaded = store.get_document(document.document_id, scope)
    assert reloaded.state is DocumentState.FAILED
    assert reloaded.failed_stage == "digitize"


def test_reclaiming_a_job_with_attempts_left_leaves_its_document_alone(
    store, make_document
):
    """The mirror: a reclaimed job that still has attempts must not mark its
    document failed, because the next attempt may well succeed."""
    from mrip.auth.scope import Scope
    from mrip.ingest import pipeline  # noqa: F401 — registers the failure hooks
    from mrip.schemas import DocumentState

    scope = Scope.unrestricted("test")
    document = make_document("slow_but_alive.pdf")
    store.register_document(document)
    store.documents.set_state(document.document_id, DocumentState.DIGITIZED)

    queue = JobQueue(store.connection)
    enqueued = queue.enqueue(
        "document.digitize", {"document_id": document.document_id}, max_attempts=3
    )
    queue.claim(WORKER_A)
    queue._conn.execute(
        sa.update(jobs)
        .where(jobs.c.job_id == enqueued.job.job_id)
        .values(lease_expires_at=sa.func.now() - timedelta(minutes=1))
    )
    queue.reclaim_expired()

    assert queue.get(enqueued.job.job_id).state is JobState.PENDING
    reloaded = store.get_document(document.document_id, scope)
    assert reloaded.state is DocumentState.DIGITIZED


def test_a_live_lease_is_not_reclaimed(queue):
    queue.enqueue("document.digitize")
    queue.claim(WORKER_A, lease_seconds=600)
    assert queue.reclaim_expired() == 0


# ------------------------------------------------------------------- retention


def test_pruning_removes_succeeded_jobs_but_keeps_failures(queue):
    """Pruning a failure on a timer is how an outage becomes invisible."""
    done = queue.enqueue("a", idempotency_key="done")
    dead = queue.enqueue("b", idempotency_key="dead", max_attempts=1)

    queue.claim(WORKER_A, limit=2)
    queue.complete(done.job.job_id, WORKER_A)
    queue.fail(dead.job.job_id, WORKER_A, "boom")

    # Age both past the retention window.
    queue._conn.execute(
        sa.update(jobs).values(finished_at=sa.func.now() - timedelta(days=30))
    )

    assert queue.prune_finished(older_than_days=14) == 1
    assert queue.get(done.job.job_id) is None
    assert queue.get(dead.job.job_id) is not None


def test_recent_succeeded_jobs_survive_pruning(queue):
    done = queue.enqueue("a")
    queue.claim(WORKER_A)
    queue.complete(done.job.job_id, WORKER_A)

    assert queue.prune_finished(older_than_days=14) == 0


# -------------------------------------------------------------------- backoff


def test_backoff_grows_and_is_capped():
    assert backoff_seconds(1) < backoff_seconds(4) < backoff_seconds(8)
    # Capped, so a long-failing job retries hourly-ish rather than never.
    assert backoff_seconds(50) <= 600 * 1.25


def test_backoff_is_jittered():
    """Without jitter, a batch that failed together retries in lockstep and
    knocks the recovering dependency over again."""
    samples = {backoff_seconds(5) for _ in range(20)}
    assert len(samples) > 1


# ------------------------------------------------------------------- registry


def test_the_maintenance_handlers_are_registered():
    """Importing mrip.jobs.handlers is what wires them; if that import is
    dropped the worker silently has nothing to run."""
    import mrip.jobs.handlers  # noqa: F401

    kinds = registered_kinds()
    assert "maintenance.detect_conflicts" in kinds
    assert "maintenance.flag_low_confidence" in kinds
    assert "maintenance.prune_jobs" in kinds


def test_an_unknown_kind_fails_permanently_and_says_what_exists():
    with pytest.raises(PermanentJobError) as raised:
        get_handler("document.teleport")

    message = str(raised.value)
    assert "document.teleport" in message
    assert "Registered kinds" in message


def test_registering_two_handlers_for_one_kind_is_refused():
    """Otherwise work routes to whichever module imported last."""

    @handler("test.duplicate")
    def _first(ctx):  # pragma: no cover - never executed
        pass

    with pytest.raises(RuntimeError, match="Two handlers registered"):

        @handler("test.duplicate")
        def _second(ctx):  # pragma: no cover - never executed
            pass


# ------------------------------------------------------ handlers, end to end


def test_the_conflict_sweep_handler_runs_against_the_real_store(store, make_fact):
    """Exercises the whole path: a job's handler doing real work on real rows."""
    from mrip.jobs.handlers import detect_conflicts
    from mrip.jobs.registry import JobContext

    store.insert_facts([make_fact(value=193.0e6), make_fact(value=191.5e6)])

    job = JobQueue(store.connection).enqueue("maintenance.detect_conflicts").job
    detect_conflicts(
        JobContext(job=job, store=store, worker_id=WORKER_A, lease_seconds=60)
    )

    from mrip.auth.scope import Scope

    assert len(store.open_conflicts(Scope.unrestricted("test"))) == 1


def test_a_handler_can_require_payload_fields(store):
    from mrip.jobs.registry import JobContext

    job = JobQueue(store.connection).enqueue("document.digitize", {"page": 4}).job
    ctx = JobContext(job=job, store=store, worker_id=WORKER_A, lease_seconds=60)

    assert ctx.require("page") == 4
    with pytest.raises(PermanentJobError, match="missing 'document_id'"):
        ctx.require("document_id")


# ------------------------------------------------------------ queue health


def test_stats_reports_how_long_the_oldest_job_has_waited(queue):
    """Depth alone cannot tell a busy queue from a stalled one.

    Forty queued items is healthy if the oldest is twenty seconds old and an
    outage if it is four hours old, so the age is the reading an alert watches.
    Computed by the database rather than returned as a timestamp for a caller to
    subtract: it is the same clock ``available_at`` was written against, so the
    two cannot disagree, and a browser with a wrong clock cannot invent an outage.
    """
    queue.enqueue("document.digitize", {"document_id": "doc_1"})
    queue._conn.execute(
        sa.update(jobs).values(available_at=sa.func.now() - timedelta(minutes=42))
    )

    stats = queue.stats()

    assert stats["pending"] == 1
    age = stats["oldest_pending_age_seconds"]
    assert age is not None
    # Allow a wide band: the point is "about forty minutes", not a stopwatch.
    assert 2_400 < age < 2_640, age


def test_an_empty_queue_reports_no_age_rather_than_zero(queue):
    """``None`` and ``0`` mean different things: nothing is waiting, versus
    something is waiting and has just arrived. An alert on ``> 900`` must not be
    fed a number that implies a backlog exists."""
    stats = queue.stats()

    assert stats["pending"] == 0
    assert stats["oldest_pending_age_seconds"] is None


def test_a_job_scheduled_for_later_is_not_reported_as_a_backlog(queue):
    """A retry serving its backoff, or a scheduled sweep, is waiting *to become
    due* — not waiting to be picked up. A raw subtraction makes that a negative
    age, and a negative age sorts below every threshold while still looking like a
    measurement. Floored at zero so it reads as "nothing is behind"."""
    queue.enqueue("maintenance.prune_jobs", delay_seconds=3600)

    stats = queue.stats()

    assert stats["oldest_pending_age_seconds"] == 0.0
