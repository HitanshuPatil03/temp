"""Built-in job handlers.

Importing this module is what registers them, so it is imported for its side
effect by the worker. Ingestion handlers arrive with Phase 1; these are the
corpus-wide sweeps the scheduler drives, and they exist now so the queue is
exercised end to end rather than sitting as untested scaffolding.

Every sweep here runs **unrestricted** and says why. A maintenance pass that
covered only one subsidiary's facts would silently leave the rest of the corpus
unflagged, which is worse than not running.
"""

from __future__ import annotations

from mrip import log
from mrip.auth.scope import Scope

# Importing the pipeline is what registers the six ingestion stages. Done here,
# in the module the worker already imports, so there is exactly one place that
# decides which handlers a worker knows about.
from mrip.ingest import pipeline  # noqa: F401
from mrip.jobs.queue import JobQueue
from mrip.jobs.registry import JobContext, handler

__all__ = [
    "accept_high_confidence",
    "detect_conflicts",
    "flag_low_confidence",
    "prune_jobs",
    "recompute_topics",
]

logger = log.get_logger("mrip.jobs.maintenance")


@handler("maintenance.accept_high_confidence")
def accept_high_confidence(ctx: JobContext) -> None:
    """Accept facts at or above the confidence threshold.

    A separate job from its mirror image rather than one that does both halves,
    so an operator reading the job table sees which direction ran. The kinds are
    also the handler keys, so broadening ``flag_low_confidence`` instead would
    have left its name describing half of what it did.

    The standing sweep matters because one path puts a fact back into
    ``extracted`` long after its document was ingested: when a disagreement
    clears, the surviving figure is returned to the pipeline rather than
    vouched for, and without this it would stay there unusably.
    """
    threshold = ctx.payload.get("threshold")
    accepted = ctx.store.promote_high_confidence(threshold)
    logger.info("facts accepted", accepted=accepted, threshold=threshold)


@handler("maintenance.flag_low_confidence")
def flag_low_confidence(ctx: JobContext) -> None:
    """Route facts below the confidence threshold into the review queue.

    Re-run rather than done once, because the threshold is a setting: lowering it
    should pull previously-accepted facts into review, and raising it should not
    require re-extracting anything.
    """
    threshold = ctx.payload.get("threshold")
    flagged = ctx.store.flag_low_confidence(threshold)
    logger.info(
        "facts flagged for review",
        flagged=flagged,
        threshold=threshold,
    )


@handler("maintenance.detect_conflicts")
def detect_conflicts(ctx: JobContext) -> None:
    """Recompute the conflict radar over the whole corpus.

    Unrestricted by necessity: a conflict is a disagreement *between* sources,
    and two documents from different subsidiaries reporting the same CIL total
    differently is exactly the case worth catching. Scoping this would make such
    a pair undetectable.
    """
    scope = Scope.unrestricted("maintenance sweep: conflict detection is corpus-wide")
    groups = ctx.store.detect_conflicts(
        scope, material_spread=ctx.payload.get("material_spread")
    )
    logger.info("conflict radar recomputed", open_groups=len(groups))


@handler("maintenance.prune_jobs")
def prune_jobs(ctx: JobContext) -> None:
    """Delete succeeded jobs past the retention window.

    Succeeded only. Failed and dead jobs are kept until someone has dealt with
    them — pruning a failure on a timer is how an outage becomes invisible.
    """
    days = int(ctx.payload.get("older_than_days", 14))
    removed = JobQueue(ctx.store.connection).prune_finished(older_than_days=days)
    logger.info("succeeded jobs pruned", removed=removed, older_than_days=days)


@handler("topics.recompute")
def recompute_topics(ctx: JobContext) -> None:
    """Rebuild every document's keyphrases with IDF over the whole corpus.

    Per-document extraction runs in the ingest pipeline's index stage, where a
    document's IDF is only approximate — it reflects the corpus extracted so
    far. This corpus-wide pass recomputes IDF across every document at once, so
    the cloud reflects the corpus as it actually stands. It is a **job**, not a
    synchronous endpoint: a two-pass scan over all evidence text is minutes of
    work and megabytes of memory on a real corpus, which an HTTP request must
    not hold. Unrestricted, like every maintenance sweep — IDF is a property of
    the corpus, not of any one reader; the cloud is scoped on read.
    """
    from mrip.topics.service import recompute_corpus

    written = recompute_corpus(ctx.store)
    logger.info(
        "topic keyphrases recomputed",
        documents=len(written),
        terms_written=sum(written.values()),
    )
