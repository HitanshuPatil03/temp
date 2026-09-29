"""The CIL monthly performance statement, read as the form it is.

Coal India publishes, every month, one page titled *"PRODUCTION AND OFFTAKE
PERFORMANCE OF CIL AND SUBSIDIARY COMPANIES"*. It is the single most-cited
document in the sector, there are hundreds of them in the corpus, and the generic
table reader gets **nothing** out of it. That is not a bug in the generic reader;
it is what the generic reader's honesty costs. The page looks like this:

                        COAL PRODUCTION (Figs in Mill Te)
              ┌───────── AUG'23 ─────────┬──── APR'23 - AUG'23 ────┐
              │ ACTUAL   ACTUAL     %    │ ACTUAL  ACTUAL     %    │
              │  THIS    SAME PERIOD     │  THIS   SAME PERIOD     │
              │  YEAR    LAST YEAR  GROWTH│ YEAR   LAST YEAR  GROWTH│
      ECL     │   2.7      2.4      12.2 │  15.6    13.4     16.6  │

No column header names a metric, a period or an entity on its own. "ACTUAL THIS
YEAR" is not a period; ``AUG'23`` sits two rows above it spanning three columns;
the metric is in a caption; the unit is in that caption's parenthetical; and the
same page carries a second, identical block for offtake. A reader that asks "what
does this column mean?" is asking the wrong question — the meaning is in the
*shape*, which repeats month after month and is worth recognising once.

So this module recognises it, and then does something the generic reader cannot:
**it checks its reading against the document's own arithmetic.** Every row prints
a growth percentage computed from the two figures beside it. If our column
assignment is right, ``(this - last) / last`` must reproduce that percentage
within the rounding the document itself applied. That turns "we think column 1 is
this year" from an assumption into a testable claim, evaluated per row, on every
document, at ingest time.

It earns its keep twice over, because these statements are often *scans*. A
scanned page whose OCR was written back into the PDF reads as a text layer but
says ``IUCL`` for ``MCL`` and — the dangerous one — ``635`` for ``63.5``. A
dropped decimal point still parses as a number, so no amount of care about
*parsing* catches it. The growth column does: ``2.3`` against ``31`` implies
-92.6% where the document prints -25.5%, so the row is refused. When a third of a
block's verifiable rows disagree, the whole block is refused rather than read
row-by-row, because that corruption can also produce a pair that happens to be
self-consistent.

Two deliberate omissions:

**CIL's own line is dropped as a total.** It is the sum of the subsidiary rows
above it, and storing a total beside its parts double-counts every aggregate the
platform computes. The CIL figure is recovered by summing subsidiaries, which is
also how it becomes traceable to nine cells instead of one.

**Growth percentages are not stored.** They are derived from two figures this
module already stores, and a derived number with no cell of its own is not
evidence. They are used for verification and then discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from mrip import log
from mrip.facts.cells import (
    TOTAL_LABELS,
    Cell,
    SkipReason,
    build_fact,
    parse_number,
    try_period,
    unit_from_text,
)
from mrip.normalize.entities import resolve_entity
from mrip.normalize.metrics import Metric, resolve_metric
from mrip.normalize.periods import FISCAL_START_MONTH, Period, PeriodKind
from mrip.normalize.units import (
    MTConvention,
    Quantity,
    UnknownUnitError,
    normalize_unit,
)
from mrip.schemas import Document, Fact

__all__ = ["LAYOUT_NAME", "StatementReading", "read_performance_statement"]

logger = log.get_logger("mrip.facts.performance_statement")

#: Reported in the extraction summary so a run can say how many of these it read.
LAYOUT_NAME = "cil_performance_statement"

#: The six figures each subsidiary row carries, in the order they are printed.
#: The pairing is the whole point: columns 0-2 describe the month, 3-5 the year to
#: date, and reading one as the other overstates August production fivefold.
_MONTH_THIS, _MONTH_LAST, _MONTH_GROWTH = 0, 1, 2
_CUM_THIS, _CUM_LAST, _CUM_GROWTH = 3, 4, 5
_FIGURES_PER_ROW = 6

#: Metric words that head a block, mapped to the lexicon term to resolve. A block
#: caption naming *two* of these is the page title, not a block caption.
_BLOCK_METRICS: dict[str, str] = {
    "production": "coal production",
    "offtake": "coal offtake",
    "despatch": "coal offtake",
    "dispatch": "coal offtake",
}

#: ``APR'23 - AUG'23`` in the several ways these pages print it, including the OCR
#: variants where the dash came out as a full stop. Deliberately looser than the
#: normalizer's own pattern: this only *locates* the range and splits it at the
#: separator, and :func:`~mrip.normalize.periods.normalize_period` is what decides
#: whether either half names a month.
#:
#: The two halves are captured because they have to be resolved *separately*. Given
#: the whole match, the normalizer reads ``APR' 26 - IVIAY'26`` — a scan that ate
#: the M of MAY — as plain "Apr 2026", since a range with one illegible endpoint
#: falls through to its single-month shape. That turns a two-month cumulative
#: column into a one-month column, which is the error
#: :class:`~mrip.normalize.periods.PeriodKind.MONTH_RANGE` exists to prevent.
_RANGE = re.compile(
    r"([a-z]{3,9}\s*['’./ -]?\s*\d{2,4})\s*(?:-|to|\.|–)\s*"
    r"([a-z]{3,9}\s*['’./ -]?\s*\d{2,4})",
    re.IGNORECASE,
)

#: What OCR makes of a capital M in this font. Applied only as a *fallback*, after
#: the unrepaired text has already failed to parse, so a repair can never change a
#: reading that already worked.
_M_CONFUSIONS = ("IVI", "lVl", "lvl", "IVl", "iVi", "I\\/I", "l\\/l", "\\/I")

#: "Mill Te", "MillTe", "Miil Te", "lVlill Te" — one million tonnes, four ways.
#: The token is canonicalised to "million" rather than added to the unit lexicon
#: because "miil" is not a word anyone means; it is this scanner's rendering of
#: one, and the unit vocabulary should not grow a row per scanner.
_MILLION_GLUED = re.compile(r"\bm[il1|]{2,4}(te|tonnes?)\b", re.IGNORECASE)
_MILLION_SPACED = re.compile(r"\bm[il1|]{2,4}\b(\s+)(te|tonnes?|t)\b", re.IGNORECASE)

#: Below this many percentage points of rounding slack the printed growth figure
#: is worth checking against; above it, two figures rounded to one decimal cannot
#: pin the percentage down well enough for disagreement to mean anything.
_INFORMATIVE_TOLERANCE = 12.0
#: Rows that must be checkable before a block is trusted at all.
_MIN_INFORMATIVE_ROWS = 3
#: Share of checkable rows that must agree with their own printed growth figure.
_MIN_AGREEMENT = 0.8


@dataclass
class StatementReading:
    """What this reader made of one table.

    Returned — rather than ``None`` — as soon as the layout is *recognised*, even
    when it yields no facts. A recognised table that failed its self-check must
    not then be handed to the generic reader, which would read the same cells with
    none of the checks and no idea that the growth columns exist.
    """

    facts: list[Fact] = field(default_factory=list)
    #: ``(reason, sample)`` pairs for the caller to fold into its own report.
    skips: list[tuple[str, str]] = field(default_factory=list)
    blocks: int = 0
    verified_blocks: int = 0

    def skip(self, reason: str, sample: str) -> None:
        self.skips.append((reason, sample))


@dataclass(frozen=True, slots=True)
class _Block:
    """One metric's half of the page: a caption, a period row, and the rows below."""

    caption: str
    metric: Metric
    unit_text: str
    unit_note: str | None
    factor: float
    month: Period
    cumulative: Period
    first_row: int
    last_row: int


