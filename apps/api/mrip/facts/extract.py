"""Table → facts.

The semantic step, and the one with the most ways to be wrong. Its governing
rule is the project's: **it refuses rather than guesses, and it says what it
refused.**

A table of figures is three questions per cell — *which entity, which metric,
which period* — and this module answers them only from things that are written
down:

- the **row label** and **column header**, resolved through the closed entity
  and metric vocabularies and the period normalizer;
- the **unit**, taken from a parenthetical on the header, on the row label, or
  from a caption line on the same page ("(Figures in Million Tonnes)");
- the **document's own** publisher and fiscal year, used only to fill a
  dimension the table itself leaves implicit.

If a cell's three questions are not all answered, no fact is produced and the
reason is counted. A run over a 200-page annual report typically refuses more
cells than it accepts — page numbers, totals rows, ratios with no unit — and the
report of *what* was skipped is part of the output rather than a silence.

Two refusals are worth naming because a lesser extractor would not make them:

**A unit is never assumed.** "Production" in a table with no unit anywhere is not
assumed to be in tonnes, or million tonnes, or lakh tonnes. The Indian sector
uses all three in the same document, and `MT` alone is genuinely ambiguous
(million tonnes vs metric tonne) — the unit normalizer already refuses to resolve
it without a convention, and this module does not override that.

**The dimension has to match the metric.** A "Production" column that parses as a
percentage is refused rather than stored as 0.45 tonnes, because a misread column
header is far more likely than a production figure of half a tonne.

Some tables cannot be read this way at all, because their meaning lives in the
*pairing* of columns beneath a five-row header rather than in any single header
cell. Those are handed to a **layout reader** — see
:mod:`mrip.facts.performance_statement` — which is tried first and, when it
recognises the form, takes the whole table. Everything the two routes share, and
the vocabulary they both refuse in, is in :mod:`mrip.facts.cells`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from mrip import log
from mrip.facts.cells import (
    ACTIONABLE_SKIPS,
    NULL_TOKENS,
    TOTAL_LABELS,
    Cell,
    ExtractionReport,
    SkipReason,
    build_fact,
    parse_number,
    to_cells,
    try_period,
    unit_from_text,
)
from mrip.facts.performance_statement import LAYOUT_NAME, read_performance_statement
from mrip.normalize.entities import resolve_entity
from mrip.normalize.metrics import Metric, resolve_metric
from mrip.normalize.periods import Period
from mrip.normalize.units import (
    Dimension,
    MTConvention,
    UnknownUnitError,
    normalize_quantity,
)
from mrip.schemas import Document

__all__ = [
    "ACTIONABLE_SKIPS",
    "ExtractionReport",
    "SkipReason",
    "extract_facts",
    "parse_number",
]

logger = log.get_logger("mrip.extract")

_CAPTION_UNIT = re.compile(
    r"\b(?:figures?|amount|quantity|values?|all\s+figures?)?\s*"
    r"(?:in|are\s+in)\s+([A-Za-z.\s/']{2,30})\b",
    re.IGNORECASE,
)


def _metric_from_caption(lines: Iterable[str]) -> Metric | None:
    """The metric named by a caption near the table.

    The common CIL shape is a table whose rows are subsidiaries and whose columns
    are fiscal years — the *metric* appears in neither, only in the heading above
    ("Coal production by subsidiary"). Without this, such a table produces no
    facts at all.

    If the page names **more than one** metric, this returns ``None`` rather than
    picking one. Two tables on a page, one of production and one of offtake, is
    exactly the case where a guess would attach every figure to the wrong series.
    """
    found: list[Metric] = []
    for line in lines:
        metric = resolve_metric(line)
        if metric is not None and metric not in found:
            found.append(metric)
    return found[0] if len(found) == 1 else None


def _unit_from_caption(lines: Iterable[str]) -> str | None:
    """A unit named by a caption near the table: "(Figures in lakh tonnes)".

    Searched only in lines on the same page, and only with an explicit "in"
    construction. A bare "Tonnes" heading elsewhere on the page is not taken as
    the table's unit — that is the kind of inference this module exists to avoid.
    """
    for line in lines:
        match = _CAPTION_UNIT.search(line)
        if match:
            unit = match.group(1).strip(" .")
            if unit and len(unit.split()) <= 4:
                return unit
    return None


def _header_row(grid: dict[tuple[int, int], Cell], rows: Sequence[int]) -> int:
    """The first row whose cells are mostly *not* numbers.

    Not "row 0": a table often carries a title row, a unit row, or a blank line
    above the real header, and a spreadsheet's first row may be a sheet caption.
    """
    for row in rows:
        values = [cell.text for (r, _), cell in grid.items() if r == row and cell.text]
        if len(values) < 2:
            continue
        numeric = sum(1 for value in values if parse_number(value) is not None)
        if numeric <= len(values) // 2:
            return row
    return rows[0] if rows else 0


def _label_column(
    grid: dict[tuple[int, int], Cell], header_row: int, columns: Sequence[int]
) -> int:
    """The column carrying row labels: the leftmost mostly-non-numeric column."""
    for col in columns:
        values = [
            cell.text
            for (r, c), cell in grid.items()
            if c == col and r > header_row and cell.text
        ]
        if not values:
            continue
        numeric = sum(1 for value in values if parse_number(value) is not None)
        if numeric <= len(values) // 2:
            return col
    return columns[0] if columns else 0


def _row_captions(
    grid: dict[tuple[int, int], Cell], rows: Sequence[int], header_row: int
) -> list[str]:
    """Text from the rows above the header that carry no figures.

    A row above the header with no numbers in it is a heading, not data — "Coal
    offtake by subsidiary", or "(Figures in Million Tonnes)".

    This is how a **spreadsheet** gets a caption at all: a workbook has no text
    lines, only cells, so the page-level caption scan finds nothing and every
    figure in it was refused for `no_metric` until this existed. It helps PDFs
    too, where a caption is sometimes swallowed into the table's own bounds.

    The row's cells are joined rather than taken one at a time, because a caption
    that spans the page is routinely cut into pieces by the column guides
    underneath it: ``COAL`` | ``PRODUCTIO`` | ``N (Figs in Mill Te)`` is one
    heading in three cells, and no piece of it resolves to a metric alone. Reading
    only single-cell rows — which is what this did — meant that on a real CIL
    statement the caption was never seen at all.
    """
    captions: list[str] = []
    for row in rows:
        if row >= header_row:
            break
        texts = [
            cell.text for (r, _), cell in sorted(grid.items()) if r == row and cell.text
        ]
        if not texts or any(parse_number(text) is not None for text in texts):
            continue
        captions.append(" ".join(texts))
    return captions


def extract_facts(
    document: Document,
    evidence_rows: Sequence[dict[str, Any]],
    *,
    page_lines: dict[int | None, list[str]] | None = None,
    mt_convention: MTConvention = MTConvention.MILLION_TONNES,
) -> ExtractionReport:
    """Read every table in a document's evidence and produce facts.

    ``page_lines`` is the digitized text of each page, used only to find a unit
    caption. Passing it is optional; without it, tables whose unit lives in a
    caption rather than in a header are refused with ``no_unit`` — which is the
    correct outcome, not a degradation.
    """
    report = ExtractionReport()
    cells = to_cells(evidence_rows)
    if not cells:
        return report

    by_table: dict[str, list[Cell]] = defaultdict(list)
    for cell in cells:
        by_table[cell.table_id or "t?"].append(cell)

    for table_id, table_cells in sorted(by_table.items()):
        report.tables_seen += 1
        _extract_table(
            document,
            table_id,
            table_cells,
            report,
            page_lines=page_lines or {},
            mt_convention=mt_convention,
        )

    logger.info(
        "extraction complete",
        document_id=document.document_id,
        facts=len(report.facts),
        tables=report.tables_seen,
        layouts=dict(report.layouts_read),
        skipped=dict(report.skipped),
    )
    return report


def _extract_table(
    document: Document,
    table_id: str,
    cells: list[Cell],
    report: ExtractionReport,
    *,
    page_lines: dict[int | None, list[str]],
    mt_convention: MTConvention,
) -> None:
    grid = {(cell.row, cell.col): cell for cell in cells}
    rows = sorted({cell.row for cell in cells})
    columns = sorted({cell.col for cell in cells})
    if len(rows) < 2 or len(columns) < 2:
        return  # a single row or column is not a table of figures

    # A recognised layout takes the whole table, including when it then refuses
    # it. Falling through to the generic path after a layout reader has declined
    # would read the same cells with none of the cross-checks that made it
    # decline — the worst of both readings.
    reading = read_performance_statement(
        document, table_id, cells, mt_convention=mt_convention
    )
    if reading is not None:
        report.layout(LAYOUT_NAME)
        report.facts.extend(reading.facts)
        for reason, sample in reading.skips:
            report.skip(reason, sample)
        return

    header_row = _header_row(grid, rows)
    label_col = _label_column(grid, header_row, columns)
    # `None` is a legitimate page for a document that has none (a .docx), and it
    # is a key in `page_lines` for the same reason — so this is a plain lookup
    # rather than a `page or -1` that would silently miss those captions.
    page = next((cell.page for cell in cells if cell.page is not None), None)
    captions = [*page_lines.get(page, []), *_row_captions(grid, rows, header_row)]
    caption_unit = _unit_from_caption(captions)
    caption_metric = _metric_from_caption(captions)

    # What each column header means, decided once per table rather than per cell.
    column_period: dict[int, Period] = {}
    column_metric: dict[int, Metric] = {}
    column_unit: dict[int, str] = {}
    for col in columns:
        header = grid.get((header_row, col))
        if header is None or not header.text:
            continue
        period = try_period(header.text)
        if period is not None:
            column_period[col] = period
        metric = resolve_metric(header.text)
        if metric is not None:
            column_metric[col] = metric
        unit = unit_from_text(header.text)
        if unit:
            column_unit[col] = unit

    for row in rows:
        if row <= header_row:
            continue
        label_cell = grid.get((row, label_col))
        label = label_cell.text if label_cell else ""
        if not label:
            continue
        if label.strip().lower() in TOTAL_LABELS:
            report.skip(SkipReason.TOTAL_ROW, label)
            continue

        row_metric = resolve_metric(label)
        row_entity = resolve_entity(label)
        row_unit = unit_from_text(label)
        row_period = try_period(label)

        for col in columns:
            if col == label_col:
                continue
            cell = grid.get((row, col))
            if cell is None or not cell.text:
                continue

            value = parse_number(cell.text)
            if value is None:
                report.skip(
                    SkipReason.NULL_CELL
                    if cell.text.strip().lower() in NULL_TOKENS
                    else SkipReason.NOT_A_NUMBER,
                    cell.text,
                )
                continue

            if cell.text_layer_recognised:
                # This reader has no redundancy to check the figure against, and
                # an OCR layer's failure mode is quiet: ``61 .2`` parses cleanly
                # as 61 and loses two hundred thousand tonnes without a word.
                # Refused here and counted; the layout readers, which verify each
                # figure against the row's own printed growth percentage, take
                # these same cells and do produce facts from them.
                report.skip(SkipReason.UNVERIFIABLE_OCR_LAYER, cell.text)
                continue

            metric = row_metric or column_metric.get(col) or caption_metric
            if metric is None:
                report.skip(SkipReason.NO_METRIC, f"{label} | {cell.text}")
                continue

            period = column_period.get(col) or row_period
            if period is None and document.fiscal_year:
                # The document's own fiscal year, used only when the table is
                # silent. A table that names its periods always wins.
                period = try_period(document.fiscal_year)
            if period is None:
                report.skip(SkipReason.NO_PERIOD, f"{label} | column {col}")
                continue

            entity_id = None
            entity_review = False
            if row_entity is not None:
                entity_id = row_entity.entity.entity_id
                entity_review = row_entity.needs_review
            elif document.publisher_entity_id:
                entity_id = document.publisher_entity_id
            if entity_id is None:
                report.skip(SkipReason.NO_ENTITY, label)
                continue

            raw_unit = column_unit.get(col) or row_unit or caption_unit
            if not raw_unit:
                report.skip(SkipReason.NO_UNIT, f"{label} | {cell.text}")
                continue

            try:
                quantity = normalize_quantity(
                    value, raw_unit, mt_convention=mt_convention
                )
            except UnknownUnitError:
                report.skip(SkipReason.AMBIGUOUS_UNIT, f"{raw_unit} | {cell.text}")
                continue

            if quantity.dimension is not metric.dimension:
                # The check that catches a misread header: a "Production" column
                # whose unit is a percentage is not a production figure.
                report.skip(
                    SkipReason.WRONG_DIMENSION,
                    f"{metric.key} expects {metric.dimension.value}, "
                    f"{raw_unit!r} is {quantity.dimension.value}",
                )
                continue

            report.facts.append(
                build_fact(
                    document=document,
                    cell=cell,
                    table_id=table_id,
                    entity_id=entity_id,
                    metric=metric,
                    period=period,
                    quantity=quantity,
                    label=label,
                    needs_review=entity_review or quantity.ambiguous,
                )
            )


def dimension_of(metric_key: str) -> Dimension | None:
    """The dimension a metric must have. Used by the validators."""
    metric = resolve_metric(metric_key)
    return metric.dimension if metric else None
