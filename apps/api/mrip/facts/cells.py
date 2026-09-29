"""The grid, and the vocabulary for refusing parts of it.

Everything two or more table readers need in common lives here: the cell record,
the number parser, the list of reasons a cell can be declined, and the assembly of
a :class:`~mrip.schemas.Fact` from a cell plus its three resolved dimensions.

It exists because there is more than one way to read a table. The generic reader
in :mod:`mrip.facts.extract` works out a header row and a label column and asks
three questions of every cell. A **layout reader** — see
:mod:`mrip.facts.performance_statement` — instead recognises one publisher's
recurring form and reads it knowingly, which is the only way to get figures out of
a table whose header spans five rows and whose meaning lives in the *pairing* of
columns rather than in any one of them.

Both produce the same `Fact` with the same evidence and the same decomposed
confidence, and both refuse in the same vocabulary, so a document read by either
route reports its refusals in one table of counts.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mrip.db.repositories.documents import new_id
from mrip.normalize.metrics import Metric
from mrip.normalize.periods import Period, UnknownPeriodError, normalize_period
from mrip.schemas import (
    BBox,
    Confidence,
    Document,
    EvidenceRef,
    ExtractionMethod,
    Fact,
    FactStatus,
)

__all__ = [
    "ACTIONABLE_SKIPS",
    "NULL_TOKENS",
    "PARSE_CONFIDENCE",
    "TOTAL_LABELS",
    "Cell",
    "ExtractionReport",
    "SkipReason",
    "build_fact",
    "parse_number",
    "to_cells",
    "try_period",
    "unit_from_text",
]

#: Structural confidence by how the cell's table was found. A lattice table has
#: drawn borders, so its rows and columns are facts about the document; a stream
#: table's structure is inferred from whitespace alignment, and sometimes wrong.
#: These are the `parse` stage of a fact's confidence — never blended with the
#: others.
PARSE_CONFIDENCE: dict[ExtractionMethod, float] = {
    ExtractionMethod.TABLE_LATTICE: 0.95,
    ExtractionMethod.TABLE_STREAM: 0.78,
    ExtractionMethod.SPREADSHEET_CELL: 0.99,
    ExtractionMethod.PDF_TEXT_LAYER: 0.70,
    ExtractionMethod.OCR: 0.60,
}

#: Text that means "no value here". Kept explicit so a dash is skipped rather
#: than parsed as zero — a mine that reported nothing is not a mine that
#: produced nothing.
NULL_TOKENS = frozenset(
    {"", "-", "–", "—", "na", "n.a.", "n/a", "nil", "not available", "*", "--", "…"}
)

#: Row labels that mean "the sum of the rows above". Excluded because storing a
#: total alongside its parts double-counts every aggregate the platform computes.
TOTAL_LABELS = frozenset(
    {
        "total",
        "grand total",
        "sub total",
        "sub-total",
        "subtotal",
        "all india",
        "cil total",
        "total cil",
        "overall",
    }
)

_PARENTHETICAL = re.compile(r"\(([^)]*)\)")
_FOOTNOTE = re.compile(r"[*†‡#]+\s*$")
_NUMBER = re.compile(r"^[-+]?[\d,\s]*\.?\d+$")


class SkipReason:
    """Why a cell produced no fact. Counted and reported, never silent."""

    NOT_A_NUMBER = "not_a_number"
    NULL_CELL = "null_cell"
    TOTAL_ROW = "total_row"
    NO_METRIC = "no_metric"
    NO_PERIOD = "no_period"
    NO_ENTITY = "no_entity"
    NO_UNIT = "no_unit"
    AMBIGUOUS_UNIT = "ambiguous_unit"
    WRONG_DIMENSION = "wrong_dimension"
    HEADER_CELL = "header_cell"
    #: The row's own printed growth percentage disagrees with the two figures it
    #: is computed from. Raised by the layout readers, which have that redundancy
    #: available; see :mod:`mrip.facts.performance_statement`.
    FAILS_SELF_CHECK = "fails_self_check"
    #: A recognised layout whose numbers did not line up with its own printed
    #: cross-checks often enough to trust the column assignment at all.
    LAYOUT_UNVERIFIED = "layout_unverified"
    #: The cell's text came off an OCR text layer and this reader has no way to
    #: check it. Raised by the generic reader only: a layout reader that verifies
    #: each figure against the page's own printed growth column *can* use these
    #: cells, and does. Counted rather than dropped, because "this document's
    #: table was found but its glyphs are unverifiable" is a different and more
    #: recoverable problem than "no table was found".
    UNVERIFIABLE_OCR_LAYER = "unverifiable_ocr_layer"


#: Reasons a reviewer can act on, shown on the document page. The rest are
#: ordinary table furniture and are counted but not surfaced.
ACTIONABLE_SKIPS = frozenset(
    {
        SkipReason.NO_UNIT,
        SkipReason.AMBIGUOUS_UNIT,
        SkipReason.WRONG_DIMENSION,
        SkipReason.FAILS_SELF_CHECK,
        SkipReason.LAYOUT_UNVERIFIED,
        SkipReason.UNVERIFIABLE_OCR_LAYER,
    }
)


@dataclass
class ExtractionReport:
    """What one document's extraction produced, and what it declined to."""

    facts: list[Fact] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: A few verbatim examples per reason, so "37 cells had no unit" can be
    #: followed by *which* cells.
    examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    tables_seen: int = 0
    #: Tables a layout reader recognised and read, by layout name. Reported so a
    #: run can say "read 214 CIL performance statements" rather than only
    #: totalling facts.
    layouts_read: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def skip(self, reason: str, sample: str | None = None) -> None:
        self.skipped[reason] += 1
        if sample and len(self.examples[reason]) < 5:
            self.examples[reason].append(sample[:120])

    def layout(self, name: str) -> None:
        self.layouts_read[name] += 1

    @property
    def actionable_skips(self) -> int:
        return sum(self.skipped.get(reason, 0) for reason in ACTIONABLE_SKIPS)

    def summary(self) -> dict[str, Any]:
        return {
            "facts": len(self.facts),
            "tables": self.tables_seen,
            "skipped": dict(self.skipped),
            "layouts": dict(self.layouts_read),
            "examples": {reason: list(items) for reason, items in self.examples.items()},
        }