@dataclass(frozen=True, slots=True)
class _DataRow:
    label: str
    entity_id: str
    needs_review: bool
    cells: tuple[Cell, ...]
    values: tuple[float, ...]


def read_performance_statement(
    document: Document,
    table_id: str,
    cells: list[Cell],
    *,
    mt_convention: MTConvention = MTConvention.MILLION_TONNES,
) -> StatementReading | None:
    """Read a CIL performance statement, or return ``None`` if this is not one.

    Args:
        document: The document the cells came from, for provenance.
        table_id: The digitizer's id for this table, carried into the evidence.
        cells: Every cell of one table.
        mt_convention: Passed through for consistency; this layout always states
            its unit in the caption, so it is not consulted.

    Returns:
        The reading, or ``None`` when no block caption was recognised — which is
        the signal to the caller that the generic reader should handle the table.
    """
    grid = {(cell.row, cell.col): cell for cell in cells if cell.text}
    if not grid:
        return None
    rows = sorted({row for row, _ in grid})

    captions = [row for row in rows if _caption_metric(_squash(_joined(grid, row)))]
    if not captions:
        return None

    reading = StatementReading()
    for position, caption_row in enumerate(captions):
        end = captions[position + 1] - 1 if position + 1 < len(captions) else rows[-1]
        reading.blocks += 1
        block = _read_block(grid, rows, caption_row, end, reading)
        if block is not None:
            _emit_block(document, table_id, grid, rows, block, reading)

    logger.info(
        "performance statement read",
        document_id=document.document_id,
        table_id=table_id,
        blocks=reading.blocks,
        verified=reading.verified_blocks,
        facts=len(reading.facts),
    )
    return reading


