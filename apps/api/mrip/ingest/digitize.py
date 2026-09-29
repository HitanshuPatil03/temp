"""Digitization: bytes → evidence rows.

This stage does exactly one thing and refuses to do more: **record what is on
the page, with where it is**. No interpretation, no guessing which number is
production and which is despatch — that is the extractor's job, one stage later.
The separation is what makes the evidence trail trustworthy: an evidence row is
a verbatim quote plus a coordinate, and nothing in it depends on our reading of
the document.

Three paths, chosen by what the file actually contains:

**Text-layer PDFs** (the majority of CIL filings) go through PyMuPDF, which
gives a line's text and its bounding box directly from the PDF's own operators.
That is the highest-fidelity source there is — no recognition step, therefore no
OCR confidence, therefore no OCR error.

**Tables** go through pdfplumber a second time over the same pages. Two passes
over one file is deliberate: PyMuPDF sees lines of text, pdfplumber sees ruling
lines and cell boundaries, and a table's *structure* is the thing a figure needs
(`r3c2` means nothing without it). Cells are emitted with their row and column
index, so a later stage can say "the value under the FY2024-25 column, in the
SECL row".

**Spreadsheets** are read cell by cell with their real cell reference (`C14`),
which is the strongest evidence locator this system has: it is the address the
source system itself uses.

Scanned pages need OCR, which is an optional install (`pip install -e .[ocr]`).
A scanned document on a deployment without it **fails with that sentence** rather
than being ingested as an empty document — the same rule as the encrypted PDF at
the boundary.

**A text layer is not automatically trustworthy.** Several real CIL monthly
production statements are scans whose OCR output was saved back into the PDF.
PyMuPDF reports a healthy text layer for those pages and the text says ``IUCL``
where the paper says ``MCL``, with the decimal points dropped from the columns
beside it. A page whose text layer carries OCR shape-confusions is therefore
classed as needing recognition, not as text — see
:func:`text_layer_is_recognised`. Reading it as native text would not fail; it
would produce a wrong figure, attributed to the wrong subsidiary, with a citation
pointing at a real page, which is the worst outcome available to this system.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mrip import log
from mrip.schemas import DocumentClass, ExtractionMethod

__all__ = [
    "MIN_CHARS_FOR_TEXT_LAYER",
    "OCR_ARTEFACTS_PER_PAGE",
    "PageProfile",
    "classify_pdf",
    "digitize_docx",
    "digitize_image",
    "digitize_pdf_tables",
    "digitize_pdf_text",
    "digitize_spreadsheet",
    "text_layer_is_recognised",
]

logger = log.get_logger("mrip.digitize")

#: A page with fewer extractable characters than this is treated as an image of
#: a page rather than a page. Chosen well below a sparse table of figures (a
#: production table is a few hundred characters) and well above the page numbers
#: and running headers that a scanner's text layer sometimes carries.
MIN_CHARS_FOR_TEXT_LAYER = 120

#: Cap on a stored snippet. The column allows 1000; a line longer than this is a
#: run-on paragraph whose tail adds nothing to a citation.
_MAX_SNIPPET = 1000

# ------------------------------------------------- text-layer quality

#: Substitutions a bad OCR engine makes, which a *real* text layer essentially
#: never contains. These are not spelling mistakes — they are shape confusions,
#: and each one is evidence that the "text layer" was written by a recogniser
#: rather than by the program that typeset the page.
#:
#: This matters more here than in most document pipelines. Several CIL monthly
#: production statements are scans that somebody ran through OCR and saved *with*
#: the text layer embedded. PyMuPDF then reports a perfectly good text layer, and
#: the page says:
#:
#:     IUCL   14.3   17.9   -20.1
#:
#: where the paper says ``MCL 14.3``. ``M`` became ``IVI`` became ``IUCL``, and
#: the decimal points are gone from the neighbouring columns — ``63.5`` reads as
#: ``635``. Trusting that text layer does not produce a failure; it produces a
#: *fact*, attributed to the wrong subsidiary, with a tenfold error, carrying a
#: citation that points at a real page. That is the single worst thing this
#: platform could do, so a page whose text layer looks recognised is routed to
#: OCR (which records its own confidence) instead of being read as truth.
_OCR_ARTEFACTS: tuple[str, ...] = (
    "IVI",  # M
    "I\\/I",  # M
    "|ONAL",  # TIONAL
    "TI-IE",  # THE
    "TFIE",  # THE
    "0F ",  # OF
    "1'",  # T
    "l\\/l",  # M
    "VIANCE",  # MANCE, from PERFORMANCE
    "COIVI",  # COM
    "PERFORI",  # PERFORM
)

#: A page needs at least this many artefact hits before it is called recognised.
#: One hit is a coincidence — a table of mine names can legitimately contain
#: "0F " if a column is a code. Two independent hits on one page is a pattern.
OCR_ARTEFACTS_PER_PAGE = 2

#: A number broken by a space *within one line*: ``61 .2``, ``191 0``, ``1 1.8``.
#:
#: This is the second artefact class, and on the CIL production series it is the
#: more common one. A typesetter does not put a space inside a figure; a
#: recogniser does it constantly, because the gap between glyphs in a scanned
#: table is ambiguous. It is more dangerous than the letter confusions above,
#: because a letter confusion produces an entity name nothing resolves — which
#: fails loudly — while ``61 .2`` parses cleanly as ``61`` and loses 0.2 million
#: tonnes without a word.
#:
#: Anchored to reject legitimate text: a bare ``2 3`` inside prose ("clause 2 3")
#: is caught too, which is acceptable — the page still goes to OCR, and OCR of a
#: clean page reproduces it.
_SPLIT_NUMBER = re.compile(r"(?<![\d.])\d+ +\.\d|(?<![\d.])\d+ +\d(?![\d])")

#: Split numbers needed on a page before the layer is distrusted. Three, not two:
#: a scanned annexure routinely shows a dozen, whereas one or two can come from a
#: line wrap in a paragraph of prose.
SPLIT_NUMBERS_PER_PAGE = 3


def text_layer_is_recognised(text: str) -> bool:
    """Whether this text layer looks like OCR output rather than real text.

    Two independent signals, either of which is enough: letter shape-confusions
    (:data:`_OCR_ARTEFACTS`) and numbers broken by spaces (:data:`_SPLIT_NUMBER`).
    Measured on real CIL statements, the June 2026 production statement scores 23
    split numbers and the August 2023 one scores none — the separation is not
    marginal.
    """
    if not text:
        return False
    hits = sum(text.count(artefact) for artefact in _OCR_ARTEFACTS)
    if hits >= OCR_ARTEFACTS_PER_PAGE:
        return True
    # Line by line: `\s` would match the newline between two legitimate table
    # cells and call every table a scan.
    splits = sum(len(_SPLIT_NUMBER.findall(line)) for line in text.splitlines())
    return splits >= SPLIT_NUMBERS_PER_PAGE


ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class PageProfile:
    """What one page turned out to contain. Drives the class decision."""

    page: int
    characters: int
    images: int
    #: Whether the characters on this page look like OCR output. A page with a
    #: *recognised* text layer is treated as a scan: the text is there, but it
    #: cannot be trusted as the document's own, so it is re-recognised by an
    #: engine that reports a confidence rather than read as ground truth.
    recognised_text: bool = False

    @property
    def has_text_objects(self) -> bool:
        """Whether this page carries positioned text at all, trusted or not.

        Deliberately distinct from :attr:`has_text_layer`, which is about *trust*.
        This one is about geometry: a page saved with an OCR layer has real text
        objects at real coordinates, and :func:`digitize_pdf_tables` clusters word
        positions rather than ruling lines, so it can recover the table grid from
        such a page even though the glyphs themselves must not be read as truth.

        Conflating the two costs real documents. Measured over the corpus's 191
        statement-like PDFs, 60 of them — 31% — have at least one page with a
        recognised layer, and on the CIL monthly statements that is the single
        page carrying the table, so treating it as a bare scan yields nothing at
        all from the document.
        """
        return self.characters >= MIN_CHARS_FOR_TEXT_LAYER

    @property
    def has_text_layer(self) -> bool:
        """Whether this page's own text can be read as ground truth."""
        return self.has_text_objects and not self.recognised_text

    @property
    def needs_ocr(self) -> bool:
        return not self.has_text_layer


