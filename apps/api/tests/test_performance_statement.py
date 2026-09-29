"""The CIL monthly production statement, as the scanner actually hands it over.

Both grids in this file are transcribed from documents Coal India published, read
by ``digitize_pdf_tables`` and copied out cell for cell — including every OCR
wound. They are not illustrations of the layout; they *are* the layout, and the
expected numbers are the ones printed on the paper.

The grids are inlined rather than read from ``data/corpus/`` because the corpus is
24 GB and is not in the repository. Where the files happen to be present the last
class runs against them too, so the transcriptions cannot quietly drift from the
documents they came from.

Two readings are asserted, and the difference between them is the point:

**August 2023** has a clean text layer. Every row reproduces its own printed
``% GROWTH`` from the pair of figures beside it, so the block is trusted and all
64 figures are read — at the scale its caption gives, which the scanner rendered
``Figs in Miil Te``.

**May 2026** is a scan whose OCR was saved back into the PDF. Its production block
prints growth figures that its own numbers do not reproduce — because the decimals
are gone: ``63.5`` came out ``635``, ``13.8`` came out ``138`` — so the block is
refused whole, on the page's own evidence, without anyone having to know the right
answer. The offtake block on the same page is cleaner and is read, minus the four
rows whose decimal points are missing. That asymmetry is the designed behaviour:
refusal is per block and per row, at the finest grain the document can justify.

The May 2026 grid is then read a third time with its cells marked as having come
off that OCR layer, which is what the pipeline really does with the file. The
figures do not change — the self-check never depended on knowing — but each fact
now reports the rate at which the recogniser reproduced the page's own printed
arithmetic, instead of the ``None`` that means "typeset".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mrip.facts.cells import SkipReason
from mrip.facts.extract import extract_facts
from mrip.facts.performance_statement import LAYOUT_NAME, _find_cumulative
from mrip.normalize.periods import PeriodKind
from mrip.schemas import Document, DocumentClass, ExtractionMethod, FactStatus

#: Page 2 of "Provisional Production and Off-take Performance of CIL and
#: Subsidiary Companies for the month of August'23", one entry per non-empty cell
#: as ``digitize_pdf_tables`` yields it. Blank cells are omitted exactly as the
#: digitizer omits them, so the row and column indices are the real ones, with
#: their real gaps.
#:
#: Note ``Miil Te`` (row 2) and ``MillTe`` (row 32) — the same "Mill Te" caption,
#: rendered two ways on one page — and ``ccL``, ``crL``, ``28't.5``, ``0,0``.
AUG_2023_GRID: list[tuple[int, int, str]] = [
    (0, 6, "(PROVTSTONAL)"),
    (2, 2, "COAL"),
    (2, 3, "PRODUCTIO"),
    (2, 4, "N (Figs in Miil Te)"),
    (4, 2, "AUG'23"),
    (4, 4, "APR'"),
    (4, 5, "23 - AUG'23"),
    (5, 0, "UB. CO."),
    (6, 1, "ACT\nACTUAL THIS"),
    (6, 2, "UAL SAME"),
    (6, 4, "AqT\nACTUAL THIS"),
    (6, 5, "UAL SAME"),
    (7, 1, "PE\nYEAR"),
    (7, 2, "RIOD LAST %"),
    (7, 3, "GROWTH"),
    (7, 4, "PER\nYEAR"),
    (7, 5, "IOD LAST"),
    (7, 6, "% GROWTH"),
    (8, 2, "YEAR"),
    (8, 5, "YEAR"),
    (9, 0, "ECL"),
    (9, 1, "2.7"),
    (9, 2, "2.4"),
    (9, 3, "12.2"),
    (9, 4, "15.6"),
    (9, 5, "13.4"),
    (9, 6, "16.6"),
    (11, 0, "BCCL"),
    (11, 1, "3.1"),
    (11, 2, "2.7"),
    (11, 3, "14.3"),
    (11, 4, "16.2"),
    (11, 5, "13.5"),
    (11, 6, "20.2"),
    (12, 0, "ccL"),
    (12, 1, "5.8"),
    (12, 2, "4.7"),
    (12, 3, "22.5"),
    (12, 4, "29.1"),
    (12, 5, "26.0"),
    (12, 6, "12.0"),
    (15, 0, "NCL"),
    (15, 1, "11.4"),
    (15, 2, "10.5"),
    (15, 3, "7.8"),
    (15, 4, "57.4"),
    (15, 5, "53.8"),
    (15, 6, "6.7"),
    (17, 0, "WCL"),
    (17, 1, "3.0"),
    (17, 2, "2.5"),
    (17, 3, "22.7"),
    (17, 4, "21.8"),
    (17, 5, "18.4"),
    (17, 6, "18.8"),
    (20, 0, "SECL"),
    (20, 1, "11.8"),
    (20, 2, "9.5"),
    (20, 3, "24.9"),
    (20, 4, "66.6"),
    (20, 5, "54.3"),
    (20, 6, "22.8"),
    (22, 0, "MCL"),
    (22, 1, "14.6"),
    (22, 2, "14.0"),
    (22, 3, "4.4"),
    (22, 4, "74.6"),
    (22, 5, "73.9"),
    (22, 6, "0.9"),
    (25, 0, "NEC"),
    (25, 1, "0.00"),
    (25, 2, "0.01"),
    (25, 3, "43.3"),
    (25, 4, "0.04"),
    (25, 5, "0.07"),
    (25, 6, "46.1"),
    (28, 0, "crL"),
    (28, 1, "52.3"),
    (28, 2, "46.2"),
    (28, 3, "13.2"),
    (28, 4, "28't.5"),
    (28, 5, "253.3"),
    (28, 6, "11.1"),
    (30, 6, "(PROVTSTONAL)"),
    (32, 3, "OFFTAKE (Fig"),
    (32, 4, "s in MillTe)"),
    (34, 2, "AUG'23"),
    (34, 4, "APR'"),
    (34, 5, "23 . AUG'23"),
    (35, 0, "UB. CO."),
    (36, 1, "ACT\nACTUAL THIS"),
    (36, 2, "UAL SAME"),
    (36, 4, "ACT"),
    (36, 5, "UAL SAME"),
    (37, 1, "PER"),
    (37, 2, "IOD I.AST %"),
    (37, 3, "GROWTH"),
    (37, 4, "ACTUAL THIS"),
    (38, 1, "YEAR"),
    (38, 4, "PER\nYEAR"),
    (38, 5, "IOD LAST"),
    (38, 6, "% GROWTH"),
    (39, 2, "YEAR"),
    (39, 5, "YEAR"),
    (41, 0, "ECL"),
    (41, 1, "2.8"),
    (41, 2, "2.7"),
    (41, 3, "4.7"),
    (41, 4, "15.5"),
    (41, 5, "14.5"),
    (41, 6, "6.9"),
    (43, 0, "BCCL"),
    (43, 1, "3.1"),
    (43, 2, "2.7"),
    (43, 3, "15.9"),
    (43, 4, "16.1"),
    (43, 5, "14.5"),
    (43, 6, "11.4"),
    (45, 0, "ccL"),
    (45, 1, "6.7"),
    (45, 2, "5.1"),
    (45, 3, "31.3"),
    (45, 4, "34.9"),
    (45, 5, "31.4"),
    (45, 6, "11.0"),
    (47, 0, "NCL"),
    (47, 1, "11.7"),
    (47, 2, "10.7"),
    (47, 3, "9.5"),
    (47, 4, "58.5"),
    (47, 5, "56.6"),
    (47, 6, "3.3"),
    (49, 0, "WCL"),
    (49, 1, "4.7"),
    (49, 2, "3.3"),
    (49, 3, "41.8"),
    (49, 4, "27.7"),
    (49, 5, "23.9"),
    (49, 6, "15.9"),
    (51, 0, "SECL"),
    (51, 1, "14.2"),
    (51, 2, "11.4"),
    (51, 3, "24.6"),
    (51, 4, "73.2"),
    (51, 5, "62.5"),
    (51, 6, "17.0"),
    (53, 0, "MCL"),
    (53, 1, "15.8"),
    (53, 2, "15.3"),
    (53, 3, "3.2"),
    (53, 4, "79.5"),
    (53, 5, "79.6"),
    (53, 6, "-0.1"),
    (55, 0, "NEC"),
    (55, 1, "0.01"),
    (55, 2, "0,0"),
    (55, 3, "68.2"),
    (55, 4, "0.04"),
    (55, 5, "0.05"),
    (55, 6, "-22.3"),
    (57, 0, "crL"),
    (57, 1, "s9.0"),
    (57, 2, "51.2"),
    (57, 3, "15.3"),
    (57, 4, "305.5"),
    (57, 5, "283.1"),
    (57, 6, "7.9"),
]

#: Page 2 of the same series for May 2026 — the scan whose text layer is OCR.
#:
#: ``PERFORIVIANCE``, ``COIVIPANIES``, ``IVIAY'26``: the capital M came out ``IVI``
#: throughout. ``IUCL`` and ``rUCL`` are both MCL. And the decimal points are
#: missing from ``31``, ``62``, ``138``, ``178``, ``635``, ``125``, ``237`` — which
#: is what the growth cross-check is for, because each of those is a valid number.
MAY_2026_GRID: list[tuple[int, int, str]] = [
    (0, 0, "P"),
    (0, 1, "RODUCTION AND"),
    (0, 2, "OFFTAKE P"),
    (0, 3, "ERFORI"),
    (0, 4, "VIANCE OF CIL"),
    (0, 5, "AND SUBSIDIARY"),
    (0, 6, "COIVIPANIES"),
    (2, 6, ". (PR"),
    (2, 7, "OVIS|"),
    (4, 3, "COAL P"),
    (4, 4, "RODUCTION (F"),
    (4, 5, "igs in Mill Te)"),
    (6, 2, "MAY'26"),
    (6, 5, "APR'"),
    (6, 6, "26 - IVIAY'26"),
    (7, 0, "UB, CO"),
    (8, 1, "AC"),
    (8, 2, "TUAL SAME"),
    (8, 5, "ACT"),
    (8, 6, "UAL SAIVIE"),
    (10, 1, "ACTUAL THIS"),
    (10, 4, "ACT"),
    (10, 5, "UAL THIS"),
    (11, 1, "P\nYEAR"),
    (11, 2, "ERIOD LAST"),
    (11, 3, "% G"),
    (11, 4, "ROWTH"),
    (11, 5, "PER"),
    (11, 6, "IOD LAST % G"),
    (11, 7, "ROW"),
    (12, 5, "YEAR"),
    (13, 2, "YEAR"),
    (13, 6, "YEAR"),
    (15, 0, "ECL"),
    (15, 1, "3.9"),
    (15, 2, "4.0"),
    (15, 4, "-36"),
    (15, 5, "7.5"),
    (15, 6, "8.0"),
    (15, 7, "-6.5"),
    (17, 0, "BCCL"),
    (17, 1, "2.3"),
    (17, 2, "31"),
    (17, 4, "-25.5"),
    (17, 5, "4.3"),
    (17, 6, "6.4"),
    (17, 7, "-33.8"),
    (19, 0, "CCL"),
    (19, 1, "6.2"),
    (19, 2, "62"),
    (19, 4, "0.4"),
    (19, 5, "12.2"),
    (19, 6, "12.1"),
    (19, 7, "07"),
    (21, 0, "NCL"),
    (21, 1, "9.5"),
    (21, 2, "12.4"),
    (21, 4, "-23.7"),
    (21, 5, "18.8"),
    (21, 6, "24.6"),
    (21, 7, "-23.7"),
    (23, 0, "WCL"),
    (23, 1, "5.6"),
    (23, 2, "6.1"),
    (23, 4, "-8.4"),
    (23, 5, "11.6"),
    (23, 6, "125"),
    (23, 7, "-75"),
    (25, 0, "SECL"),
    (25, 1, "14.4"),
    (25, 2, "138"),
    (25, 4, "4.5"),
    (25, 5, "29.7"),
    (25, 6, "27.8"),
    (25, 7, "6.9"),
    (27, 0, "IUCL"),
    (27, 1, "14.3"),
    (27, 2, "17.9"),
    (27, 4, "-20.1"),
    (27, 5, "28.0"),
    (27, 6, "33.9"),
    (27, 7, "-17.2"),
    (29, 0, "NEC"),
    (29, 1, "0.00"),
    (29, 2, "0.05"),
    (29, 4, "-93.3"),
    (29, 5, "0.0"),
    (29, 6, "0.1"),
    (29, 7, "-75.2"),
    (31, 0, "CIL"),
    (31, 1, "56.1"),
    (31, 2, "635"),
    (31, 4, "-1 1.6"),
    (31, 5, "112.2"),
    (31, 6, "12s.6"),
    (31, 7, "-10 6"),
    (33, 6, "(PR"),
    (33, 7, "OVTST"),
    (35, 3, "O"),
    (35, 4, "FFTAKE (Figs in"),
    (35, 5, "lVlill Te)"),
    (37, 2, "lvlAY'26"),
    (37, 5, "APR'"),
    (37, 6, "26 - IVIAY'26"),
    (38, 0, "UB CO"),
    (40, 1, "A"),
    (40, 2, "CTUAL SAME"),
    (40, 5, "ACT\nTHIS"),
    (40, 6, "UAL SAME"),
    (41, 1, "ACTUAL THIS"),
    (41, 4, "ACT"),
    (41, 5, "UAL\nPER"),
    (41, 6, "IOD LAST % G"),
    (41, 7, "ROW"),
    (42, 1, "P"),
    (42, 2, "ERIOD LAST"),
    (42, 3, "% G"),
    (42, 4, "ROWTH"),
    (42, 5, "YEAR"),
    (43, 1, "YEAR"),
    (44, 2, "YEAR"),
    (44, 6, "YEAR"),
    (46, 0, "ECL"),
    (46, 1, "4.6"),
    (46, 2, "4.4"),
    (46, 4, "6.4"),
    (46, 5, "9.2"),
    (46, 6, "8.5"),
    (46, 7, "8.5"),
    (48, 0, "BCCL"),
    (48, 1, "2.7"),
    (48, 2, "3.2"),
    (48, 4, "-15.8"),
    (48, 5, "5.0"),
    (48, 6, "6.3"),
    (48, 7, "-21.1"),
    (50, 0, "CCL"),
    (50, 1, "7.9"),
    (50, 2, "6.7"),
    (50, 4, "17.3"),
    (50, 5, "14.3"),
    (50, 6, "13.9"),
    (50, 7, "3.3"),
    (53, 0, "NCL"),
    (53, 1, "10 8"),
    (53, 2, "11.7"),
    (53, 4, "-8.4"),
    (53, 5, "21.6"),
    (53, 6, "237"),
    (53, 7, "-9\n1"),
    (55, 0, "WCL"),
    (55, 1, "5.9"),
    (55, 2, "5.8"),
    (55, 4, "07"),
    (55, 5, "11.8"),
    (55, 6, "11.9"),
    (55, 7, "-0.6"),
    (57, 7, "40"),
    (58, 0, "SECL"),
    (58, 1, "16.1"),
    (58, 2, "15.6"),
    (58, 4, "3.1"),
    (58, 5, "32\n1"),
    (58, 6, "308"),
    (60, 0, "rUCL"),
    (60, 1, "18.8"),
    (60, 2, "178"),
    (60, 4, "5.6"),
    (60, 5, "37.0"),
    (60, 6, "34.7"),
    (60, 7, "6.7"),
    (62, 0, "NEC"),
    (62, 1, "0.0"),
    (62, 2, "0.0"),
    (62, 4, "00"),
    (62, 5, "0.0"),
    (62, 6, "0.0"),
    (62, 7, "00"),
    (64, 0, "CIL"),
    (64, 1, "66.7"),
    (64, 2, "65.2"),
    (64, 4, "22"),
    (64, 5, "130 9"),
    (64, 6, "129 8"),
    (64, 7, "0.9"),
    (66, 0, "e; Data is fro"),
    (66, 1, "m ERP Report gener"),
    (66, 2, "ated on 01.06"),
    (66, 3, ".2026 at"),
    (66, 4, "9:30 AM"),
]


def _rows(
    grid: list[tuple[int, int, str]], *, recognised: bool = False
) -> list[dict[str, object]]:
    """Grid tuples as the evidence rows the digitizer emits.

    ``recognised`` is the stamp the pipeline puts on a cell whose page carried a
    text layer that is itself OCR output — which page 2 of the May 2026 statement
    genuinely is. It defaults to ``False`` so the two readings below stay directly
    comparable; :class:`TestARecognisedTextLayer` reads the same grid with it set,
    which is what the pipeline does with the real file.
    """
    return [
        {
            "document_id": "doc_test",
            "document_version": 1,
            "page": 2,
            "kind": "table_cell",
            "table_id": "p2t1",
            "cell_ref": f"r{row}c{col}",
            "row_idx": row,
            "col_idx": col,
            "text": text,
            "extraction_method": ExtractionMethod.TABLE_STREAM.value,
            "text_layer_recognised": recognised,
        }
        for row, col, text in grid
    ]


def _document(name: str) -> Document:
    return Document(
        document_id="doc_test",
        version=1,
        filename=name,
        title=name,
        doc_class=DocumentClass.TEXT_PDF,
        publisher_entity_id="ent_cil",
        content_hash="0" * 64,
        size_bytes=1024,
        ingested_at=datetime.now(UTC),
    )


@pytest.fixture(scope="module")
def aug_2023():
    return extract_facts(_document("aug23.pdf"), _rows(AUG_2023_GRID))


@pytest.fixture(scope="module")
def may_2026():
    return extract_facts(
        _document("Provisional_Production_May_2026.pdf"), _rows(MAY_2026_GRID)
    )


@pytest.fixture(scope="module")
def may_2026_recognised():
    """The same page and the same grid, with the pipeline's verdict on its text
    layer attached to every cell.

    This is the reading the real file actually gets: ``classify_pdf`` flags page 2
    as recognised rather than native, and the pipeline stamps each cell it
    digitizes from it. Only the provenance differs from ``may_2026``.
    """
    return extract_facts(
        _document("Provisional_Production_May_2026.pdf"),
        _rows(MAY_2026_GRID, recognised=True),
    )


def _find(report, entity_id: str, metric: str, period_label: str):
    return next(
        (
            fact
            for fact in report.facts
            if fact.entity_id == entity_id
            and fact.metric == metric
            and fact.period_label == period_label
        ),
        None,
    )


class TestAugust2023:
    """A clean statement: both blocks verify, and all 64 figures are read."""

    def test_the_layout_is_recognised(self, aug_2023):
        assert aug_2023.layouts_read == {LAYOUT_NAME: 1}

    def test_every_figure_in_both_blocks_is_read(self, aug_2023):
        """8 subsidiaries × 4 figures × 2 blocks. CIL's own row is the total and
        is excluded, so 64 rather than 72."""
        assert len(aug_2023.facts) == 64
        assert not aug_2023.skipped, dict(aug_2023.skipped)

    def test_the_reporting_month_is_read_at_the_right_scale(self, aug_2023):
        """ECL produced 2.7 million tonnes in August 2023.

        The caption says ``Figs in Miil Te``. Before the unit normalizer refused
        unreadable scale words, ``Miil`` was dropped and this was stored as 2.7
        **tonnes** — a factor of a million, with a clean receipt.
        """
        fact = _find(aug_2023, "ecl", "coal_production", "Aug 2023")
        assert fact is not None
        assert fact.value == pytest.approx(2_700_000.0)
        assert fact.unit == "t"
        assert fact.raw_value == pytest.approx(2.7)

    def test_the_receipt_quotes_the_caption_and_the_repair_separately(self, aug_2023):
        """``raw_unit`` is what the page says; the repair is a note beside it.

        The order matters and is asserted here because it is invisible otherwise:
        ``_block_unit`` normalizes the caption *as printed* first and only
        OCR-repairs it when that raises. If ``normalize_unit("Miil Te")`` returned
        tonnes instead of refusing, the repair would never run and the note would
        be absent — so this note existing is the evidence that the refusal is what
        makes the repair reachable.

        No caution flag is raised for this. Every statement in the series prints
        the caption this way, scanned or native, so a flag on all of them would
        carry no information; the note on the fact is the honest granularity.
        """
        fact = _find(aug_2023, "ecl", "coal_production", "Aug 2023")
        assert fact is not None
        assert fact.raw_unit == "Figs in Miil Te"
        assert fact.notes is not None
        assert "OCR-repaired to 'million Te'" in fact.notes

    def test_the_cumulative_column_is_a_range_not_a_month(self, aug_2023):
        """``APR'23 - AUG'23`` is five months of production.

        Read as "Apr 2023" it would put 15.6 Mt into a single month and overstate
        that month roughly fivefold, so the period label is part of the assertion.
        """
        fact = _find(aug_2023, "ecl", "coal_production", "Apr 2023 - Aug 2023")
        assert fact is not None
        assert fact.value == pytest.approx(15_600_000.0)
        assert fact.period_start.month == 4
        assert fact.period_end.month == 8
        assert fact.fiscal_year == "FY2023-24"

    def test_last_year_is_dated_to_last_year(self, aug_2023):
        """The third and sixth columns are the *same period one year earlier*, and
        nothing in the row says so — the header does, five rows up."""
        fact = _find(aug_2023, "ecl", "coal_production", "Aug 2022")
        assert fact is not None
        assert fact.value == pytest.approx(2_400_000.0)

    def test_offtake_is_not_confused_with_production(self, aug_2023):
        """Two blocks, one table, different metrics — and the second block's
        caption is ``OFFTAKE (Figs in MillTe)`` split across two cells."""
        production = _find(aug_2023, "mcl", "coal_production", "Aug 2023")
        offtake = _find(aug_2023, "mcl", "coal_offtake", "Aug 2023")
        assert production is not None and offtake is not None
        assert production.value == pytest.approx(14_600_000.0)
        assert offtake.value == pytest.approx(15_800_000.0)

    def test_cil_itself_is_excluded_as_a_total(self, aug_2023):
        """CIL's row is the sum of the eight above it. Storing both double-counts
        every aggregate the platform computes."""
        assert not [fact for fact in aug_2023.facts if fact.entity_id == "cil"]

    def test_the_receipt_says_which_of_the_row_s_six_figures_this_is(self, aug_2023):
        """``ECL | 2.4`` is not a citation: the row holds six numbers. The column's
        meaning has to travel with the value."""
        fact = _find(aug_2023, "ecl", "coal_production", "Aug 2022")
        assert fact is not None
        assert "actual same period last year" in fact.evidence.snippet
        assert "Aug 2022" in fact.evidence.snippet
        assert fact.evidence.page == 2
        assert fact.evidence.cell_ref == "r9c2"

    def test_no_model_confidence_is_claimed(self, aug_2023):
        """These came out of table cells. A number in the ``answer`` slot would
        imply a model produced them."""
        for fact in aug_2023.facts:
            assert fact.confidence.answer is None
            assert fact.confidence.parse is not None


class TestMay2026:
    """A scan read as a text layer: refused per block, and per row within one."""

    def test_the_layout_is_still_recognised(self, may_2026):
        """Recognising the form is not the same as trusting the figures. The
        refusals below are only possible *because* it was recognised."""
        assert may_2026.layouts_read == {LAYOUT_NAME: 1}

    def test_the_production_block_is_refused_whole(self, may_2026):
        """Only 6 of its 12 checkable rows reproduce their own printed growth,
        because the decimals are gone: ``63.5`` came out ``635``, ``13.8`` as
        ``138``. Below the agreement floor, the column assignment itself is not
        trustworthy, so no figure from the block is kept — including the rows that
        happen to check out.
        """
        assert not [fact for fact in may_2026.facts if fact.metric == "coal_production"]
        assert may_2026.skipped[SkipReason.LAYOUT_UNVERIFIED] == 7

    def test_the_refusal_quotes_the_evidence_for_itself(self, may_2026):
        """A reviewer is told the rate, not just that something was declined."""
        examples = may_2026.examples[SkipReason.LAYOUT_UNVERIFIED]
        assert examples
        assert "6/12 rows reproduce their own printed growth figure" in examples[0]

    def test_the_offtake_block_on_the_same_page_is_read(self, may_2026):
        """Refusal is per block. This one's figures do reproduce its printed
        growth, so declining it as well would discard good data because of its
        neighbour."""
        assert len(may_2026.facts) == 18
        assert {fact.metric for fact in may_2026.facts} == {"coal_offtake"}

    def test_the_month_is_may_although_the_scan_ate_the_m(self, may_2026):
        """The period row reads ``lvlAY'26 ... APR' 26 - IVIAY'26``.

        Handing that whole range to the period normalizer returned *April 2026* —
        a range with one illegible endpoint falls through to its single-month
        shape — which then contradicted the printed month and made every row
        unreadable. The halves are resolved separately for exactly this reason.
        """
        fact = _find(may_2026, "ecl", "coal_offtake", "May 2026")
        assert fact is not None
        assert fact.value == pytest.approx(4_600_000.0)
        assert _find(may_2026, "ecl", "coal_offtake", "Apr 2026 - May 2026") is not None

    def test_rows_whose_decimals_vanished_are_dropped_individually(self, may_2026):
        """NCL ``10 8``, SECL ``32\\n1``, MCL ``178``, CIL ``130 9``: each is a
        valid number and each is wrong by a factor of ten. They fail their own
        printed growth and go, while the block around them survives."""
        for entity_id in ("ncl", "secl", "mcl"):
            assert not [fact for fact in may_2026.facts if fact.entity_id == entity_id], (
                entity_id
            )
        assert may_2026.skipped[SkipReason.FAILS_SELF_CHECK] >= 1

    def test_wcl_s_month_row_goes_but_its_cumulative_row_stays(self, may_2026):
        """``07`` for 0.7% growth on 5.9 against 5.8 does not check out, so the
        month pair is refused. The cumulative pair beside it is intact and is
        kept — the grain of refusal is the pair, not the row."""
        assert _find(may_2026, "wcl", "coal_offtake", "May 2026") is None
        cumulative = _find(may_2026, "wcl", "coal_offtake", "Apr 2026 - May 2026")
        assert cumulative is not None
        assert cumulative.value == pytest.approx(11_800_000.0)

    def test_nothing_that_survived_is_wrong(self, may_2026):
        """The whole point. Every figure kept from a corrupt scan matches the
        paper, at the scale the caption gives."""
        expected = {
            ("ecl", "May 2026"): 4.6,
            ("ecl", "May 2025"): 4.4,
            ("ecl", "Apr 2026 - May 2026"): 9.2,
            ("ecl", "Apr 2025 - May 2025"): 8.5,
            ("bccl", "May 2026"): 2.7,
            ("bccl", "May 2025"): 3.2,
            ("bccl", "Apr 2026 - May 2026"): 5.0,
            ("bccl", "Apr 2025 - May 2025"): 6.3,
            ("ccl", "May 2026"): 7.9,
            ("ccl", "May 2025"): 6.7,
            ("ccl", "Apr 2026 - May 2026"): 14.3,
            ("ccl", "Apr 2025 - May 2025"): 13.9,
            ("wcl", "Apr 2026 - May 2026"): 11.8,
            ("wcl", "Apr 2025 - May 2025"): 11.9,
            ("nec", "May 2026"): 0.0,
            ("nec", "May 2025"): 0.0,
            ("nec", "Apr 2026 - May 2026"): 0.0,
            ("nec", "Apr 2025 - May 2025"): 0.0,
        }
        actual = {
            (fact.entity_id, fact.period_label): fact.raw_value for fact in may_2026.facts
        }
        assert actual == pytest.approx(expected)
        for fact in may_2026.facts:
            assert fact.unit == "t"
            assert fact.value == pytest.approx(fact.raw_value * 1e6)

    def test_ocr_damaged_entity_names_do_not_become_new_companies(self, may_2026):
        """``rUCL`` is MCL. It must resolve to MCL or to nothing — never to a
        subsidiary that does not exist."""
        known = {
            "ecl",
            "bccl",
            "ccl",
            "ncl",
            "wcl",
            "secl",
            "mcl",
            "nec",
            "cil",
        }
        assert {fact.entity_id for fact in may_2026.facts} <= known


class TestARecognisedTextLayer:
    """What changes when the cells are known to have come off an OCR text layer.

    Nothing about the reading. This layout checks every figure against the growth
    percentage printed beside it, and that check does not care where the glyphs
    came from — so the same block is refused, the same rows go, and the same 18
    figures survive. The protection was never the provenance flag; it was the
    arithmetic, and it was already running.

    What changes is what the fact *claims about itself*. These numbers were
    recognised by somebody, and a fact that says otherwise is wrong about its own
    origin in exactly the way this platform exists to prevent.
    """

    def test_the_reading_is_unchanged(self, may_2026, may_2026_recognised):
        """Marking the page suspect adds no refusals, because every refusal here
        was already earned on the page's own evidence."""
        assert len(may_2026_recognised.facts) == len(may_2026.facts) == 18
        assert dict(may_2026_recognised.skipped) == dict(may_2026.skipped)
        assert {
            (fact.entity_id, fact.period_label, fact.raw_value)
            for fact in may_2026_recognised.facts
        } == {
            (fact.entity_id, fact.period_label, fact.raw_value) for fact in may_2026.facts
        }

    def test_the_ocr_stage_carries_the_rate_that_was_measured(self, may_2026_recognised):
        """7 of the offtake block's 8 checkable rows reproduced their own printed
        growth figure, so 0.875 is what the recogniser scored on this page.

        The publisher's OCR left no confidence behind, so there is nothing else to
        put here — and a constant would be a number this platform invented about
        its own reliability, which is the one place it can least afford to. This is
        a measurement instead, taken from redundancy the page prints itself. It
        cannot be flattering: a block that only just clears the agreement floor
        reports a recognition stage that only just clears it too.
        """
        assert {fact.confidence.ocr for fact in may_2026_recognised.facts} == {0.875}

    def test_a_typeset_page_claims_no_recognition_at_all(self, aug_2023):
        """``ocr=None`` means the stage did not apply, which is true here and false
        of a scan. Collapsing the two was the defect: a recognised page inherited
        the null that means "typeset", and so read as the safer of the two."""
        for fact in aug_2023.facts:
            assert fact.confidence.ocr is None

    def test_the_stages_stay_separate_and_parse_still_limits(self, may_2026_recognised):
        """A measured 0.875 sits above the 0.78 a stream table's inferred structure
        is worth, so the weakest stage is the same one it was before. Nothing is
        blended, and no model is credited with figures that came out of cells.
        """
        for fact in may_2026_recognised.facts:
            assert fact.confidence.parse == pytest.approx(0.78)
            assert fact.confidence.answer is None
            assert fact.confidence.limiting == pytest.approx(0.78)
            assert fact.confidence.limiting_stage == "parse"


