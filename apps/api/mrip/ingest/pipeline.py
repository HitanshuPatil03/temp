"""The ingestion pipeline: one job handler per stage.

Each stage is a job, and the chain is driven by the **queue** rather than by one
long function. That is what makes a 400-page annual report survivable: a worker
that dies during OCR loses one stage's work, not the document, and the next
worker picks up exactly where the lease expired.

Three rules hold for every stage here, and they are why a `kill -9` mid-ingest
is uneventful:

**A stage's output and its state transition commit together.** Both happen in
the job's transaction (see :mod:`mrip.jobs.worker`), so a document is never
recorded as ``digitized`` without its evidence rows, and never has evidence rows
that no state accounts for.

**A stage replaces its own output.** Re-running digitize deletes this document
version's rows *for that extraction method* and writes them again
(:meth:`~mrip.db.repositories.documents.EvidenceRepository.replace_stage`). A
retry therefore produces the same rows, not a second copy — which is what makes
"resume after a crash" a safe operation rather than a duplication risk.

**The next stage is enqueued inside the same transaction.** The queue lives in
the same database (ARCHITECTURE §4), so there is no window where a stage has
committed and its successor was lost.

Progress is published *outside* the transaction, on purpose: a page counter that
only becomes visible when the stage finishes is not progress.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mrip import log
from mrip.auth.scope import Scope
from mrip.blobs import BlobNotFoundError, get_blob_store
from mrip.db import Store
from mrip.ingest import digitize as digitizer
from mrip.ingest.lifecycle import next_job_for, require_transition
from mrip.jobs.queue import JobQueue, PermanentJobError
from mrip.jobs.registry import JobContext, handler
from mrip.schemas import Document, DocumentClass, DocumentState, ExtractionMethod

__all__ = [
    "classify_document",
    "digitize_document",
    "enqueue_next_stage",
    "extract_document",
    "index_document",
    "normalize_document",
    "validate_document",
]

logger = log.get_logger("mrip.pipeline")

#: Every stage reads the document under an unrestricted scope. The reason is
#: structural rather than lax: a worker has no user behind it, and a document is
#: processed because it was accepted, not because someone can see it. Access
#: control is enforced when a *person* reads the result.
PIPELINE_SCOPE = Scope.unrestricted("ingestion worker: acts on its own job's document")


def _load(ctx: JobContext) -> Document:
    """The document this job is about, or a permanent failure."""
    document_id = ctx.require("document_id")
    document = ctx.store.get_document(document_id, PIPELINE_SCOPE)
    if document is None:
        raise PermanentJobError(
            f"Document {document_id!r} does not exist. It was deleted after this "
            "job was enqueued; there is nothing to process."
        )
    return document


@contextmanager
def _local_copy(document: Document) -> Iterator[Path]:
    """The document's bytes on local disk, for parsers that need a file.

    Copied out of the blob store rather than read from it directly: PyMuPDF and
    pdfplumber both want random access, and an S3-backed store would not give
    them that. The copy is deleted on the way out even if the stage raises.
    """
    if not document.blob_key:
        raise PermanentJobError(
            f"Document {document.document_id!r} has no blob key, so its bytes "
            "were never stored. It cannot be processed; re-upload the file."
        )

    store = get_blob_store()
    suffix = Path(document.filename).suffix or ".bin"
    # `delete=False` and an explicit unlink in the `finally`: the file has to
    # outlive this handle so a parser can reopen it by name, which is exactly
    # what a `with` block here would prevent. The cleanup is the contextmanager's
    # own, so it still happens when the stage raises.
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
    path = Path(handle.name)
    try:
        with handle:
            try:
                with store.open(document.blob_key) as source:
                    while chunk := source.read(1024 * 1024):
                        handle.write(chunk)
            except BlobNotFoundError as missing:
                raise PermanentJobError(
                    f"The stored bytes for {document.document_id!r} are missing "
                    f"from the blob store (key {document.blob_key}). The document "
                    "row survived a restore that the blob volume did not."
                ) from missing
        yield path
    finally:
        path.unlink(missing_ok=True)


def enqueue_next_stage(store: Store, document: Document, state: DocumentState) -> None:
    """Queue whatever runs after ``state``, in the caller's transaction.

    Idempotency key is ``(document, version, stage)``, so a stage that is somehow
    run twice does not enqueue its successor twice — the second call collapses
    onto the job already recorded.
    """
    stage = next_job_for(state)
    if stage is None:
        return
    JobQueue(store.connection).enqueue(
        stage.job_kind,
        {"document_id": document.document_id},
        idempotency_key=f"{document.document_id}:v{document.version}:{stage.name}",
    )


def _advance(
    ctx: JobContext,
    document: Document,
    target: DocumentState,
    *,
    progress: dict[str, Any] | None = None,
) -> None:
    """Move the document forward, refusing an illegal transition."""
    require_transition(document.state, target)
    ctx.store.documents.set_state(document.document_id, target, progress=progress)
    enqueue_next_stage(ctx.store, document, target)


# --------------------------------------------------------------------- classify


@handler("document.classify")
def classify_document(ctx: JobContext) -> None:
    """Decide what this document *is*, and therefore how to read it.

    The boundary already sniffed the media type; this stage answers the harder
    question for PDFs — text layer, scanned images, or a mix. Getting it wrong
    in either direction is expensive: OCRing a text-layer report wastes hours and
    degrades perfectly good text, while treating a scan as text yields an empty
    document.
    """
    document = _load(ctx)

    if document.doc_class not in {
        DocumentClass.TEXT_PDF,
        DocumentClass.SCANNED_PDF,
        DocumentClass.MIXED_PDF,
    }:
        # Spreadsheets, Word files and images need no page-level decision.
        _advance(ctx, document, DocumentState.CLASSIFIED)
        return

    with _local_copy(document) as path:
        doc_class, profiles = digitizer.classify_pdf(path)

    needs_ocr = [profile.page for profile in profiles if not profile.has_text_layer]
    ctx.store.documents.set_class(document.document_id, doc_class)
    logger.info(
        "document classified",
        document_id=document.document_id,
        doc_class=doc_class.value,
        pages=len(profiles),
        pages_needing_ocr=len(needs_ocr),
    )
    _advance(
        ctx,
        document,
        DocumentState.CLASSIFIED,
        progress={
            "classify": {
                "done": len(profiles),
                "total": len(profiles),
                "pages_needing_ocr": len(needs_ocr),
            }
        },
    )


# --------------------------------------------------------------------- digitize


@handler("document.digitize")
def digitize_document(ctx: JobContext) -> None:
    """Read the document into evidence rows: text lines, table cells, sheet cells.

    Each source of rows is written with :meth:`replace_stage` keyed on its own
    extraction method, so the text pass and the table pass do not overwrite each
    other and either can be re-run alone.
    """
    document = _load(ctx)

    with _local_copy(document) as path:
        if document.doc_class is DocumentClass.SPREADSHEET:
            rows = list(
                digitizer.digitize_spreadsheet(
                    path, document.document_id, document.version
                )
            )
            written = ctx.store.evidence.replace_stage(
                document.document_id,
                document.version,
                ExtractionMethod.SPREADSHEET_CELL.value,
                rows,
            )
            _advance(
                ctx,
                document,
                DocumentState.DIGITIZED,
                progress={"digitize": {"done": written, "total": written}},
            )
            return

        if document.doc_class is DocumentClass.DOCX:
            rows = list(
                digitizer.digitize_docx(path, document.document_id, document.version)
            )
            written = ctx.store.evidence.replace_stage(
                document.document_id,
                document.version,
                ExtractionMethod.PDF_TEXT_LAYER.value,
                [row for row in rows if row["kind"] == "line"],
            ) + ctx.store.evidence.replace_stage(
                document.document_id,
                document.version,
                ExtractionMethod.TABLE_LATTICE.value,
                [row for row in rows if row["kind"] == "table_cell"],
            )
            _advance(
                ctx,
                document,
                DocumentState.DIGITIZED,
                progress={"digitize": {"done": written, "total": written}},
            )
            return

        if document.doc_class is DocumentClass.IMAGE:
            # A photographed table is a real input. It goes through the same OCR
            # implementation as a scanned page — and fails with the same
            # actionable sentence when the OCR extra is not installed, rather
            # than being accepted at the boundary and dying here with "no
            # digitizer", which is what it used to do.
            rows = list(
                digitizer.digitize_image(path, document.document_id, document.version)
            )
            written = ctx.store.evidence.replace_stage(
                document.document_id,
                document.version,
                ExtractionMethod.OCR.value,
                rows,
            )
            _advance(
                ctx,
                document,
                DocumentState.DIGITIZED,
                progress={"digitize": {"done": written, "total": written}},
            )
            return

        if document.doc_class not in {
            DocumentClass.TEXT_PDF,
            DocumentClass.SCANNED_PDF,
            DocumentClass.MIXED_PDF,
        }:
            raise PermanentJobError(
                f"No digitizer for {document.doc_class.value!r}. This class is "
                "accepted at the upload boundary, so reaching here means the two "
                "have drifted apart — every class the boundary admits must have a "
                "digitizer."
            )

        _, profiles = digitizer.classify_pdf(path)
        text_pages = [p.page for p in profiles if p.has_text_layer]
        scan_pages = [p.page for p in profiles if not p.has_text_layer]
        # Three sets, not two. Prose is read only off pages whose text is the
        # document's own (``text_pages``) and every other page is re-recognised
        # (``scan_pages``) — but *table geometry* is available from any page that
        # carries positioned text, including one whose layer is OCR output, and
        # withholding it there discarded the table on 31% of the corpus's
        # statements for no protective benefit. See ``PageProfile``.
        grid_pages = [p.page for p in profiles if p.has_text_objects]
        recognised_pages = frozenset(
            p.page for p in profiles if p.has_text_objects and p.recognised_text
        )

        def report(done: int, total: int) -> None:
            ctx.progress(document.document_id, "digitize", done, total)

        text_rows = list(
            digitizer.digitize_pdf_text(
                path,
                document.document_id,
                document.version,
                pages=text_pages,
                on_progress=report,
            )
        )
        ctx.store.evidence.replace_stage(
            document.document_id,
            document.version,
            ExtractionMethod.PDF_TEXT_LAYER.value,
            text_rows,
        )

        if scan_pages:
            ocr_rows = list(
                digitizer.digitize_scanned_pages(
                    path,
                    document.document_id,
                    document.version,
                    pages=scan_pages,
                    on_progress=report,
                )
            )
            ctx.store.evidence.replace_stage(
                document.document_id,
                document.version,
                ExtractionMethod.OCR.value,
                ocr_rows,
            )

        # Tables are found over every page carrying positioned text. A *true*
        # scan is excluded because pdfplumber has nothing to cluster on a bare
        # image; a page with a recognised layer is included, because its words
        # have real coordinates, and each cell from one is marked so no reader
        # mistakes it for the document's own text.
        table_rows = list(
            digitizer.digitize_pdf_tables(
                path,
                document.document_id,
                document.version,
                pages=grid_pages,
                recognised_pages=recognised_pages,
            )
        )
        for method in (ExtractionMethod.TABLE_LATTICE, ExtractionMethod.TABLE_STREAM):
            ctx.store.evidence.replace_stage(
                document.document_id,
                document.version,
                method.value,
                [row for row in table_rows if row["extraction_method"] == method.value],
            )

    total_rows = len(text_rows) + len(table_rows)
    logger.info(
        "document digitized",
        document_id=document.document_id,
        text_rows=len(text_rows),
        table_cells=len(table_rows),
        ocr_pages=len(scan_pages),
    )
    _advance(
        ctx,
        document,
        DocumentState.DIGITIZED,
        progress={"digitize": {"done": total_rows, "total": total_rows}},
    )


# ---------------------------------------------------------------------- extract


@handler("document.extract")
def extract_document(ctx: JobContext) -> None:
    """Turn table cells into facts, and record what was refused.

    The skip report is stored on the document rather than logged: "37 cells had
    no unit" is a work item for whoever owns that filing, and a log line is not
    where they will find it.
    """
    from mrip.facts.extract import extract_facts

    document = _load(ctx)

    cells = ctx.store.evidence.for_document(
        document.document_id, document.version, kinds=("table_cell", "sheet_cell")
    )
    lines = ctx.store.evidence.page_text(document.document_id, document.version)

    report = extract_facts(document, cells, page_lines=lines)

    # Replace this document version's facts rather than appending: a re-run after
    # a fixed extractor must not leave the old reading of the same cell behind.
    ctx.store.facts.delete_for_document(document.document_id, document.version)
    ctx.store.insert_facts(report.facts)

    _advance(
        ctx,
        document,
        DocumentState.EXTRACTED,
        progress={
            "extract": {
                "done": len(report.facts),
                "total": len(report.facts) + sum(report.skipped.values()),
                "tables": report.tables_seen,
                "skipped": dict(report.skipped),
                "examples": dict(report.examples),
            }
        },
    )


# -------------------------------------------------------------------- normalize


@handler("document.normalize")
def normalize_document(ctx: JobContext) -> None:
    """Route this document's weak figures to review.

    Extraction already canonicalised units and periods — that is what the
    normalizers do, and a fact cannot be built without them. What is left for
    this stage is the *judgement*: which of the resulting facts are too weak to
    stand without a person looking, by the deployment's configured threshold.

    Kept as its own stage rather than folded into extraction because the
    threshold is a setting. Lowering it should pull previously-accepted figures
    into review without re-reading a single PDF, and that is only possible if the
    decision is separable from the reading.
    """
    document = _load(ctx)
    flagged = ctx.store.facts.flag_low_confidence_for_document(
        document.document_id,
        document.version,
        ctx.store.settings.review_confidence_threshold,
    )
    logger.info(
        "document normalized",
        document_id=document.document_id,
        flagged_for_review=flagged,
    )
    _advance(
        ctx,
        document,
        DocumentState.NORMALIZED,
        progress={"normalize": {"done": flagged, "total": flagged}},
    )


# --------------------------------------------------------------------- validate


@handler("document.validate")
def validate_document(ctx: JobContext) -> None:
    """Run the validation rules and the conflict radar over this document.

    Conflict detection runs **corpus-wide within this document's entities**, not
    within the document: the disagreement worth catching is between a figure here
    and the same figure in last quarter's filing from a different subsidiary.
    """
    from mrip.validate.rules import run_rules

    document = _load(ctx)
    findings = run_rules(ctx.store, document)
    conflicts = ctx.store.detect_conflicts(PIPELINE_SCOPE)

    logger.info(
        "document validated",
        document_id=document.document_id,
        findings=len(findings),
        open_conflicts=len(conflicts),
    )
    _advance(
        ctx,
        document,
        DocumentState.VALIDATED,
        progress={
            "validate": {
                "done": len(findings),
                "total": len(findings),
                "findings": [finding.as_dict() for finding in findings[:20]],
                "open_conflicts": len(conflicts),
            }
        },
    )


# ------------------------------------------------------------------------ index


@handler("document.index")
def index_document(ctx: JobContext) -> None:
    """Make the document findable, and fill in what it can say about itself.

    Lexical search needs nothing here — ``evidence.search_vector`` is a generated
    column, so it was written with the rows. What this stage does is derive the
    document's *facets* from what was extracted: which entities it covers, which
    metrics, which fiscal years. That is what the document list filters on, and
    deriving it from the facts is more reliable than trusting a filename.
    """
    document = _load(ctx)
    facets = ctx.store.facts.document_facets(document.document_id, document.version)

    # A document that never had a fiscal year takes the one its facts agree on.
    if not document.fiscal_year and len(facets["fiscal_years"]) == 1:
        ctx.store.documents.set_fiscal_year(
            document.document_id, facets["fiscal_years"][0]
        )
    if not document.publisher_entity_id and len(facets["entities"]) == 1:
        ctx.store.documents.set_publisher(document.document_id, facets["entities"][0])

    logger.info("document indexed", document_id=document.document_id, **facets)
    _advance(
        ctx,
        document,
        DocumentState.INDEXED,
        progress={"index": facets},
    )
    # `indexed → ready` has no stage of its own: there is nothing left to do, and
    # a stage that exists only to set a state is a stage that can fail for no
    # reason.
    require_transition(DocumentState.INDEXED, DocumentState.READY)
    ctx.store.documents.set_state(document.document_id, DocumentState.READY)