# --- recognising the block ----------------------------------------------------


def _joined(grid: dict[tuple[int, int], Cell], row: int) -> str:
    """One row's cells, left to right, separated by a space.

    Words in these tables are routinely split across columns — ``COAL`` |
    ``PRODUCTIO`` | ``N (Figs in Miil Te)`` is one caption in three cells — so
    both this and :func:`_squash` are needed: the spaced form keeps ``Figs in``
    readable for the unit, the squashed form makes ``PRODUCTIO N`` findable.
    """
    return " ".join(
        cell.text.replace("\n", " ")
        for (r, _), cell in sorted(grid.items())
        if r == row and cell.text
    )


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _caption_metric(squashed: str) -> str | None:
    """The single metric word a caption names, if it names exactly one.

    The page title names both production and offtake, and treating it as a block
    caption would attach every figure on the page to one of them.
    """
    if "(" not in squashed:
        return None
    found = {term for word, term in _BLOCK_METRICS.items() if word in squashed}
    return next(iter(found)) if len(found) == 1 else None


def _read_block(
    grid: dict[tuple[int, int], Cell],
    rows: list[int],
    caption_row: int,
    end_row: int,
    reading: StatementReading,
) -> _Block | None:
    """Resolve one block's metric, unit and two periods, or decline it."""
    caption = _joined(grid, caption_row)
    term = _caption_metric(_squash(caption))
    metric = resolve_metric(term) if term else None
    if metric is None:
        reading.skip(SkipReason.NO_METRIC, caption)
        return None

    unit = _block_unit(caption)
    if unit is None:
        reading.skip(SkipReason.NO_UNIT, caption)
        return None
    unit_text, factor, unit_note = unit

    inner = [row for row in rows if caption_row < row <= end_row]
    periods = _block_periods(grid, inner)
    if periods is None:
        reading.skip(SkipReason.NO_PERIOD, caption)
        return None
    period_row, month, cumulative = periods

    first_data = next(
        (row for row in inner if row > period_row and _data_row(grid, row) is not None),
        None,
    )
    if first_data is None:
        reading.skip(SkipReason.LAYOUT_UNVERIFIED, f"{caption}: no subsidiary rows")
        return None

    header = _squash(
        " ".join(_joined(grid, row) for row in inner if period_row < row < first_data)
    )
    if "actualthis" not in header and "grow" not in header:
        # A weak gate on purpose: the load-bearing check is arithmetic, below.
        # This only refuses a table that has the caption and the periods but none
        # of the column headings, which is a different layout wearing this one's
        # words.
        reading.skip(SkipReason.LAYOUT_UNVERIFIED, f"{caption}: no column headings")
        return None

    return _Block(
        caption=caption,
        metric=metric,
        unit_text=unit_text,
        unit_note=unit_note,
        factor=factor,
        month=month,
        cumulative=cumulative,
        first_row=first_data,
        last_row=end_row,
    )


def _repair_ocr_m(text: str) -> str:
    for confusion in _M_CONFUSIONS:
        text = text.replace(confusion, "M")
    return text


def _repair_million(text: str) -> str:
    text = _repair_ocr_m(text)
    text = _MILLION_GLUED.sub(r"million \1", text)
    return _MILLION_SPACED.sub(r"million\1\2", text)