def classify_pdf(path: Path) -> tuple[DocumentClass, list[PageProfile]]:
    """Decide whether a PDF can be read or has to be recognised.

    Returns the class *and* the per-page profile, because the digitizer needs to
    know which individual pages need OCR — a 200-page annual report with twelve
    scanned annexures is the normal case, not an exception, and treating the
    whole document as scanned would put it through OCR 188 times for nothing.
    """
    import pymupdf

    profiles: list[PageProfile] = []
    with pymupdf.open(path) as document:
        for index, page in enumerate(document, start=1):
            text = page.get_text("text") or ""
            profiles.append(
                PageProfile(
                    page=index,
                    characters=len(text.strip()),
                    images=len(page.get_images(full=False)),
                    recognised_text=text_layer_is_recognised(text),
                )
            )

    if not profiles:
        return DocumentClass.UNKNOWN, profiles

    recognised = sum(1 for profile in profiles if profile.recognised_text)
    if recognised:
        logger.warning(
            "text layer looks recognised, not native; those pages go through OCR",
            path=str(path),
            pages=recognised,
            of=len(profiles),
        )

    with_text = sum(1 for profile in profiles if profile.has_text_layer)
    if with_text == len(profiles):
        return DocumentClass.TEXT_PDF, profiles
    if with_text == 0:
        return DocumentClass.SCANNED_PDF, profiles
    return DocumentClass.MIXED_PDF, profiles


