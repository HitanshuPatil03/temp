"""The document lifecycle.

A document version moves through a fixed sequence of states, and this module is
the only place that says which moves are legal (ARCHITECTURE §6)::

    received → classified → digitized → extracted → normalized
             → validated → indexed → ready

plus two exits: ``failed`` (a stage broke; retryable) and ``quarantined``
(refused at the boundary; never handed to a worker).

**Why a table of allowed transitions rather than an integer you compare.**
Ordering alone would permit a document to skip from ``received`` straight to
``ready`` — which is exactly what a buggy handler that catches its own exception
would do, and the result is a document that reports success with no evidence
rows behind it. Enumerating the legal moves makes that a raised error instead of
a silent lie.

**Retries move backwards on purpose.** A failed document is re-run from the
stage that failed, so ``failed → digitized`` is legal. That is safe only because
every stage is idempotent: re-running it replaces its own output for this
document version rather than appending a second copy (see
:meth:`~mrip.db.repositories.documents.EvidenceRepository.replace_stage`).

The stage → job-kind mapping lives here too, so "what runs next" has one
definition shared by the upload path, the worker and the retry endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mrip.schemas import DocumentState

__all__ = [
    "PIPELINE",
    "STAGES",
    "IllegalTransitionError",
    "Stage",
    "can_transition",
    "next_job_for",
    "require_transition",
    "stage_for_state",
]


class IllegalTransitionError(RuntimeError):
    """A state change that the lifecycle does not permit.

    Raised rather than logged: reaching here means a handler's control flow is
    wrong, and letting it through would produce a document whose recorded state
    does not describe what is actually stored for it.
    """


@dataclass(frozen=True, slots=True)
class Stage:
    """One step of the pipeline: the job that does it and the state it reaches."""

    name: str
    job_kind: str
    #: The state a document is in once this stage has completed.
    completes_to: DocumentState
    #: Human-readable, shown on the document page beside the progress counter.
    description: str


#: The pipeline, in order. Each stage's job is enqueued when the previous one
#: commits, so the chain is driven by the queue rather than by one long-running
#: process that a restart would lose.
STAGES: Final[tuple[Stage, ...]] = (
    Stage(
        name="classify",
        job_kind="document.classify",
        completes_to=DocumentState.CLASSIFIED,
        description="Decide whether the pages carry a text layer or need OCR",
    ),
    Stage(
        name="digitize",
        job_kind="document.digitize",
        completes_to=DocumentState.DIGITIZED,
        description="Read text spans and table cells, with page and bounding box",
    ),
    Stage(
        name="extract",
        job_kind="document.extract",
        completes_to=DocumentState.EXTRACTED,
        description="Turn table cells into candidate figures",
    ),
    Stage(
        name="normalize",
        job_kind="document.normalize",
        completes_to=DocumentState.NORMALIZED,
        description="Route figures that are too weak to stand without review",
    ),
    Stage(
        name="validate",
        job_kind="document.validate",
        completes_to=DocumentState.VALIDATED,
        description="Run the validation rules and the conflict radar",
    ),
    Stage(
        name="index",
        job_kind="document.index",
        completes_to=DocumentState.INDEXED,
        description="Make the evidence searchable",
    ),
)

#: State → the stage that runs next from it.
PIPELINE: Final[dict[DocumentState, Stage]] = {
    DocumentState.RECEIVED: STAGES[0],
    DocumentState.CLASSIFIED: STAGES[1],
    DocumentState.DIGITIZED: STAGES[2],
    DocumentState.EXTRACTED: STAGES[3],
    DocumentState.NORMALIZED: STAGES[4],
    DocumentState.VALIDATED: STAGES[5],
}

#: Stage name → the stage, for retrying a named failure.
_BY_NAME: Final[dict[str, Stage]] = {stage.name: stage for stage in STAGES}

#: Every legal move. Written out rather than derived from the order, because the
#: point is to forbid the moves that an ordering would allow.
_ALLOWED: Final[dict[DocumentState, frozenset[DocumentState]]] = {
    DocumentState.RECEIVED: frozenset(
        {DocumentState.CLASSIFIED, DocumentState.FAILED, DocumentState.QUARANTINED}
    ),
    DocumentState.CLASSIFIED: frozenset(
        {DocumentState.DIGITIZED, DocumentState.FAILED, DocumentState.QUARANTINED}
    ),
    DocumentState.DIGITIZED: frozenset({DocumentState.EXTRACTED, DocumentState.FAILED}),
    DocumentState.EXTRACTED: frozenset({DocumentState.NORMALIZED, DocumentState.FAILED}),
    DocumentState.NORMALIZED: frozenset({DocumentState.VALIDATED, DocumentState.FAILED}),
    DocumentState.VALIDATED: frozenset({DocumentState.INDEXED, DocumentState.FAILED}),
    DocumentState.INDEXED: frozenset({DocumentState.READY, DocumentState.FAILED}),
    # A finished document can be re-run — a corrected extractor, a new metric —
    # but only back to the start of a stage, never forward to a state whose work
    # has not been done.
    DocumentState.READY: frozenset({DocumentState.CLASSIFIED, DocumentState.QUARANTINED}),
    # Retry: back to the state *before* the stage that failed, so the stage runs
    # again. Idempotent stages make this safe.
    DocumentState.FAILED: frozenset(
        {
            DocumentState.RECEIVED,
            DocumentState.CLASSIFIED,
            DocumentState.DIGITIZED,
            DocumentState.EXTRACTED,
            DocumentState.NORMALIZED,
            DocumentState.VALIDATED,
            DocumentState.QUARANTINED,
        }
    ),
    # Terminal. A quarantined document is not re-processed: whatever made it
    # unsafe is still true of the bytes, and "try again" on a decompression bomb
    # is not a recovery strategy. It is deleted, or the source is re-supplied as
    # a new upload.
    DocumentState.QUARANTINED: frozenset(),
}


def can_transition(current: DocumentState, target: DocumentState) -> bool:
    """Whether this move is legal. Same-state is allowed (an idempotent re-run)."""
    if current is target:
        return True
    return target in _ALLOWED.get(current, frozenset())


def require_transition(current: DocumentState, target: DocumentState) -> None:
    """Raise :class:`IllegalTransitionError` unless the move is legal."""
    if can_transition(current, target):
        return
    legal = sorted(state.value for state in _ALLOWED.get(current, frozenset()))
    raise IllegalTransitionError(
        f"A document in {current.value!r} cannot move to {target.value!r}. "
        f"Legal from here: {', '.join(legal) or 'nothing — this state is terminal'}."
    )


def next_job_for(state: DocumentState) -> Stage | None:
    """The stage to enqueue from this state, or ``None`` if there is nothing left.

    ``None`` for ``ready`` (finished), ``quarantined`` (refused) and ``failed`` —
    a failed document waits for a person or a retry endpoint, because
    automatically re-queueing it is how a poison document becomes an infinite
    loop with a database behind it.
    """
    return PIPELINE.get(state)


def stage_for_state(state: DocumentState) -> Stage | None:
    """The stage whose completion produces this state. Used to retry a failure."""
    for stage in STAGES:
        if stage.completes_to is state:
            return stage
    return None


def stage_by_name(name: str) -> Stage:
    """Look up a stage by name, for a retry that names one."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"No stage named {name!r}. Stages: {', '.join(_BY_NAME)}."
        ) from None