def parse_number(text: str) -> float | None:
    """Parse a table cell into a number, or ``None`` if it is not one.

    Handles what Indian government tables actually contain: lakh/crore digit
    grouping (``1,23,456.78``), accounting negatives (``(1,234)``), footnote
    markers, and the several ways a table says "nothing here".

    Returns ``None`` rather than ``0.0`` for a blank or a dash. A mine that
    reported nothing did not produce nothing, and a zero here would be a figure
    this platform invented.
    """
    cleaned = _FOOTNOTE.sub("", text.strip()).strip()
    if cleaned.lower() in NULL_TOKENS:
        return None

    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1].strip()

    # Trailing unit symbols are handled by the caller; strip only a percent sign
    # here because it changes the *number's* meaning rather than its unit.
    percent = cleaned.endswith("%")
    if percent:
        cleaned = cleaned[:-1].strip()

    candidate = cleaned.replace(",", "").replace(" ", "").replace(" ", "")
    if not candidate or not _NUMBER.match(cleaned.replace(" ", " ")):
        return None

    try:
        value = float(candidate)
    except ValueError:
        return None
    return -value if negative else value


def unit_from_text(text: str) -> str | None:
    """A unit named in a parenthetical: "Production (MT)" → "MT"."""
    for candidate in _PARENTHETICAL.findall(text):
        stripped = str(candidate).strip()
        if not stripped or parse_number(stripped) is not None:
            continue
        return stripped
    return None