def digitize_pdf_text(
    path: Path,
    document_id: str,
    version: int,
    *,
    pages: list[int] | None = None,
    on_progress: ProgressCallback | None = None,
) -> Iterator[dict[str, Any]]:
    """Text lines with their bounding boxes, one evidence row each.

    A *line*, not a word and not a paragraph. Words would make a citation
    unreadable ("the value in span 4,192"); paragraphs would make a bounding box
    that highlights half a page. A line is what a person points at when they say
    "there".
    """
    import pymupdf

    with pymupdf.open(path) as document:
        wanted = pages or list(range(1, document.page_count + 1))
        total = len(wanted)
        for position, page_number in enumerate(wanted, start=1):
            page = document[page_number - 1]
            layout = page.get_text("dict")
            for block in layout.get("blocks", ()):
                # type 0 is text; type 1 is an image, which has no text to quote.
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", ()):
                    text = "".join(
                        span.get("text", "") for span in line.get("spans", ())
                    ).strip()
                    if not text:
                        continue
                    x0, y0, x1, y1 = line["bbox"]
                    yield {
                        "document_id": document_id,
                        "document_version": version,
                        "page": page_number,
                        "kind": "line",
                        "text": text[:_MAX_SNIPPET],
                        "bbox_x0": x0,
                        "bbox_y0": y0,
                        "bbox_x1": x1,
                        "bbox_y1": y1,
                        "extraction_method": ExtractionMethod.PDF_TEXT_LAYER.value,
                        # No OCR ran, so there is no OCR confidence. Left null
                        # rather than set to 1.0: "did not apply" and "perfect"
                        # are different claims, and `LEAST` ignores nulls.
                    }
            if on_progress is not None:
                on_progress(position, total)


def digitize_pdf_tables(
    path: Path,
    document_id: str,
    version: int,
    *,
    pages: list[int] | None = None,
    recognised_pages: Collection[int] | None = None,
    on_progress: ProgressCallback | None = None,
) -> Iterator[dict[str, Any]]:
    """Table cells, with row and column indices and a per-table id.

    Uses pdfplumber's ruling-line strategy first and falls back to its
    whitespace-alignment strategy, recording **which one produced the cell** as
    the extraction method. That distinction is not bookkeeping: a lattice table
    has drawn borders and its structure is certain, while a stream table's
    columns are inferred from alignment and its structure is a guess. The
    reviewer sees which, and the accuracy of each can be measured separately.

    ``recognised_pages`` names the pages whose text layer is OCR output rather
    than the document's own (see :func:`text_layer_is_recognised`). Cells from
    those pages are still produced — the grid is real even when the glyphs are
    suspect — but each is stamped ``text_layer_recognised``, so a reader that
    cannot check a figure against anything can decline it while one that verifies
    against the page's own printed growth column can use it.
    """
    import pdfplumber

    suspect = frozenset(recognised_pages or ())

    with pdfplumber.open(path) as document:
        wanted = pages or list(range(1, len(document.pages) + 1))
        total = len(wanted)
        for position, page_number in enumerate(wanted, start=1):
            page = document.pages[page_number - 1]
            recognised = page_number in suspect

            found = page.find_tables()
            method = ExtractionMethod.TABLE_LATTICE
            if not found:
                found = page.find_tables(
                    {
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                    }
                )
                method = ExtractionMethod.TABLE_STREAM

            for table_index, table in enumerate(found, start=1):
                table_id = f"p{page_number}t{table_index}"
                try:
                    rows = table.extract()
                except Exception:  # pdfplumber raises assorted geometry errors
                    logger.warning(
                        "table could not be extracted",
                        document_id=document_id,
                        page=page_number,
                        table_id=table_id,
                    )
                    continue

                x0, top, x1, bottom = table.bbox
                for row_index, row in enumerate(rows):
                    for col_index, cell in enumerate(row):
                        value = (cell or "").strip()
                        if not value:
                            # An empty cell carries no evidence. Its *position*
                            # still matters for reading the table, which is why
                            # row and column indices are absolute rather than
                            # counted over non-empty cells.
                            continue
                        yield {
                            "document_id": document_id,
                            "document_version": version,
                            "page": page_number,
                            "kind": "table_cell",
                            "table_id": table_id,
                            "cell_ref": f"r{row_index}c{col_index}",
                            "row_idx": row_index,
                            "col_idx": col_index,
                            "text": value[:_MAX_SNIPPET],
                            # The table's box, not the cell's: pdfplumber does not
                            # give a reliable per-cell rectangle for stream
                            # tables, and a wrong highlight is worse than a
                            # coarse one.
                            "bbox_x0": x0,
                            "bbox_y0": top,
                            "bbox_x1": x1,
                            "bbox_y1": bottom,
                            "extraction_method": method.value,
                            "text_layer_recognised": recognised,
                        }
            if on_progress is not None:
                on_progress(position, total)


