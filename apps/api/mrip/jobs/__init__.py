"""Background work: the queue, its workers, and the scheduler.

The queue lives in PostgreSQL and is claimed with ``SELECT … FOR UPDATE SKIP
LOCKED``. That is not a shortcut around a "real" broker — it is what lets
``enqueue`` participate in the same transaction as the write that needs the work
done, so a document can never be registered with nothing scheduled to process it.

    from mrip.db import transaction
    from mrip.jobs import JobQueue

    with transaction() as conn:
        store = Store(conn)
        document = store.register_document(incoming)          # same transaction
        JobQueue(conn).enqueue(                                # …as the job
            "document.digitize",
            {"document_id": document.document_id},
            idempotency_key=f"digitize:{document.document_id}:1",
        )
"""

from mrip.jobs.queue import (
    DEFAULT_QUEUE,
    Enqueued,
    Job,
    JobQueue,
    PermanentJobError,
)
from mrip.jobs.registry import JobContext, JobHandler, handler, registered_kinds

__all__ = [
    "DEFAULT_QUEUE",
    "Enqueued",
    "Job",
    "JobContext",
    "JobHandler",
    "JobQueue",
    "PermanentJobError",
    "handler",
    "registered_kinds",
]
