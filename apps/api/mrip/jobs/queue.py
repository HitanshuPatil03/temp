"""The job queue.

Claimed with ``SELECT … FOR UPDATE SKIP LOCKED``, which is the whole reason this
architecture has no Redis: **enqueueing a job is part of the same transaction as
the write that requires it.** A crash can never leave a document registered with
nothing scheduled to process it, because either both rows committed or neither
did.

Four properties are load-bearing, and each is tested in ``tests/test_jobs.py``:

**Attempts are counted at claim time, not at failure.** A job that reliably kills
its worker never reports a failure, so counting on failure would let a poison pill
loop forever. Counting on claim means it exhausts its attempts and dead-letters.

**A claim is a lease, not ownership.** If a worker dies mid-job the lease expires
and another worker may reclaim it. That is only safe because every pipeline stage
is idempotent (ARCHITECTURE §6) — the queue relies on that and does not provide
it.

**A worker that lost its lease cannot report.** ``complete`` and ``fail`` both
require the caller to still be the lease holder, so a slow worker whose job was
reclaimed cannot overwrite the outcome of the worker that actually finished it.

**Retries return to ``pending``.** ``failed`` and ``dead`` are both terminal and
distinct: ``dead`` means the attempts ran out, ``failed`` means a handler said
"this will never work" (a malformed payload, an unknown kind, an encrypted PDF).
Conflating them would make a permanent failure look like something worth retrying.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from mrip.db.tables import jobs
from mrip.schemas import JobState


# Imported lazily at call time rather than at module top, so the queue does not
# import the hooks registry at startup. The hook registry itself imports nothing
# from this module, so there is no cycle — but making the import conditional on
# call keeps the queue's own boot path dependency-free.
def _run_hook_if_any(
    conn: Connection, kind: str, payload: dict[str, Any], error: str
) -> None:
    """Fire the terminal-failure hook for ``kind``, if one is registered.

    Deferred import so a worker that never imports mrip.ingest.pipeline (which
    is what registers hooks) does not crash on a missing handler — the hook
    registry returns False for an unregistered kind rather than raising.
    """
    from mrip.jobs.hooks import run_terminal_failure_hook

    run_terminal_failure_hook(conn, kind, payload, error)


__all__ = [
    "DEFAULT_QUEUE",
    "Enqueued",
    "Job",
    "JobQueue",
    "PermanentJobError",
    "backoff_seconds",
]

DEFAULT_QUEUE = "default"

#: Retry schedule: 5s, 10s, 20s, … capped at 10 minutes.
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_CAP_SECONDS = 600.0
_BACKOFF_JITTER = 0.25


class PermanentJobError(Exception):
    """Raised by a handler that will never succeed on this input.

    Sends the job straight to ``failed`` without consuming further attempts.
    Retrying a malformed payload or an encrypted PDF ten times is wasted work and
    it buries the real failures in the queue.
    """


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff with jitter.

    The jitter matters more than the curve: without it, a batch of jobs that all
    failed against a briefly-unavailable dependency retries in lockstep and
    knocks it over again.
    """
    delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** max(0, attempts - 1))
    spread = delay * _BACKOFF_JITTER
    # Scheduling jitter, not a security decision.
    jittered: float = delay + random.uniform(-spread, spread)  # noqa: S311
    return max(1.0, jittered)


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    queue: str
    kind: str
    payload: dict[str, Any]
    state: JobState
    priority: int
    attempts: int
    max_attempts: int
    available_at: datetime
    claimed_by: str | None
    lease_expires_at: datetime | None
    error: str | None
    idempotency_key: str | None
    created_at: datetime

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    @property
    def is_terminal(self) -> bool:
        return self.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.DEAD}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Job:
        return cls(
            job_id=row["job_id"],
            queue=row["queue"],
            kind=row["kind"],
            payload=row["payload"] or {},
            state=JobState(row["state"]),
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            available_at=row["available_at"],
            claimed_by=row["claimed_by"],
            lease_expires_at=row["lease_expires_at"],
            error=row["error"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class Enqueued:
    """The outcome of an :meth:`JobQueue.enqueue` call.

    ``created`` is a fact about the *operation*, not about the job, which is why
    it does not live on :class:`Job`: a job fetched with :meth:`JobQueue.get`
    would carry a meaningless ``created=False``. Callers that only want the job
    write ``queue.enqueue(...).job``.
    """

    job: Job
    #: False when an ``idempotency_key`` collapsed this onto existing work.
    created: bool


class JobQueue:
    """Queue operations over one transaction.

    Like the repositories, a ``JobQueue`` is bound to a caller-supplied
    connection rather than owning one. That is what lets ``enqueue`` join the
    transaction that registers a document.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # -------------------------------------------------------------- enqueue

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        queue: str = DEFAULT_QUEUE,
        priority: int = 0,
        delay_seconds: float = 0.0,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
    ) -> Enqueued:
        """Schedule work.

        With an ``idempotency_key``, re-requesting the same work collapses onto
        the job already recorded for it instead of duplicating it, and the result
        reports ``created=False``. Note that this holds even when that job has
        already succeeded: "digitize version 3 of this document" is a statement
        about a desired end state, and it has been reached. A caller that
        genuinely wants the work redone uses a different key.
        """
        from mrip.db.repositories.documents import new_id

        values: dict[str, Any] = {
            "job_id": new_id("job"),
            "queue": queue,
            "kind": kind,
            "payload": payload or {},
            "state": JobState.PENDING.value,
            "priority": priority,
            "max_attempts": max_attempts,
            "idempotency_key": idempotency_key,
        }
        if delay_seconds > 0:
            values["available_at"] = sa.func.now() + timedelta(seconds=delay_seconds)

        if idempotency_key is None:
            row = (
                self._conn.execute(sa.insert(jobs).values(**values).returning(jobs))
                .mappings()
                .one()
            )
            return Enqueued(Job.from_row(dict(row)), created=True)

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        statement = (
            pg_insert(jobs)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(jobs)
        )
        inserted = self._conn.execute(statement).mappings().first()
        if inserted is not None:
            return Enqueued(Job.from_row(dict(inserted)), created=True)

        # Lost the race, or the work was already requested. Return what is there.
        existing = (
            self._conn.execute(
                sa.select(jobs).where(jobs.c.idempotency_key == idempotency_key)
            )
            .mappings()
            .one()
        )
        return Enqueued(Job.from_row(dict(existing)), created=False)

    # ----------------------------------------------------------------- claim

    def claim(
        self,
        worker_id: str,
        *,
        queues: list[str] | None = None,
        limit: int = 1,
        lease_seconds: int = 300,
    ) -> list[Job]:
        """Take up to ``limit`` claimable jobs, skipping rows another worker holds.

        ``SKIP LOCKED`` is what makes this safe without a broker: two workers
        running this statement concurrently step over each other's locked rows
        rather than blocking or double-claiming.
        """
        claimable = (
            sa.select(jobs.c.job_id)
            .where(
                jobs.c.state == JobState.PENDING.value,
                jobs.c.available_at <= sa.func.now(),
            )
            .order_by(
                jobs.c.priority.desc(),
                jobs.c.available_at,
                jobs.c.created_at,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if queues:
            claimable = claimable.where(jobs.c.queue.in_(queues))

        rows = (
            self._conn.execute(
                sa.update(jobs)
                .where(jobs.c.job_id.in_(claimable.scalar_subquery()))
                .values(
                    state=JobState.CLAIMED.value,
                    claimed_by=worker_id,
                    claimed_at=sa.func.now(),
                    heartbeat_at=sa.func.now(),
                    lease_expires_at=sa.func.now() + timedelta(seconds=lease_seconds),
                    # Counted here, not on failure — see the module docstring.
                    attempts=jobs.c.attempts + 1,
                )
                .returning(jobs)
            )
            .mappings()
            .all()
        )
        return [Job.from_row(dict(row)) for row in rows]

    # ------------------------------------------------------------- lifecycle

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> bool:
        """Extend the lease. ``False`` means the lease was lost — abandon the job.

        A worker that keeps working after losing its lease is racing whoever
        reclaimed it, so the return value is not advisory.
        """
        result = self._conn.execute(
            sa.update(jobs)
            .where(
                jobs.c.job_id == job_id,
                jobs.c.claimed_by == worker_id,
                jobs.c.state == JobState.CLAIMED.value,
            )
            .values(
                heartbeat_at=sa.func.now(),
                lease_expires_at=sa.func.now() + timedelta(seconds=lease_seconds),
            )
        )
        return (result.rowcount or 0) == 1

    def complete(self, job_id: str, worker_id: str) -> bool:
        """Mark succeeded. ``False`` if this worker no longer holds the lease."""
        result = self._conn.execute(
            sa.update(jobs)
            .where(
                jobs.c.job_id == job_id,
                jobs.c.claimed_by == worker_id,
                jobs.c.state == JobState.CLAIMED.value,
            )
            .values(
                state=JobState.SUCCEEDED.value,
                finished_at=sa.func.now(),
                lease_expires_at=None,
                error=None,
            )
        )
        return (result.rowcount or 0) == 1

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        permanent: bool = False,
    ) -> JobState | None:
        """Record a failure and decide what happens next.

        Returns the resulting state, or ``None`` if the lease was lost. A job that
        has exhausted its attempts becomes ``dead`` and is **kept**: a failure
        nobody can inspect is a failure that happens again.
        """
        current = (
            self._conn.execute(
                sa.select(jobs.c.attempts, jobs.c.max_attempts).where(
                    jobs.c.job_id == job_id,
                    jobs.c.claimed_by == worker_id,
                    jobs.c.state == JobState.CLAIMED.value,
                )
            )
            .mappings()
            .first()
        )
        if current is None:
            return None

        exhausted = current["attempts"] >= current["max_attempts"]
        values: dict[str, Any] = {
            # Truncated: a multi-megabyte traceback in a queue row is not a log.
            "error": error[:8000],
            "lease_expires_at": None,
            "claimed_by": None,
            "claimed_at": None,
        }

        if permanent:
            state = JobState.FAILED
            values["finished_at"] = sa.func.now()
        elif exhausted:
            state = JobState.DEAD
            values["finished_at"] = sa.func.now()
        else:
            state = JobState.PENDING
            values["available_at"] = sa.func.now() + timedelta(
                seconds=backoff_seconds(current["attempts"])
            )

        values["state"] = state.value
        self._conn.execute(
            sa.update(jobs).where(jobs.c.job_id == job_id).values(**values)
        )
        # Fire the terminal-failure hook only when no further attempt is coming.
        # PENDING means another try has been scheduled — marking a document
        # failed while a retry is still coming would falsely alarm an officer.
        if state in {JobState.FAILED, JobState.DEAD}:
            payload_row = (
                self._conn.execute(
                    sa.select(jobs.c.kind, jobs.c.payload).where(jobs.c.job_id == job_id)
                )
                .mappings()
                .first()
            )
            if payload_row is not None:
                _run_hook_if_any(
                    self._conn,
                    str(payload_row["kind"]),
                    dict(payload_row["payload"]),
                    error,
                )
        return state

    # ----------------------------------------------------------- maintenance

    def reclaim_expired(self, *, limit: int = 100) -> int:
        """Return jobs whose lease expired to the queue, or dead-letter them.

        The safety net behind "a worker is killable at any instant". Runs from the
        scheduler rather than a worker, so a worker that hangs without dying
        cannot also be responsible for noticing.

        A job that exhausts its attempts here is dead-lettered exactly as
        :meth:`fail` would, so the terminal-failure hook fires for it too. That
        path matters as much as the first: the common way a document
        half-processes is a worker killed mid-OCR, whose job is *reclaimed*, not
        failed by a handler — and the document would otherwise stay in
        ``digitized``-and-a-half forever with nobody told.
        """
        rows = (
            self._conn.execute(
                sa.select(
                    jobs.c.job_id,
                    jobs.c.kind,
                    jobs.c.payload,
                    jobs.c.attempts,
                    jobs.c.max_attempts,
                )
                .where(
                    jobs.c.state == JobState.CLAIMED.value,
                    jobs.c.lease_expires_at < sa.func.now(),
                )
                .order_by(jobs.c.lease_expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .all()
        )
        if not rows:
            return 0

        # The rows are locked by the statement above, so this update acts on
        # exactly the set that was read — no second scan, and no race with a
        # worker that reclaims concurrently (it would block on the lock).
        expiring = [row["job_id"] for row in rows]
        result = self._conn.execute(
            sa.update(jobs)
            .where(jobs.c.job_id.in_(expiring))
            .values(
                state=sa.case(
                    (
                        jobs.c.attempts >= jobs.c.max_attempts,
                        JobState.DEAD.value,
                    ),
                    else_=JobState.PENDING.value,
                ),
                claimed_by=None,
                claimed_at=None,
                lease_expires_at=None,
                error=sa.func.coalesce(
                    jobs.c.error,
                    sa.literal(
                        "Lease expired: the worker holding this job stopped "
                        "reporting. Reclaimed for another worker."
                    ),
                ),
                finished_at=sa.case(
                    (jobs.c.attempts >= jobs.c.max_attempts, sa.func.now()),
                    else_=None,
                ),
            )
        )

        # Reconcile only the ones that just dead-lettered. The rest went back to
        # ``pending`` and still have attempts left, so their document must not be
        # marked failed while a retry is still coming.
        reason = (
            "Lease expired: the worker holding this job stopped reporting. "
            "No attempts remain, so the job was dead-lettered."
        )
        for row in rows:
            if row["attempts"] >= row["max_attempts"]:
                _run_hook_if_any(
                    self._conn, str(row["kind"]), dict(row["payload"]), reason
                )

        return result.rowcount or 0

    def prune_finished(self, *, older_than_days: int = 14) -> int:
        """Delete succeeded jobs past the retention window.

        Only ``succeeded``. ``dead`` and ``failed`` jobs are retained until an
        operator has dealt with them — pruning a failure on a timer is how an
        outage becomes invisible.
        """
        result = self._conn.execute(
            sa.delete(jobs).where(
                jobs.c.state == JobState.SUCCEEDED.value,
                jobs.c.finished_at < sa.func.now() - timedelta(days=older_than_days),
            )
        )
        return result.rowcount or 0

    # ----------------------------------------------------------------- reads

    def get(self, job_id: str) -> Job | None:
        row = (
            self._conn.execute(sa.select(jobs).where(jobs.c.job_id == job_id))
            .mappings()
            .first()
        )
        return Job.from_row(dict(row)) if row else None

    def depth(self) -> dict[str, int]:
        """Per-state counts. Backs the pipeline health probe."""
        rows = self._conn.execute(
            sa.select(jobs.c.state, sa.func.count()).group_by(jobs.c.state)
        ).all()
        counts = {state.value: 0 for state in JobState}
        counts.update({str(state): int(count) for state, count in rows})
        return counts

    def stats(self) -> dict[str, Any]:
        """Queue health in one round trip.

        ``oldest_pending_age_seconds`` is the number that actually matters. A
        depth of 40 is healthy if the oldest item is twenty seconds old and an
        outage if it is four hours old, so a probe that reported only depth would
        miss a stalled worker pool entirely.

        The age is computed **here**, by the database, rather than left for a
        caller to subtract from a timestamp. Two reasons: the database's clock is
        the one the queue's own ``available_at`` was written against, so the
        subtraction cannot disagree with itself; and a browser with a wrong clock
        would otherwise report an outage that is not happening, or miss one that
        is. A reading that decides whether someone is paged should not depend on
        whose watch is right.
        """
        row = (
            self._conn.execute(
                sa.select(
                    sa.func.count()
                    .filter(jobs.c.state == JobState.PENDING.value)
                    .label("pending"),
                    sa.func.count()
                    .filter(jobs.c.state == JobState.CLAIMED.value)
                    .label("in_flight"),
                    sa.func.count()
                    .filter(jobs.c.state == JobState.DEAD.value)
                    .label("dead"),
                    sa.func.count()
                    .filter(jobs.c.state == JobState.FAILED.value)
                    .label("failed"),
                    sa.func.min(jobs.c.available_at)
                    .filter(jobs.c.state == JobState.PENDING.value)
                    .label("oldest_pending_at"),
                    sa.func.extract(
                        "epoch",
                        sa.func.now()
                        - sa.func.min(jobs.c.available_at).filter(
                            jobs.c.state == JobState.PENDING.value
                        ),
                    ).label("oldest_pending_age_seconds"),
                    sa.func.min(jobs.c.lease_expires_at)
                    .filter(jobs.c.state == JobState.CLAIMED.value)
                    .label("earliest_lease_expiry"),
                )
            )
            .mappings()
            .one()
        )
        age = row["oldest_pending_age_seconds"]
        # Negative for a job deliberately scheduled in the future (a retry's
        # backoff, a scheduled sweep): that job is waiting *to become* due, not
        # waiting *to be picked up*, and reporting it as a backlog age would read
        # as a stall. Floored at zero for that reason.
        oldest_age = max(0.0, float(age)) if age is not None else None
        return {
            "pending": int(row["pending"] or 0),
            "in_flight": int(row["in_flight"] or 0),
            "dead": int(row["dead"] or 0),
            "failed": int(row["failed"] or 0),
            "oldest_pending_at": row["oldest_pending_at"],
            "oldest_pending_age_seconds": oldest_age,
            "earliest_lease_expiry": row["earliest_lease_expiry"],
        }