def digitize_spreadsheet(
    path: Path,
    document_id: str,
    version: int,
    *,
    on_progress: ProgressCallback | None = None,
) -> Iterator[dict[str, Any]]:
    """Sheet cells, addressed the way the source system addresses them.

    ``C14`` on sheet "Production" is a stronger locator than any coordinate: a
    reviewer can open the workbook and land on the same cell. The sheet name is
    the ``table_id``, so one workbook's sheets stay distinguishable.

    Read with ``data_only=True``, so a formula cell yields the *cached value* the
    source system computed. A formula's text is not a figure, and recomputing it
    here would mean this platform inventing a number — which is the one thing it
    does not do.
    """
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = workbook.sheetnames
        for position, name in enumerate(sheets, start=1):
            sheet = workbook[name]
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    text = str(cell.value).strip()
                    if not text:
                        continue
                    yield {
                        "document_id": document_id,
                        "document_version": version,
                        # A workbook has no pages; the sheet's ordinal stands in
                        # so the evidence viewer has something to page through.
                        "page": position,
                        "kind": "sheet_cell",
                        "table_id": name,
                        "cell_ref": cell.coordinate,
                        "row_idx": cell.row,
                        "col_idx": cell.column,
                        "text": text[:_MAX_SNIPPET],
                        "extraction_method": ExtractionMethod.SPREADSHEET_CELL.value,
                    }
            if on_progress is not None:
                on_progress(position, len(sheets))
    finally:
        workbook.close()


def digitize_docx(
    path: Path,
    document_id: str,
    version: int,
    *,
    on_progress: ProgressCallback | None = None,
) -> Iterator[dict[str, Any]]:
    """Paragraphs and table cells from a Word document.

    Word files are a real CIL input — a draft parliamentary answer or a monthly
    note is written in Word long before it becomes a PDF — and the PS names
    "digital documents" explicitly.

    A .docx has **no pages**: pagination is computed by the renderer, not stored
    in the file. So `page` is left null rather than invented, and the locator
    leans on the paragraph index and the table cell reference instead. Claiming
    "page 4" for a position Word would have to lay out to know is exactly the
    kind of plausible-but-unfounded detail this platform refuses elsewhere.
    """
    import docx

    document = docx.Document(str(path))

    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text.strip()
        if not text:
            continue
        yield {
            "document_id": document_id,
            "document_version": version,
            "page": None,
            "kind": "line",
            # The paragraph's own ordinal is the locator: "¶12" is something a
            # reviewer can find with Ctrl+Home and twelve presses of ↓.
            "cell_ref": f"p{index}",
            "row_idx": index,
            "text": text[:_MAX_SNIPPET],
            "extraction_method": ExtractionMethod.PDF_TEXT_LAYER.value,
        }

    for table_index, table in enumerate(document.tables, start=1):
        table_id = f"t{table_index}"
        for row_index, row in enumerate(table.rows):
            for col_index, cell in enumerate(row.cells):
                text = cell.text.strip()
                if not text:
                    continue
                yield {
                    "document_id": document_id,
                    "document_version": version,
                    "page": None,
                    "kind": "table_cell",
                    "table_id": table_id,
                    "cell_ref": f"r{row_index}c{col_index}",
                    "row_idx": row_index,
                    "col_idx": col_index,
                    "text": text[:_MAX_SNIPPET],
                    # A Word table's structure is declared in the file, not
                    # inferred from ruling lines — as certain as a lattice table.
                    "extraction_method": ExtractionMethod.TABLE_LATTICE.value,
                }
        if on_progress is not None:
            on_progress(table_index, len(document.tables))


