"""Upload and document-lifecycle endpoints.

The upload path is the one place untrusted bytes enter the system, and its order
of operations is the design:

1. **stream to staging**, counting bytes against the cap as they arrive — a size
   limit checked after the upload has landed is not a limit;
2. **inspect at the boundary** (:mod:`mrip.ingest.intake`) — type by magic
   number, page cap, encrypted PDF refused, active content stripped;
3. **hash what will be stored**, after sanitizing, so the content hash is the
   hash of the bytes the corpus actually holds;
4. **dedup on that hash** — identical bytes are the same document, and the
   second upload is a no-op that returns the first;
5. **register and enqueue in one transaction**, so a crash can never leave a
   document row with nothing scheduled to process it.

Step 5 is why there is no message broker in this architecture (ARCHITECTURE §4).

A refusal from the boundary is a **422 with a reason a person can act on**, not a
400 with "invalid file". The distinction matters to the officer who has been
handed a password-protected PDF by a subsidiary and needs to know what to ask
for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel

from mrip import log
from mrip.api.deps import (
    ScopeDep,
    SettingsDep,
    SourceIpDep,
    StoreDep,
    require_role,
)
from mrip.auth.principal import Principal
from mrip.blobs import CHUNK_BYTES, get_blob_store
from mrip.db.repositories.documents import new_id
from mrip.facts.cells import ACTIONABLE_SKIPS, skip_label
from mrip.ingest.intake import IntakeError, inspect_upload
from mrip.ingest.lifecycle import (
    PIPELINE,
    STAGES,
    IllegalTransitionError,
    require_transition,
    stage_by_name,
)
from mrip.ingest.pipeline import enqueue_next_stage
from mrip.jobs.queue import JobQueue
from mrip.schemas import Document, DocumentState, Role, Sensitivity

router = APIRouter(tags=["documents"])

logger = log.get_logger("mrip.upload")

#: Uploading is how figures enter the corpus; a viewer is someone who reads it.
OfficerDep = Annotated[Principal, Depends(require_role(Role.OFFICER))]


class UploadResponse(BaseModel):
    """What an accepted upload returns."""

    document_id: str
    content_hash: str
    filename: str
    doc_class: str
    state: DocumentState
    page_count: int | None
    size_bytes: int
    #: False when these bytes were already in the corpus. Not an error: it is the
    #: mechanism that makes a retried upload safe.
    created: bool
    #: What the boundary did to the file, verbatim, so the uploader is told
    #: rather than surprised.
    notes: list[str] = []
    #: The document this one was recorded as replacing, if the uploader said so,
    #: and how many of its facts were retired as a result.
    supersedes: str | None = None
    facts_superseded: int = 0


@router.post(
    "/documents",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
    officer: OfficerDep,
    scope: ScopeDep,
    caller_ip: SourceIpDep,
    file: Annotated[UploadFile, File(description="The source document")],
    publisher_entity_id: Annotated[str | None, Form()] = None,
    fiscal_year: Annotated[str | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
    sensitivity: Annotated[Sensitivity, Form()] = Sensitivity.INTERNAL,
    supersedes: Annotated[str | None, Form()] = None,
) -> UploadResponse:
    """Accept a source document and start its ingestion.

    Requires the **officer** role: uploading is how figures enter the corpus, and
    a viewer is someone who reads it.

    ``supersedes`` is how a *correction* enters the corpus. CIL reissues
    statements — a provisional monthly figure is replaced by the audited annual
    one — and without naming the document being replaced the two sit side by
    side, both active, and the conflict radar flags the organisation's own
    revision as a disagreement between sources. Naming it retires the old
    version's facts to ``superseded``: still queryable, so "what did the earlier
    version say?" is still answerable, but no longer offered as the answer.
    """
    settings.ensure_dirs()

    # Validated before a byte is written. A caller naming a document they cannot
    # see, or one that does not exist, is told so instead of having the upload
    # succeed with the supersession silently dropped — which would leave two
    # active versions of the same figure and no record that anyone intended
    # otherwise. Checked under the caller's own scope, and 404 rather than 403
    # for an out-of-scope id, as everywhere else: confirming that a document
    # exists outside your scope is itself disclosure.
    if supersedes is not None:
        replaced = store.get_document(supersedes, scope)
        if replaced is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No document {supersedes!r} in your scope, so this upload "
                    "cannot be recorded as replacing it."
                ),
            )
    # The staging path is built from the generated id alone, never from
    # file.filename. A filename carrying a NUL byte, a path separator, or more
    # than the filesystem's name limit would otherwise raise ValueError/OSError
    # on open() — before the IntakeError handler below is in scope — and surface
    # as a 500 on a hostile filename. The uploader's filename is still recorded
    # and inspected as advisory metadata; it just never touches the filesystem.
    staging = settings.upload_staging_dir / f"{new_id('up')}.upload"

    written = 0
    try:
        with staging.open("wb") as sink:
            while chunk := await file.read(CHUNK_BYTES):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    # Refused mid-stream: a cap enforced after the fact would
                    # have already cost the disk and the wait.
                    raise IntakeError(
                        "too_large",
                        f"This upload exceeds the "
                        f"{settings.max_upload_bytes / 1e6:,.0f} MB limit and was "
                        "stopped part-way.",
                    )
                sink.write(chunk)

        report = inspect_upload(
            staging,
            filename=file.filename or "upload.bin",
            max_bytes=settings.max_upload_bytes,
            max_pages=settings.max_upload_pages,
        )

        # Hashed *after* sanitizing, so the content hash identifies the bytes the
        # corpus holds rather than the ones that were sent.
        blobs = get_blob_store()
        blob = blobs.put_file(staging)

        existing = store.find_document_by_hash(blob.content_hash)
        if existing is not None:
            logger.info(
                "upload deduplicated",
                document_id=existing.document_id,
                content_hash=blob.content_hash,
            )
            return UploadResponse(
                document_id=existing.document_id,
                content_hash=existing.content_hash,
                filename=existing.filename,
                doc_class=existing.doc_class.value,
                state=existing.state,
                page_count=existing.page_count,
                size_bytes=existing.size_bytes,
                created=False,
                notes=[
                    "These exact bytes are already in the corpus, so nothing was "
                    f"re-ingested. The existing document is {existing.document_id}."
                ],
            )

        document = Document(
            document_id=new_id("doc"),
            content_hash=blob.content_hash,
            filename=file.filename or "upload.bin",
            doc_class=report.doc_class,
            page_count=report.page_count,
            size_bytes=report.size_bytes,
            title=title,
            publisher_entity_id=publisher_entity_id,
            fiscal_year=fiscal_year,
            ingested_at=datetime.now(UTC),
            notes="\n".join(report.notes) or None,
            state=DocumentState.RECEIVED,
            sensitivity=sensitivity,
            blob_key=blob.content_hash,
            uploaded_by=officer.user_id,
        )

        # Registration and the first job commit together. That is the property
        # that makes a broker unnecessary: there is no instant at which a
        # document exists with nothing scheduled to process it.
        store.register_document(document)

        # The supersession commits in the same transaction as the registration
        # that justifies it. A correction that landed without retiring what it
        # corrects would leave the old figures active until someone noticed.
        facts_superseded = 0
        if supersedes is not None:
            facts_superseded = store.mark_superseded(supersedes, document.document_id)

        enqueue_next_stage(store, document, DocumentState.RECEIVED)
        store.audit.record(
            "document.uploaded",
            actor_user_id=officer.user_id,
            actor_username=officer.username,
            subject_type="document",
            subject_id=document.document_id,
            entity_scope=publisher_entity_id,
            detail={
                "filename": document.filename,
                "content_hash": blob.content_hash,
                "doc_class": report.doc_class.value,
                "pages": report.page_count,
                "stripped": list(report.stripped),
                "size_bytes": report.size_bytes,
                # Which document this replaced, and how much it retired. A
                # revision is the kind of thing someone asks about a year later.
                "supersedes": supersedes,
                "facts_superseded": facts_superseded,
            },
            request_id=getattr(request.state, "request_id", None),
            source_ip=caller_ip,
        )

        logger.info(
            "document accepted",
            document_id=document.document_id,
            doc_class=report.doc_class.value,
            pages=report.page_count,
            size_bytes=report.size_bytes,
        )
        return UploadResponse(
            document_id=document.document_id,
            content_hash=document.content_hash,
            filename=document.filename,
            doc_class=document.doc_class.value,
            state=document.state,
            page_count=document.page_count,
            size_bytes=document.size_bytes,
            created=True,
            notes=report.notes,
            supersedes=supersedes,
            facts_superseded=facts_superseded,
        )

    except IntakeError as refused:
        # Recorded even though nothing was stored: a stream of rejected uploads
        # is something an administrator should be able to see.
        store.audit.record(
            "document.refused",
            actor_user_id=officer.user_id,
            actor_username=officer.username,
            subject_type="upload",
            subject_id=file.filename,
            detail={"code": refused.code, "reason": str(refused), **refused.detail},
            request_id=getattr(request.state, "request_id", None),
            source_ip=caller_ip,
        )
        logger.warning("upload refused", code=refused.code, filename=file.filename)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": refused.code,
                "message": str(refused),
                **({"context": refused.detail} if refused.detail else {}),
            },
        ) from refused
    finally:
        staging.unlink(missing_ok=True)


class RetryRequest(BaseModel):
    """Re-run a document from a named stage."""

    stage: str = "classify"


@router.post("/documents/{document_id}/retry")
def retry_document(
    document_id: str,
    body: RetryRequest,
    store: StoreDep,
    scope: ScopeDep,
    officer: OfficerDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> dict[str, Any]:
    """Re-run a document from a stage, after a failure or a fixed extractor.

    Safe because every stage replaces its own output rather than appending to it:
    a re-run produces the same rows, not a second copy. The state is moved back
    to *before* the named stage so the normal chain carries it forward from
    there.
    """
    document = store.get_document(document_id, scope)
    if document is None:
        raise HTTPException(status_code=404, detail=f"No document {document_id!r}")

    try:
        stage = stage_by_name(body.stage)
    except ValueError as unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "unknown_stage",
                "message": str(unknown),
                "stages": [item.name for item in STAGES],
            },
        ) from unknown

    # The state a document must be in for `stage` to be the next thing that runs.
    previous = {item.job_kind: state for state, item in PIPELINE.items()}[stage.job_kind]

    try:
        require_transition(document.state, previous)
    except IllegalTransitionError as illegal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "illegal_transition", "message": str(illegal)},
        ) from illegal

    store.documents.set_state(document_id, previous)
    outcome = JobQueue(store.connection).enqueue(
        stage.job_kind,
        {"document_id": document_id},
        # A retry deliberately gets a *fresh* key, or it would collapse onto the
        # job that already ran and nothing would happen.
        idempotency_key=f"{document_id}:v{document.version}:{stage.name}:retry:"
        f"{datetime.now(UTC).isoformat(timespec='seconds')}",
    )
    store.audit.record(
        "document.retried",
        actor_user_id=officer.user_id,
        actor_username=officer.username,
        subject_type="document",
        subject_id=document_id,
        detail={"stage": stage.name, "from_state": document.state.value},
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )
    return {
        "document_id": document_id,
        "stage": stage.name,
        "state": previous.value,
        "job_id": outcome.job.job_id,
    }


@router.get("/documents/{document_id}/progress")
def document_progress(
    document_id: str, store: StoreDep, scope: ScopeDep
) -> dict[str, Any]:
    """Where a document is in the pipeline, and what each stage produced.

    The counters come from ``stage_progress``, which is written **outside** the
    stage's transaction precisely so this endpoint can answer while the stage is
    still running.

    ``extraction`` is the part an officer actually asks about: "this is a
    180-page report, why did it yield four figures?" The extractor already
    records what it refused and why, a few verbatim examples of each, and which
    of those reasons a person can do something about. That is returned split and
    labelled rather than as a raw tally, because `{"no_unit": 37}` is a
    diagnostic and "37 cells gave no unit anywhere on the table" is an answer.
    """
    document = store.get_document(document_id, scope)
    if document is None:
        raise HTTPException(status_code=404, detail=f"No document {document_id!r}")

    return {
        "document_id": document_id,
        "state": document.state.value,
        "doc_class": document.doc_class.value,
        "failed_stage": document.failed_stage,
        "failed_reason": document.failed_reason,
        "progress": document.stage_progress,
        "extraction": _extraction_summary(document.stage_progress),
        "stages": [
            {
                "name": stage.name,
                "description": stage.description,
                "completed": _stage_completed(document.state, stage.completes_to),
            }
            for stage in STAGES
        ],
    }


def _extraction_summary(progress: dict[str, Any]) -> dict[str, Any] | None:
    """The extract stage's outcome, split into what needs a person and what does not.

    Returns ``None`` before extraction has run, so a caller can tell "nothing to
    report yet" from "ran and refused nothing" — the second is a real and useful
    answer about a clean document, and collapsing the two into an empty list
    would hide it.
    """
    extract = progress.get("extract")
    if not isinstance(extract, dict):
        return None

    skipped = extract.get("skipped")
    examples = extract.get("examples")
    counts: dict[str, int] = skipped if isinstance(skipped, dict) else {}
    samples: dict[str, Any] = examples if isinstance(examples, dict) else {}

    def entries(reasons: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "reason": reason,
                "label": skip_label(reason),
                "count": int(counts[reason]),
                # A handful of verbatim cells, so "37 had no unit" can be
                # followed by *which* ones. Only for the actionable reasons:
                # five examples of an empty cell is noise.
                "examples": (
                    list(samples.get(reason, []))[:5]
                    if reason in ACTIONABLE_SKIPS
                    else []
                ),
            }
            # Sorted by count: the biggest refusal is the one worth reading first.
            for reason in sorted(reasons, key=lambda item: -int(counts[item]))
        ]

    actionable = [
        reason
        for reason, count in counts.items()
        if reason in ACTIONABLE_SKIPS and int(count) > 0
    ]
    furniture = [
        reason
        for reason, count in counts.items()
        if reason not in ACTIONABLE_SKIPS and int(count) > 0
    ]

    # The validate stage's acceptance count, carried alongside the extract
    # stage's. "22 of 26 cells became figures" says the reading went well and
    # still leaves the question an officer actually has — can I use them? Until
    # acceptance existed the answer was no and nothing said so, which is most of
    # why that defect stayed invisible.
    validate = progress.get("validate")
    accepted = validate.get("accepted") if isinstance(validate, dict) else None

    return {
        "facts": extract.get("done"),
        "candidates": extract.get("total"),
        "tables": extract.get("tables"),
        "accepted": accepted,
        # Refusals a reviewer can do something about: find the unit, resolve the
        # ambiguity, re-run against a better layout reader.
        "needs_attention": entries(actionable),
        # Total rows, headers, blanks. Counted so the arithmetic adds up, and
        # separated so they do not read as problems — refusing a total row is the
        # extractor working correctly, not failing.
        "ignored": entries(furniture),
    }


#: Lifecycle order, for deciding whether a stage is behind the current state.
_ORDER = [
    DocumentState.RECEIVED,
    DocumentState.CLASSIFIED,
    DocumentState.DIGITIZED,
    DocumentState.EXTRACTED,
    DocumentState.NORMALIZED,
    DocumentState.VALIDATED,
    DocumentState.INDEXED,
    DocumentState.READY,
]


def _stage_completed(current: DocumentState, completes_to: DocumentState) -> bool:
    if current in {DocumentState.FAILED, DocumentState.QUARANTINED}:
        return False
    try:
        return _ORDER.index(current) >= _ORDER.index(completes_to)
    except ValueError:
        return False
