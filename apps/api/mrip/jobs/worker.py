"""The worker process.

    mrip-worker

Long work lives here, never in a request. The design constraint is that this
process is **killable at any instant**: `docker compose up --scale worker=4`,
a rolling restart, an OOM kill, a pulled power cord. Nothing it does may leave a
document half-processed in a way a retry cannot repair.

Three things make that true:

**The handler and the job's completion commit together.** One transaction. If the
process dies anywhere inside it, PostgreSQL rolls back both the work and the
"succeeded" mark, and the lease expiry returns the job to the queue. There is no
window in which a job is recorded as done but its output is missing.

**Failures commit in a separate transaction.** Recording *why* something failed
must survive the rollback of the thing that failed.

**SIGTERM stops claiming, then drains.** A worker asked to stop finishes the jobs
it holds rather than abandoning them to a lease timeout, so a deploy costs a few
seconds instead of the lease window.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from types import FrameType

from mrip import log
from mrip.config import Settings, get_settings
from mrip.db import Store, transaction
from mrip.db.schema_version import require_schema_version
from mrip.jobs import handlers  # noqa: F401 — importing registers the handlers
from mrip.jobs.queue import Job, JobQueue, PermanentJobError
from mrip.jobs.registry import JobContext, get_handler, registered_kinds

__all__ = ["Worker", "main"]

logger = log.get_logger("mrip.worker")


def _worker_id() -> str:
    """Identifies the lease holder.

    Host and pid, so a stuck job in the queue names the process to go and look
    at — which in a multi-container deployment is the difference between a
    five-minute diagnosis and an afternoon.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


class LeaseLostError(RuntimeError):
    """The job was reclaimed by another worker while this one was running it."""


class Worker:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        queues: list[str] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._queues = queues
        self.worker_id = worker_id or _worker_id()
        self._stopping = threading.Event()
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    # --------------------------------------------------------------- control

    def request_stop(self, *_: object) -> None:
        """Stop claiming new work. In-flight jobs are allowed to finish."""
        if not self._stopping.is_set():
            logger.info("stop requested", in_flight=len(self._in_flight))
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # Not the main thread, or a platform without the signal. Not
                # fatal: the loop still exits via request_stop().
                logger.debug("signal handler unavailable", signal=sig)

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("signal received", signal=signal.Signals(signum).name)
        self.request_stop()

    # ------------------------------------------------------------------ loop

    def run(self) -> int:
        """Claim and execute until stopped. Returns a process exit code."""
        settings = self._settings
        require_schema_version()

        logger.info(
            "worker starting",
            worker_id=self.worker_id,
            concurrency=settings.worker_concurrency,
            queues=self._queues or "all",
            lease_seconds=settings.job_lease_seconds,
            kinds=registered_kinds(),
        )

        with ThreadPoolExecutor(
            max_workers=settings.worker_concurrency,
            thread_name_prefix="mrip-job",
        ) as pool:
            pending: set[Future[None]] = set()
            while not self._stopping.is_set():
                pending = {future for future in pending if not future.done()}
                capacity = settings.worker_concurrency - len(pending)

                if capacity <= 0:
                    time.sleep(settings.job_poll_seconds)
                    continue

                claimed = self._claim(capacity)
                if not claimed:
                    # Nothing to do. Sleeping beats spinning on the database;
                    # latency here is bounded by job_poll_seconds, which is fine
                    # for work measured in minutes.
                    self._stopping.wait(settings.job_poll_seconds)
                    continue

                for job in claimed:
                    pending.add(pool.submit(self._execute, job))

            # Draining. The pool's own shutdown waits for submitted work.
            logger.info("draining before exit", in_flight=len(pending))

        logger.info("worker stopped", worker_id=self.worker_id)
        return 0

    def _claim(self, capacity: int) -> list[Job]:
        try:
            with transaction() as conn:
                return JobQueue(conn).claim(
                    self.worker_id,
                    queues=self._queues,
                    limit=capacity,
                    lease_seconds=self._settings.job_lease_seconds,
                )
        except Exception:
            # A database blip must not kill the worker; the next poll retries.
            # Logged at exception level because a *persistent* failure here means
            # the queue is not draining at all.
            logger.exception("claim failed; retrying after the poll interval")
            time.sleep(self._settings.job_poll_seconds)
            return []

    # ------------------------------------------------------------- execution

    def _execute(self, job: Job) -> None:
        with self._lock:
            self._in_flight.add(job.job_id)
        started = time.monotonic()

        # Bound for the whole execution, so a line logged three frames inside a
        # handler still says which job produced it.
        with log.bound_contextvars(
            job_id=job.job_id,
            job_kind=job.kind,
            attempt=job.attempts,
            worker_id=self.worker_id,
        ):
            self._run(job, started)

    def _run(self, job: Job, started: float) -> None:
        try:
            handler = get_handler(job.kind)
        except PermanentJobError as unknown:
            self._record_failure(job, str(unknown), permanent=True)
            with self._lock:
                self._in_flight.discard(job.job_id)
            return

        try:
            # The handler's work and the job's completion, atomically.
            with transaction() as conn:
                context = JobContext(
                    job=job,
                    store=Store(conn, self._settings),
                    worker_id=self.worker_id,
                    lease_seconds=self._settings.job_lease_seconds,
                )
                handler(context)

                if not JobQueue(conn).complete(job.job_id, self.worker_id):
                    # The lease was reclaimed while this handler ran, so another
                    # worker owns the outcome. Roll the work back rather than
                    # racing it — the winner's run is the one that counts.
                    raise LeaseLostError(job.job_id)

        except LeaseLostError:
            logger.warning("lease lost mid-execution; work rolled back")
        except PermanentJobError as permanent:
            logger.warning("job failed permanently", reason=str(permanent))
            self._record_failure(job, str(permanent), permanent=True)
        except Exception:
            detail = traceback.format_exc()
            logger.error(
                "job failed",
                attempt=job.attempts,
                max_attempts=job.max_attempts,
                error=detail.strip().splitlines()[-1],
            )
            self._record_failure(job, detail, permanent=False)
        else:
            logger.info(
                "job succeeded", elapsed_ms=round((time.monotonic() - started) * 1000)
            )
        finally:
            with self._lock:
                self._in_flight.discard(job.job_id)

    def _record_failure(self, job: Job, error: str, *, permanent: bool) -> None:
        """Persist the failure in its own transaction.

        Separate from the handler's transaction by necessity: that one has been
        rolled back, and the reason it failed is the part worth keeping.
        """
        try:
            with transaction() as conn:
                JobQueue(conn).fail(
                    job.job_id, self.worker_id, error, permanent=permanent
                )
        except Exception:
            # Nothing further to do — the lease will expire and the job will be
            # reclaimed by the scheduler. Logged so the queue's state is
            # explicable afterwards.
            logger.exception("could not record the failure")


def main() -> int:
    """``mrip-worker`` entry point."""
    settings = get_settings()
    log.configure_logging(settings)

    queues_env = os.environ.get("MRIP_WORKER_QUEUES", "").strip()
    queues = [q.strip() for q in queues_env.split(",") if q.strip()] or None

    worker = Worker(settings, queues=queues)
    worker.install_signal_handlers()
    try:
        return worker.run()
    except Exception:
        logger.exception("worker exiting on an unhandled error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