def _block_unit(caption: str) -> tuple[str, float, str | None] | None:
    """``(as printed, factor to tonnes, note)`` from the caption's parenthetical.

    The printed text is kept for the receipt even when a repair was needed to read
    it, because the receipt's job is to quote the document. The repair is recorded
    in the note instead, so a reviewer sees both what the page said and what we
    took it to mean.
    """
    printed = unit_from_text(caption)
    if printed is None:
        # An unclosed parenthesis is common in these scans: "(Figs in Mill Te"
        # loses its bracket. Fall back to the text after "in".
        tail = re.search(r"\bin\s+([A-Za-z.\s/'|1]{2,30})$", caption)
        if tail is None:
            return None
        printed = tail.group(1).strip(" .")

    candidate = re.sub(r"^.*\bin\b\s*", "", printed, flags=re.IGNORECASE).strip(" .")
    candidate = candidate or printed
    for attempt, note in ((candidate, None), (_repair_million(candidate), "repaired")):
        try:
            resolved = normalize_unit(attempt)
        except UnknownUnitError:
            continue
        if note is None:
            return printed, resolved.factor, resolved.note
        return (
            printed,
            resolved.factor,
            f"unit read from {printed!r}; OCR-repaired to {attempt!r} before normalizing",
        )
    return None


def _block_periods(
    grid: dict[tuple[int, int], Cell], inner: list[int]
) -> tuple[int, Period, Period] | None:
    """The block's ``(row, month, year-to-date)`` periods.

    The cumulative range is the anchor rather than the lone month token, because
    the range names its month twice and therefore survives OCR better. The month
    is then the range's *end* — an invariant of this layout, where the right-hand
    block is always April to the reporting month — and the lone token is used to
    confirm that, not to establish it.
    """
    for row in inner:
        text = _joined(grid, row)
        cumulative = _find_cumulative(text)
        if cumulative is None:
            continue
        month = try_period(f"{cumulative.end:%b %Y}")
        if month is None or month.kind is not PeriodKind.MONTH:
            continue
        printed = _lone_month(text)
        if printed is not None and printed.label != month.label:
            # The page's two statements of its own reporting month disagree. One
            # of them is misread, and there is no way to tell which.
            continue
        return row, month, cumulative
    return None


def _find_cumulative(text: str) -> Period | None:
    """The ``APR'23 - AUG'23`` span, which must start in April to be this layout's.

    The two halves are resolved separately and both are required to be legible
    months. Handing the whole match to the period normalizer instead let
    ``APR' 26 - IVIAY'26`` — where the scan ate the M of MAY — fall through to its
    single-month reading and come back as *April 2026*. A two-month cumulative
    column read as one month is precisely the error :class:`PeriodKind.MONTH_RANGE`
    exists to prevent, and it arrived here by the back door.

    So a half we cannot read is a refusal, never a shorter span. The span itself is
    then rebuilt from canonical labels rather than assembled here, to keep the
    fiscal-year and runs-backwards checks in one place.
    """
    for candidate in (text, _repair_ocr_m(text)):
        for match in _RANGE.finditer(candidate):
            first = try_period(match.group(1))
            second = try_period(match.group(2))
            if first is None or second is None:
                continue
            if first.kind is not PeriodKind.MONTH or second.kind is not PeriodKind.MONTH:
                continue
            if first.start.month != FISCAL_START_MONTH:
                continue
            span = try_period(f"{first.start:%b %Y} - {second.start:%b %Y}")
            if span is not None and span.kind in (
                PeriodKind.MONTH_RANGE,
                PeriodKind.MONTH,
            ):
                return span
    return None


def _lone_month(text: str) -> Period | None:
    """The single month token printed over the left-hand block, if it is legible."""
    without_range = _RANGE.sub(" ", text)
    for candidate in (without_range, _repair_ocr_m(without_range)):
        period = try_period(candidate)
        if period is not None and period.kind is PeriodKind.MONTH:
            return period
    return None


# --- reading the rows ---------------------------------------------------------


def _data_row(grid: dict[tuple[int, int], Cell], row: int) -> _DataRow | None:
    """A subsidiary's row, or ``None`` if this row is not one.

    Requires **exactly** six figures and no stray text among them. A row with five
    is not read as five: the six columns are identified by position, so a missing
    one makes every figure after it ambiguous, and "which one is missing?" is a
    guess. Refusing costs the occasional row whose growth cell is blank.
    """
    present = sorted(((c, cell) for (r, c), cell in grid.items() if r == row))
    if len(present) < _FIGURES_PER_ROW + 1:
        return None
    label_cell = present[0][1]
    match = resolve_entity(label_cell.text)
    if match is None:
        return None

    figures = [cell for _, cell in present[1:]]
    if len(figures) != _FIGURES_PER_ROW:
        return None
    values: list[float] = []
    for cell in figures:
        value = parse_number(cell.text)
        if value is None:
            return None
        values.append(value)

    return _DataRow(
        label=label_cell.text,
        entity_id=match.entity.entity_id,
        needs_review=match.needs_review,
        cells=tuple(figures),
        values=tuple(values),
    )


