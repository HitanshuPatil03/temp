"""The scheduler process.

    mrip-scheduler

Enqueues periodic work. One instance is correct; a second is harmless, and both
of those facts are engineered rather than hoped for:

**A Postgres advisory lock elects the active scheduler.** A second instance finds
the lock held, does nothing, and keeps checking — so running two (a rolling
deploy, an operator starting a spare) produces no duplicate jobs rather than
double sweeps.

**Enqueueing is idempotent on a time bucket.** Each task's idempotency key is
``<task>@<floor(now / interval)>``, so even if the lock changes hands mid-tick,
or a scheduler restarts and re-ticks, the bucket already has its job and the
second enqueue collapses onto it. Correctness does not depend on the lock; the
lock only avoids the wasted work.

**Lease reclamation runs here, inline, not as a job.** A reclaim job would need a
worker to execute it — and the situation reclamation exists for is precisely the
one where workers have stopped responding. A recovery mechanism that depends on
the thing it recovers is not a recovery mechanism.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from dataclasses import dataclass
from types import FrameType
from typing import Any

from mrip import log
from mrip.config import Settings, get_settings
from mrip.db import advisory_lock, transaction
from mrip.db.schema_version import require_schema_version
from mrip.jobs.queue import DEFAULT_QUEUE, JobQueue

__all__ = ["SCHEDULE", "PeriodicTask", "Scheduler", "main"]

logger = log.get_logger("mrip.scheduler")

#: Lock name for scheduler election. Readable so it is identifiable in pg_locks.
SCHEDULER_LOCK = "mrip:scheduler"


@dataclass(frozen=True, slots=True)
class PeriodicTask:
    name: str
    kind: str
    interval_seconds: int
    payload: dict[str, Any] | None = None
    queue: str = DEFAULT_QUEUE
    priority: int = 0

    def bucket_key(self, now: float) -> str:
        """Idempotency key for the current interval window.

        Flooring the clock into buckets is what makes a re-tick harmless: two
        ticks inside one window produce the same key, and the second enqueue
        collapses onto the first.
        """
        return f"{self.name}@{int(now // self.interval_seconds)}"


#: The standing schedule. Intervals are deliberately unaligned — sweeps that all
#: fire on the hour contend for the same workers and the same table locks.
SCHEDULE: tuple[PeriodicTask, ...] = (
    PeriodicTask(
        name="flag-low-confidence",
        kind="maintenance.flag_low_confidence",
        interval_seconds=17 * 60,
    ),
    PeriodicTask(
        name="detect-conflicts",
        kind="maintenance.detect_conflicts",
        interval_seconds=23 * 60,
    ),
    PeriodicTask(
        name="prune-jobs",
        kind="maintenance.prune_jobs",
        interval_seconds=24 * 60 * 60,
        payload={"older_than_days": 14},
        # Housekeeping yields to real work.
        priority=-10,
    ),
)

#: How often to look for expired leases. Well under the default lease so a dead
#: worker's job is back in the queue promptly rather than after a full window.
RECLAIM_INTERVAL_SECONDS = 30


class Scheduler:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        schedule: tuple[PeriodicTask, ...] = SCHEDULE,
        tick_seconds: float = 5.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._schedule = schedule
        self._tick_seconds = tick_seconds
        self._stopping = threading.Event()
        self._last_reclaim = 0.0

    def request_stop(self, *_: object) -> None:
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                logger.debug("signal handler unavailable", signal=sig)

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("signal received", signal=signal.Signals(signum).name)
        self.request_stop()

    # ------------------------------------------------------------------ tick

    def tick(self, now: float | None = None) -> dict[str, int]:
        """One scheduling pass. Returns what it did, for logs and tests."""
        moment = time.time() if now is None else now
        enqueued = 0

        with transaction() as conn:
            queue = JobQueue(conn)
            for task in self._schedule:
                outcome = queue.enqueue(
                    task.kind,
                    task.payload,
                    queue=task.queue,
                    priority=task.priority,
                    idempotency_key=task.bucket_key(moment),
                )
                if outcome.created:
                    enqueued += 1

        reclaimed = 0
        if moment - self._last_reclaim >= RECLAIM_INTERVAL_SECONDS:
            self._last_reclaim = moment
            with transaction() as conn:
                reclaimed = JobQueue(conn).reclaim_expired()
            if reclaimed:
                logger.warning("reclaimed abandoned jobs", count=reclaimed)

        return {"enqueued": enqueued, "reclaimed": reclaimed}

    # ------------------------------------------------------------------ loop

    def run(self) -> int:
        require_schema_version()
        logger.info(
            "scheduler starting",
            tasks=[task.kind for task in self._schedule],
            tick_seconds=self._tick_seconds,
        )

        held = False
        while not self._stopping.is_set():
            # Re-acquired each cycle rather than held for the process lifetime,
            # so that when the active scheduler dies a standby takes over within
            # one cycle instead of waiting for a restart.
            with advisory_lock(SCHEDULER_LOCK) as acquired:
                if acquired:
                    if not held:
                        logger.info(
                            "acquired the scheduler lock; this instance is active"
                        )
                        held = True
                    try:
                        self.tick()
                    except Exception:
                        # A failed tick must not kill the scheduler: the next one
                        # re-enqueues the same buckets.
                        logger.exception("scheduler tick failed; continuing")
                elif held:
                    logger.info("lost the scheduler lock; standing by")
                    held = False

            self._stopping.wait(self._tick_seconds)

        logger.info("scheduler stopped")
        return 0


def main() -> int:
    """``mrip-scheduler`` entry point."""
    settings = get_settings()
    log.configure_logging(settings)

    scheduler = Scheduler(settings)
    scheduler.install_signal_handlers()
    try:
        return scheduler.run()
    except Exception:
        logger.exception("scheduler exiting on an unhandled error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