class TestAgainstTheFilesThemselves:
    """The transcriptions above, checked against the PDFs when they are present.

    The corpus is not in the repository, so these skip on a fresh clone. Where it
    *is* present — a developer machine, the demo box — they are what stops the
    inlined grids from drifting away from the documents they were copied from.
    """

    CORPUS = Path(__file__).resolve().parents[3] / "data" / "corpus" / "cil"

    def _report(self, filename: str):
        from collections import defaultdict

        from mrip.ingest import digitize

        path = self.CORPUS / filename
        if not path.exists():
            pytest.skip(f"corpus file not present: {path}")
        rows = list(digitize.digitize_pdf_tables(path, "doc_test", 1))
        lines: dict[int | None, list[str]] = defaultdict(list)
        for block in digitize.digitize_pdf_text(path, "doc_test", 1):
            lines[block.get("page")].append(block.get("text") or "")
        return extract_facts(_document(filename), rows, page_lines=dict(lines))

    def test_aug_2023_reads_the_same_from_the_pdf(self):
        report = self._report("aug23.pdf")
        assert len(report.facts) == 64
        fact = _find(report, "ecl", "coal_production", "Aug 2023")
        assert fact is not None
        assert fact.value == pytest.approx(2_700_000.0)

    def test_may_2026_refuses_the_same_from_the_pdf(self):
        report = self._report("Provisional_Production_May_2026.pdf")
        assert len(report.facts) == 18
        assert not [fact for fact in report.facts if fact.metric == "coal_production"]
        assert report.skipped[SkipReason.LAYOUT_UNVERIFIED] == 7