def digitize_image(
    path: Path,
    document_id: str,
    version: int,
    *,
    on_progress: ProgressCallback | None = None,
) -> Iterator[dict[str, Any]]:
    """OCR a standalone image — a photographed or scanned table.

    Wrapped as a one-page PDF so there is exactly one OCR implementation to
    maintain and one place where the pixel-to-point scaling can be wrong.
    """
    import pymupdf

    with pymupdf.open() as wrapper:
        rect = pymupdf.open(path)  # PyMuPDF opens images as documents
        with rect:
            pdf_bytes = rect.convert_to_pdf()
        wrapper.insert_pdf(pymupdf.open("pdf", pdf_bytes))
        temporary = path.with_suffix(".ocr.pdf")
        wrapper.save(temporary)

    try:
        yield from digitize_scanned_pages(
            temporary,
            document_id,
            version,
            pages=[1],
            on_progress=on_progress,
        )
    finally:
        temporary.unlink(missing_ok=True)


def ocr_available() -> bool:
    """Whether the OCR extra is installed.

    Checked rather than assumed, because OCR is an optional install: the runtime
    weighs a few hundred megabytes and an air-gapped deployment that only handles
    born-digital filings should not have to carry it.
    """
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def digitize_scanned_pages(
    path: Path,
    document_id: str,
    version: int,
    *,
    pages: list[int],
    on_progress: ProgressCallback | None = None,
) -> Iterator[dict[str, Any]]:
    """OCR the pages that have no text layer.

    Each recognised line carries its **own** confidence, from the recogniser,
    into ``ocr_confidence`` — which is what later becomes the ``ocr`` stage of a
    fact's decomposed confidence. A single document-level score would hide
    exactly the case that matters: one smudged column in an otherwise clean scan.
    """
    if not ocr_available():
        raise RuntimeError(
            "This document has scanned pages, which need OCR, but the OCR extra "
            "is not installed on this deployment. Install it with "
            "`pip install -e '.[ocr]'` and retry the document — it is not "
            "ingested as an empty document."
        )

    import pymupdf
    from rapidocr import RapidOCR

    engine = RapidOCR()
    with pymupdf.open(path) as document:
        total = len(pages)
        for position, page_number in enumerate(pages, start=1):
            page = document[page_number - 1]
            # 300 dpi. Below ~200 the recogniser starts losing digits in a dense
            # table, and a misread digit in a production figure is the single
            # worst failure this platform can have.
            pixmap = page.get_pixmap(dpi=300)
            result = engine(pixmap.tobytes("png"))

            scale = 72.0 / 300.0  # image pixels back to PDF points
            for box, text, score in _iter_ocr_results(result):
                cleaned = text.strip()
                if not cleaned:
                    continue
                xs = [point[0] for point in box]
                ys = [point[1] for point in box]
                yield {
                    "document_id": document_id,
                    "document_version": version,
                    "page": page_number,
                    "kind": "line",
                    "text": cleaned[:_MAX_SNIPPET],
                    "bbox_x0": min(xs) * scale,
                    "bbox_y0": min(ys) * scale,
                    "bbox_x1": max(xs) * scale,
                    "bbox_y1": max(ys) * scale,
                    "ocr_confidence": float(score),
                    "extraction_method": ExtractionMethod.OCR.value,
                }
            if on_progress is not None:
                on_progress(position, total)


def _iter_ocr_results(result: Any) -> Iterator[tuple[list[list[float]], str, float]]:
    """Normalise RapidOCR's output shape.

    The library has changed its return type across versions (a list of triples,
    then an object with parallel arrays). Both are handled here so a dependency
    bump does not silently produce zero evidence rows.
    """
    boxes = getattr(result, "boxes", None)
    if boxes is not None:  # 3.x: parallel arrays on a result object
        texts = getattr(result, "txts", ()) or ()
        scores = getattr(result, "scores", ()) or ()
        for box, text, score in zip(boxes, texts, scores, strict=False):
            yield [list(point) for point in box], str(text), float(score)
        return

    for item in result or ():  # 1.x: [(box, text, score), ...]
        if len(item) >= 3:
            box, text, score = item[0], item[1], item[2]
            yield [list(point) for point in box], str(text), float(score)