def _growth_agrees(this: float, last: float, printed: float) -> bool | None:
    """Whether the printed growth reproduces from the pair. ``None`` = unknowable.

    The tolerance is derived from the document's own rounding rather than picked:
    two figures printed to one decimal place are each uncertain by 0.05, and the
    percentage they imply is uncertain by the propagated amount. Where that
    uncertainty is larger than the figure being checked — small denominators, a
    subsidiary producing 0.01 — the check cannot distinguish a right reading from
    a wrong one and says so instead of passing by default.
    """
    if last == 0:
        return None
    expected = (this - last) / last * 100.0
    tolerance = 100.0 * (0.05 / abs(last) + 0.05 * abs(this) / (last * last)) + 0.6
    if tolerance > _INFORMATIVE_TOLERANCE:
        return None
    return abs(expected - printed) <= tolerance


def _cumulative_covers_month(values: tuple[float, ...]) -> bool:
    """Year-to-date must be at least the month it ends with.

    A second, independent invariant: cumulative production includes the reporting
    month, so a block whose halves are swapped — or whose decimal points moved —
    usually violates it. Costs nothing on a correct reading.
    """
    slack = 0.051  # both sides rounded to one decimal
    return (
        values[_CUM_THIS] + slack >= values[_MONTH_THIS]
        and values[_CUM_LAST] + slack >= values[_MONTH_LAST]
    )


def _emit_block(
    document: Document,
    table_id: str,
    grid: dict[tuple[int, int], Cell],
    rows: list[int],
    block: _Block,
    reading: StatementReading,
) -> None:
    """Verify the block against its own arithmetic, then emit what survives."""
    data: list[_DataRow] = []
    for row in rows:
        if not (block.first_row <= row <= block.last_row):
            continue
        parsed = _data_row(grid, row)
        if parsed is not None:
            data.append(parsed)

    aggregate = {"cil"}
    subsidiaries = [item for item in data if item.entity_id not in aggregate]
    checks: list[bool] = []
    for item in subsidiaries:
        for this, last, printed in (
            (_MONTH_THIS, _MONTH_LAST, _MONTH_GROWTH),
            (_CUM_THIS, _CUM_LAST, _CUM_GROWTH),
        ):
            verdict = _growth_agrees(
                item.values[this], item.values[last], item.values[printed]
            )
            if verdict is not None:
                checks.append(verdict)

    agreed = sum(1 for check in checks if check)
    if len(checks) < _MIN_INFORMATIVE_ROWS or agreed < _MIN_AGREEMENT * len(checks):
        # Refused as a block, not row by row. The corruption that makes rows
        # disagree — a lost decimal point in a scanned page's text layer — can
        # also produce a pair that agrees by coincidence, so surviving rows here
        # are not evidence of anything.
        detail = (
            f"{block.caption}: {agreed}/{len(checks)} rows reproduce their own "
            f"printed growth figure"
        )
        for item in data:
            reading.skip(SkipReason.LAYOUT_UNVERIFIED, f"{item.label} | {detail}")
        if not data:
            reading.skip(SkipReason.LAYOUT_UNVERIFIED, detail)
        logger.warning(
            "performance statement block refused",
            document_id=document.document_id,
            table_id=table_id,
            caption=block.caption[:80],
            agreed=agreed,
            checked=len(checks),
        )
        return

    reading.verified_blocks += 1
    # The share of this block's figures that reproduced their own printed growth
    # percentage. Where the page's text layer is itself OCR output this is the only
    # measurement of the recogniser's fidelity that exists — the publisher's
    # recogniser left no score behind — and it is a measurement rather than an
    # assumption, taken from redundancy the page prints itself. It reaches the
    # ``ocr`` stage of facts built from such cells and is ignored for typeset ones,
    # where nothing recognised anything. The gate above puts a floor of
    # ``_MIN_AGREEMENT`` under it, and ``len(checks)`` is at least
    # ``_MIN_INFORMATIVE_ROWS``.
    recognition = agreed / len(checks)
    # An April statement's two halves are the same span; emitting both would store
    # every figure twice, from two cells, and invent a conflict out of agreement.
    same_span = block.cumulative.label == block.month.label

    for item in data:
        if item.entity_id in aggregate or item.label.strip().lower() in TOTAL_LABELS:
            reading.skip(SkipReason.TOTAL_ROW, item.label)
            continue

        month_ok = _growth_agrees(
            item.values[_MONTH_THIS], item.values[_MONTH_LAST], item.values[_MONTH_GROWTH]
        )
        cumulative_ok = _growth_agrees(
            item.values[_CUM_THIS], item.values[_CUM_LAST], item.values[_CUM_GROWTH]
        )
        if not _cumulative_covers_month(item.values):
            reading.skip(
                SkipReason.FAILS_SELF_CHECK,
                f"{item.label}: year-to-date is below the month it contains",
            )
            continue

        wanted: list[tuple[int, int, Period, str]] = []
        if month_ok is not False:
            wanted.append((_MONTH_THIS, _MONTH_LAST, block.month, "actual this year"))
        else:
            reading.skip(
                SkipReason.FAILS_SELF_CHECK,
                f"{item.label} {block.month.label}: "
                f"{item.values[_MONTH_THIS]} against {item.values[_MONTH_LAST]} "
                f"is not {item.values[_MONTH_GROWTH]}%",
            )
        if not same_span:
            if cumulative_ok is not False:
                wanted.append(
                    (_CUM_THIS, _CUM_LAST, block.cumulative, "actual this year")
                )
            else:
                reading.skip(
                    SkipReason.FAILS_SELF_CHECK,
                    f"{item.label} {block.cumulative.label}: "
                    f"{item.values[_CUM_THIS]} against {item.values[_CUM_LAST]} "
                    f"is not {item.values[_CUM_GROWTH]}%",
                )

        for this_index, last_index, period, column in wanted:
            previous = _previous_year(period)
            pairs = [(this_index, period, column)]
            if previous is not None:
                pairs.append((last_index, previous, "actual same period last year"))
            else:
                reading.skip(
                    SkipReason.NO_PERIOD,
                    f"{item.label}: no prior-year span for {period.label}",
                )
            for index, resolved, meaning in pairs:
                reading.facts.append(
                    _fact(
                        document=document,
                        table_id=table_id,
                        block=block,
                        item=item,
                        index=index,
                        period=resolved,
                        meaning=meaning,
                        recognition=recognition,
                    )
                )