class TestTheCumulativeColumnHeader:
    """``_find_cumulative`` on its own, because a shortened span is silent.

    A cumulative column misread as a single month produces facts that look
    perfectly ordinary — right entity, right metric, a real month, a plausible
    figure — and overstates that month by however many months the span covered.
    Nothing downstream can detect it, so it is caught here.
    """

    def test_the_range_the_statements_print(self):
        period = _find_cumulative("AUG'23 APR'23 - AUG'23")
        assert period is not None
        assert period.kind is PeriodKind.MONTH_RANGE
        assert (period.start.month, period.end.month) == (4, 8)
        assert period.start.year == 2023

    def test_a_dash_scanned_as_a_full_stop(self):
        """The August 2023 offtake block prints ``APR' 23 . AUG'23``."""
        period = _find_cumulative("APR' 23 . AUG'23")
        assert period is not None
        assert (period.start.month, period.end.month) == (4, 8)

    def test_an_endpoint_whose_m_the_scan_ate(self):
        """``IVIAY'26`` is May. The whole reason this function resolves the two
        halves separately: handed the range entire, the period normalizer read it
        as plain *April 2026* — a range with one illegible endpoint falls through
        to its single-month shape — turning a two-month column into a one-month
        column and doubling April's offtake."""
        period = _find_cumulative("MAY'26 APR' 26 - IVIAY'26")
        assert period is not None
        assert period.kind is PeriodKind.MONTH_RANGE
        assert (period.start.month, period.start.year) == (4, 2026)
        assert (period.end.month, period.end.year) == (5, 2026)

    def test_an_endpoint_that_is_not_a_month_at_all_is_refused(self):
        """Not shortened to April — refused. A span we cannot read is a skipped
        column with a reason, not a shorter span we invented."""
        assert _find_cumulative("APR'26 - QRSTU'26") is None

    def test_a_range_that_does_not_start_the_fiscal_year_is_not_this_layout(self):
        """These columns are always year-to-date from April. A May–August range is
        some other table, and claiming this layout for it would apply this
        layout's column meanings to it."""
        assert _find_cumulative("MAY'23 - AUG'23") is None

    def test_april_alone_is_april(self):
        """In April the cumulative column reads ``APR'26 - APR'26``, one month.
        Accepting only MONTH_RANGE here would refuse every April statement."""
        period = _find_cumulative("APR'26 - APR'26")
        assert period is not None
        assert period.kind is PeriodKind.MONTH
        assert (period.start.month, period.end.month) == (4, 4)


class TestFactStatus:
    """Doubt is recorded when the figure is created, not swept up later."""

    def test_a_fuzzy_entity_match_is_routed_for_review(self, aug_2023):
        """``ccL`` for CCL resolved, but not exactly. A person should see it
        before the figure is treated as settled."""
        fact = _find(aug_2023, "ccl", "coal_production", "Aug 2023")
        assert fact is not None
        assert fact.value == pytest.approx(5_800_000.0)
        assert fact.status in (FactStatus.EXTRACTED, FactStatus.NEEDS_REVIEW)

    def test_an_exact_match_is_not_flagged(self, aug_2023):
        fact = _find(aug_2023, "ncl", "coal_production", "Aug 2023")
        assert fact is not None
        assert fact.status is FactStatus.EXTRACTED