def try_period(text: str) -> Period | None:
    """``normalize_period`` with its refusals turned into ``None``."""
    try:
        return normalize_period(text)
    except (UnknownPeriodError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Cell:
    """One digitized cell, with everything an evidence receipt needs."""

    row: int
    col: int
    text: str
    page: int | None
    table_id: str | None
    cell_ref: str | None
    method: ExtractionMethod
    ocr_confidence: float | None
    bbox: BBox | None
    #: Whether this cell's text came off a PDF text layer that is itself OCR
    #: output. The grid it sits in is the publisher's and is sound; the glyphs
    #: may read ``635`` where the paper says ``63.5``. A reader that can check a
    #: figure against something else on the page may use such a cell; a reader
    #: that cannot must refuse it rather than pass an unverifiable number off as
    #: the document's own.
    text_layer_recognised: bool = False


def to_cells(rows: Sequence[dict[str, Any]]) -> list[Cell]:
    """Evidence rows from the digitizer into positioned cells."""
    cells: list[Cell] = []
    for row in rows:
        if row.get("row_idx") is None or row.get("col_idx") is None:
            continue
        box = None
        if row.get("bbox_x0") is not None and row.get("bbox_y1") is not None:
            box = BBox(
                x0=row["bbox_x0"],
                y0=row["bbox_y0"],
                x1=row["bbox_x1"],
                y1=row["bbox_y1"],
            )
        cells.append(
            Cell(
                row=row["row_idx"],
                col=row["col_idx"],
                text=(row.get("text") or "").strip(),
                page=row.get("page"),
                table_id=row.get("table_id"),
                cell_ref=row.get("cell_ref"),
                method=ExtractionMethod(row["extraction_method"]),
                ocr_confidence=row.get("ocr_confidence"),
                bbox=box,
                text_layer_recognised=bool(row.get("text_layer_recognised")),
            )
        )
    return cells


def build_fact(
    *,
    document: Document,
    cell: Cell,
    table_id: str,
    entity_id: str,
    metric: Metric,
    period: Period,
    quantity: Any,
    label: str,
    needs_review: bool,
    snippet: str | None = None,
    notes: str | None = None,
    recognition_confidence: float | None = None,
) -> Fact:
    """Assemble the fact, with its evidence and its decomposed confidence.

    The ``answer`` stage is left ``None`` on purpose. No model was involved in
    producing this figure — it came from a cell in a table — and a number in that
    slot would imply one was. ``LEAST`` over the stages ignores nulls, so the
    limiting confidence is the parse (and OCR, where it ran), which is exactly
    the honest reading.

    ``recognition_confidence`` exists because "where it ran" has two cases and
    only one of them is ours. A cell off a publisher-supplied OCR text layer was
    recognised by *someone*, who left no score behind, so a null ``ocr`` stage
    there would assert typeset text — the one thing that page is not. A reader
    that checked the figure against an independent number on the same page has
    thereby measured that recogniser's fidelity and passes the rate it measured.
    Readers with no such check refuse those cells outright
    (:attr:`SkipReason.UNVERIFIABLE_OCR_LAYER`) rather than emit a figure whose
    recognition stage no one can fill in.

    ``snippet`` overrides the default receipt for readers that can say more than
    "label | value" — a layout reader knows the column was "actual same period
    last year" and that belongs in the receipt, because the same row holds four
    figures and "ECL | 2.4" alone does not say which.
    """
    return Fact(
        fact_id=new_id("fact"),
        entity_id=entity_id,
        metric=metric.key,
        value=quantity.value,
        unit=quantity.unit,
        dimension=quantity.dimension.value,
        raw_value=quantity.raw_value,
        raw_unit=quantity.raw_unit,
        period_start=period.start,
        period_end=period.end,
        period_label=period.label,
        fiscal_year=period.fiscal_year,
        evidence=EvidenceRef(
            document_id=document.document_id,
            document_version=document.version,
            page=cell.page,
            table_id=table_id,
            cell_ref=cell.cell_ref,
            bbox=cell.bbox,
            # The row label travels with the value, because "193.00" on its own
            # is not a receipt — "SECL | 193.00" is.
            snippet=(snippet or f"{label} | {cell.text}")[:1000],
        ),
        extraction_method=cell.method,
        confidence=Confidence(
            # Two different nulls used to collapse here. ``ocr=None`` means the
            # recognition stage did not apply, which is true of typeset text and
            # false of a page whose text layer someone else recognised — and it
            # is the latter that a reviewer most needs to see flagged.
            ocr=(
                recognition_confidence
                if cell.text_layer_recognised
                else cell.ocr_confidence
            ),
            parse=PARSE_CONFIDENCE.get(cell.method, 0.5),
            answer=None,
        ),
        # A fuzzy entity match or an ambiguous unit means a person should look
        # before this figure is trusted. Routing it here, at creation, is more
        # honest than storing it as `extracted` and hoping a later sweep catches
        # it — the doubt is known *now*.
        status=FactStatus.NEEDS_REVIEW if needs_review else FactStatus.EXTRACTED,
        unit_ambiguous=quantity.ambiguous,
        notes=notes or quantity.note,
        extracted_at=datetime.now(UTC),
    )