def _previous_year(period: Period) -> Period | None:
    """The same span a year earlier, resolved through the period normalizer.

    Built from a label rather than by arithmetic on the dates so that the fiscal
    year, the kind and the canonical label all come from one place. The
    prior-year column is the document's, not ours — the page prints it — but the
    *span* it refers to is stated nowhere on the page, only implied by "SAME
    PERIOD LAST YEAR", so it is derived here and derived once.
    """
    start = date(period.start.year - 1, period.start.month, 1)
    end = date(period.end.year - 1, period.end.month, 1)
    label = (
        f"{start:%b %Y}"
        if period.kind is PeriodKind.MONTH
        else f"{start:%b %Y} - {end:%b %Y}"
    )
    resolved = try_period(label)
    return resolved if resolved is not None and resolved.kind is period.kind else None


def _fact(
    *,
    document: Document,
    table_id: str,
    block: _Block,
    item: _DataRow,
    index: int,
    period: Period,
    meaning: str,
    recognition: float,
) -> Fact:
    cell = item.cells[index]
    raw_value = item.values[index]
    quantity = Quantity(
        value=raw_value * block.factor,
        unit="t",
        dimension=block.metric.dimension,
        raw_value=raw_value,
        # What the page printed, not what we rewrote it to. The rewrite, where one
        # was needed, is in the note.
        raw_unit=block.unit_text,
        ambiguous=False,
        note=block.unit_note,
    )
    return build_fact(
        document=document,
        cell=cell,
        table_id=table_id,
        entity_id=item.entity_id,
        metric=block.metric,
        period=period,
        quantity=quantity,
        label=item.label,
        needs_review=item.needs_review,
        snippet=(
            f"{item.label} | {block.caption} | {period.label}, {meaning} | {cell.text}"
        ),
        notes=block.unit_note,
        recognition_confidence=recognition,
    )
