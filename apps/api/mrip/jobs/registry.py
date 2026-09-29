"""Handler registry and job context.

A handler is a plain function taking a :class:`JobContext`. The context carries
the job, a :class:`~mrip.db.Store` over the **job's own transaction**, and two
things that deliberately do *not* use that transaction:

``ctx.progress(...)`` writes in a separate transaction, because progress that is
invisible until the job commits is not progress — the point is for a reviewer to
see "OCR 142/400" while it is happening.

``ctx.keepalive()`` extends the lease, also separately, for the same reason.

An unknown job kind fails **permanently** rather than retrying. Three attempts at
a handler that does not exist produces three identical failures and buries the
real ones.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mrip import log
from mrip.db import Store, transaction
from mrip.jobs.queue import Job, JobQueue, PermanentJobError

__all__ = ["JobContext", "JobHandler", "get_handler", "handler", "registered_kinds"]

logger = log.get_logger("mrip.jobs")


@dataclass(frozen=True, slots=True)
class JobContext:
    """Everything a handler is given, and nothing it should reach around."""

    job: Job
    store: Store
    worker_id: str
    #: Seconds of lease granted per claim, so a handler can size its keepalives.
    lease_seconds: int

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload

    def require(self, key: str) -> Any:
        """Read a required payload field, or fail permanently.

        A job whose payload is missing a field will never succeed on retry, so
        this raises :class:`PermanentJobError` rather than letting three attempts
        burn on the same malformed row.
        """
        if key not in self.payload:
            raise PermanentJobError(
                f"Job {self.job.kind!r} payload is missing {key!r}. "
                f"Present keys: {sorted(self.payload) or 'none'}"
            )
        return self.payload[key]

    def keepalive(self) -> bool:
        """Extend the lease from inside a long handler.

        ``False`` means the lease was lost and another worker has the job — stop
        working. Uses its own transaction, so it is visible immediately and does
        not contend with the handler's own writes.
        """
        with transaction() as conn:
            return JobQueue(conn).heartbeat(
                self.job.job_id, self.worker_id, lease_seconds=self.lease_seconds
            )

    def progress(self, document_id: str, stage: str, done: int, total: int) -> None:
        """Publish per-stage progress for a document, immediately.

        Committed in its own transaction on purpose: progress held inside the
        job's transaction would appear only once the job finished, which is
        exactly when nobody needs it.
        """
        with transaction() as conn:
            Store(conn).documents.set_progress(document_id, stage, done, total)


JobHandler = Callable[[JobContext], None]

_HANDLERS: dict[str, JobHandler] = {}


def handler(kind: str) -> Callable[[JobHandler], JobHandler]:
    """Register a handler for a job kind.

    Kinds are namespaced (``document.digitize``, ``maintenance.prune_jobs``) so a
    queue row says which subsystem owns it without a lookup.
    """

    def register(function: JobHandler) -> JobHandler:
        if kind in _HANDLERS and _HANDLERS[kind] is not function:
            raise RuntimeError(
                f"Two handlers registered for job kind {kind!r}: "
                f"{_HANDLERS[kind].__qualname__} and {function.__qualname__}. "
                "Silently keeping one would route work to whichever module "
                "imported last."
            )
        _HANDLERS[kind] = function
        return function

    return register


def get_handler(kind: str) -> JobHandler:
    """Look up a handler, or fail permanently.

    The error names the kinds that *are* registered, because the usual cause is a
    module that was never imported rather than a typo.
    """
    try:
        return _HANDLERS[kind]
    except KeyError:
        raise PermanentJobError(
            f"No handler registered for job kind {kind!r}. "
            f"Registered kinds: {', '.join(sorted(_HANDLERS)) or 'none'}. "
            "If the handler exists, its module is not imported by "
            "mrip.jobs.handlers."
        ) from None


def registered_kinds() -> list[str]:
    return sorted(_HANDLERS)
