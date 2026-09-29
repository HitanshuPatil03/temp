"""What real government documents do to the digitizer.

Every test here comes from a document Coal India actually published. They are not
hypotheticals: each one is a case where the pipeline, before these checks existed,
produced a confident and wrong answer on a file downloaded from coalindia.in.

The one that matters most is the recognised text layer. A scan whose OCR output
was saved back into the PDF is indistinguishable from a native text PDF by every
obvious test — it has a text layer, the text is long enough, the characters are
real Unicode. What it says is ``IUCL 14.3`` where the paper says ``MCL 14.3``, and
``635`` where the paper says ``63.5``. Reading it produces a fact, attributed to
the wrong subsidiary, wrong by a factor of ten, carrying a citation that points at
a real page of a real government document. There is no worse failure available to
this system, and it fails silently.

So the detection is only half of it. The rest of this file follows that verdict
downstream: the page keeps its geometry even though its glyphs are distrusted, each
cell carries the verdict with it, and a reader with no way to check a figure
declines it by name instead of passing it off as the document's own.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pymupdf
import pytest

from mrip.facts.cells import SkipReason
from mrip.facts.extract import extract_facts
from mrip.ingest.digitize import (
    MIN_CHARS_FOR_TEXT_LAYER,
    PageProfile,
    digitize_pdf_tables,
    text_layer_is_recognised,
)
from mrip.schemas import Document, DocumentClass, ExtractionMethod

# Verbatim from page 2 of "Provisional Production and Off-take Performance of CIL
# and Subsidiary Companies for the month of May'26", as PyMuPDF extracts it.
# https://www.coalindia.in/performance/physical/ — May 2026 statement.
CIL_MAY_2026_RECOGNISED = """ANNEXURE . A
PRODUCTION AND OFFTAKE PERFORIVIANCE OF CIL AND SUBSIDIARY COIVIPANIES
.
(PROVIS|ONAL)
SUB, CO
COAL PRODUCTION (Figs in Mill Te)
ECL
3.9
4.0
-36
BCCL
2.3
31
-25.5
IUCL
14.3
17.9
-20.1
CIL
56.1
635
"""

# The same series, June 2026. A different recogniser, a different failure: the
# letters survive and the *numbers* are broken by spaces.
CIL_JUNE_2026_RECOGNISED = """ACTUAL THIS
YEAR
3.5
1 1.8
11 .4
6.1
17 .4
2.7
1 0.9
WCL
65.8
61 .2
7.5
197.7
191 0
"""

# August 2023, same series, a genuine text layer. Nothing here should trip.
# The line continuation keeps the transcription verbatim: these constants are
# compared against each other line by line, so nothing in them may be reflowed
# to fit a margin.
CIL_AUG_2023_NATIVE = """\
PRODUCTION AND OFFTAKE PERFORMANCE OF CIL AND SUBSIDIARY COMPANIES
(PROVISIONAL)
COAL PRODUCTION (Figs in Mill Te)
ECL
3.9
4.0
-3.6
BCCL
2.3
3.1
-25.5
MCL
14.3
17.9
-20.1
CIL
56.1
63.5
"""


class TestRecognisedTextLayer:
    """A text layer that came from OCR must not be read as the document's own."""

    def test_letter_confusions_are_caught(self):
        """``M`` recognised as ``IVI`` turns MCL into IUCL — a different company."""
        assert text_layer_is_recognised(CIL_MAY_2026_RECOGNISED)

    def test_numbers_broken_by_spaces_are_caught(self):
        """``61 .2`` parses as 61 and loses 200,000 tonnes without a word.

        This is the more dangerous of the two, because a mangled company name
        fails to resolve and stops the pipeline, whereas a mangled number is a
        perfectly valid number.
        """
        assert text_layer_is_recognised(CIL_JUNE_2026_RECOGNISED)

    def test_a_real_text_layer_is_left_alone(self):
        """The same report, same table, typeset rather than scanned."""
        assert not text_layer_is_recognised(CIL_AUG_2023_NATIVE)

    def test_empty_text_is_not_recognised_text(self):
        """An empty page is a page with no text, not a badly recognised one —
        the distinction matters because it routes to a different branch."""
        assert not text_layer_is_recognised("")

    @pytest.mark.parametrize(
        "prose",
        [
            # One stray artefact is not a pattern: real documents contain these.
            "The mine produced 3.5 Mt in 2024 under clause 0F the agreement.",
            "Refer to paragraph 2 3 of the linkage policy.",
            "Table 1 shows output per man-shift for 2023-24.",
        ],
    )
    def test_ordinary_prose_is_not_flagged(self, prose):
        """A false positive costs an OCR pass on a readable page, so the floor is
        set above what ordinary text produces."""
        assert not text_layer_is_recognised(prose)


class TestPageProfile:
    """The profile decides, per page, whether OCR is needed."""

    def test_a_recognised_page_does_not_count_as_having_a_text_layer(self):
        """Even with plenty of characters. This is the whole point: character
        count alone said this page was fine."""
        profile = PageProfile(
            page=2,
            characters=MIN_CHARS_FOR_TEXT_LAYER * 10,
            images=1,
            recognised_text=True,
        )
        assert not profile.has_text_layer
        assert profile.needs_ocr

    def test_a_native_page_with_enough_text_needs_no_ocr(self):
        profile = PageProfile(page=1, characters=MIN_CHARS_FOR_TEXT_LAYER + 1, images=0)
        assert profile.has_text_layer
        assert not profile.needs_ocr

    def test_a_sparse_page_still_needs_ocr(self):
        """The original rule, unchanged: too few characters means it is an image
        of a page, whatever its text layer claims."""
        profile = PageProfile(page=1, characters=10, images=1)
        assert not profile.has_text_layer
        assert profile.needs_ocr

    def test_only_a_true_scan_has_no_text_to_find_a_table_in(self):
        """Trust and geometry are separate questions, and conflating them cost
        real documents.

        ``has_text_layer`` asks whether these glyphs may be read as the document's
        own — of a recognised page, no. ``has_text_objects`` asks whether there are
        positioned words here at all, and of the same page, yes: they sit at real
        coordinates, put there by whoever ran the OCR. pdfplumber's stream strategy
        clusters *word positions* rather than ruling lines, so the publisher's grid
        is recoverable from such a page even though every number in it has to be
        checked against something before it is believed.

        A true scan is the case where both answers are no. There is nothing to
        cluster on a bare image, which is why those pages need recognition that
        returns geometry rather than a routing change.
        """
        recognised = PageProfile(
            page=2,
            characters=MIN_CHARS_FOR_TEXT_LAYER * 10,
            images=1,
            recognised_text=True,
        )
        scan = PageProfile(page=3, characters=4, images=1)

        assert recognised.has_text_objects
        assert not scan.has_text_objects
        # Both still need OCR. The difference is what can be done meanwhile.
        assert recognised.needs_ocr and scan.needs_ocr


def _ruled_table_pdf(path) -> None:
    """A one-page PDF holding the shape a CIL statement's table has.

    Ruled, so pdfplumber finds it by its borders and the test is about the
    provenance stamp rather than about table detection.
    """
    table = [
        ["Subsidiary", "FY2023-24", "FY2024-25"],
        ["SECL", "167.00", "193.00"],
        ["MCL", "193.00", "210.50"],
    ]
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Coal production by subsidiary", fontsize=13)
    page.insert_text((72, 90), "(Figures in Million Tonnes)", fontsize=9)

    left, top, row_height, col_width = 72, 110, 22, 110
    for row_index, row in enumerate(table):
        for col_index, value in enumerate(row):
            page.insert_text(
                (left + col_index * col_width + 6, top + row_index * row_height + 15),
                value,
                fontsize=10,
            )
    for row_index in range(len(table) + 1):
        y = top + row_index * row_height
        page.draw_line((left, y), (left + len(table[0]) * col_width, y))
    for col_index in range(len(table[0]) + 1):
        x = left + col_index * col_width
        page.draw_line((x, top), (x, top + len(table) * row_height))

    document.save(path)
    document.close()


class TestCellsCarryTheirOwnProvenance:
    """A cell has to say where its glyphs came from, because its text cannot.

    ``167.00`` read off a typeset page and ``167.00`` read off someone's OCR layer
    are the same five characters. The only place the difference is known is the
    page classification, and it has to travel with the cell or it is lost.
    """

    def test_a_cell_off_a_recognised_page_is_stamped(self, tmp_path):
        path = tmp_path / "statement.pdf"
        _ruled_table_pdf(path)

        cells = list(digitize_pdf_tables(path, "doc_test", 1, recognised_pages={1}))

        assert cells
        assert all(cell["text_layer_recognised"] for cell in cells)
        # The grid survives being distrusted: this is the whole reason the flag
        # exists rather than the page simply being skipped.
        assert {cell["text"] for cell in cells} >= {"SECL", "167.00", "210.50"}

    def test_a_cell_off_a_native_page_is_not(self, tmp_path):
        """Same file, no page named. The default has to be "trustworthy", because
        it is the answer for every document that was never scanned."""
        path = tmp_path / "statement.pdf"
        _ruled_table_pdf(path)

        cells = list(digitize_pdf_tables(path, "doc_test", 1))

        assert cells
        assert not any(cell["text_layer_recognised"] for cell in cells)

    def test_only_the_named_pages_are_stamped(self, tmp_path):
        """A mixed PDF is the common case — one scanned annexure bound into a
        typeset report — so the flag is per page, not per document."""
        path = tmp_path / "statement.pdf"
        _ruled_table_pdf(path)

        cells = list(digitize_pdf_tables(path, "doc_test", 1, recognised_pages={7}))

        assert cells
        assert not any(cell["text_layer_recognised"] for cell in cells)


def _subsidiary_table(*, recognised: bool) -> list[dict[str, object]]:
    """The evidence rows for a plain subsidiary-by-year table."""
    table = [
        ["Subsidiary", "FY2023-24", "FY2024-25"],
        ["SECL", "167.00", "193.00"],
        ["MCL", "193.00", "210.50"],
    ]
    return [
        {
            "document_id": "doc_test",
            "document_version": 1,
            "page": 1,
            "kind": "table_cell",
            "table_id": "p1t1",
            "cell_ref": f"r{row}c{col}",
            "row_idx": row,
            "col_idx": col,
            "text": text,
            "extraction_method": ExtractionMethod.TABLE_LATTICE.value,
            "text_layer_recognised": recognised,
        }
        for row, line in enumerate(table)
        for col, text in enumerate(line)
    ]


class TestAReaderWithNothingToCheckAgainstRefuses:
    """The generic reader declines recognised cells, and says so.

    It answers three questions per cell from the row label and the column header
    and has no fourth number to test the figure against. On a page whose glyphs
    someone else recognised that is not enough: ``61 .2`` parses cleanly as 61 and
    loses two hundred thousand tonnes without a word, and there is nothing in the
    table that would contradict it.

    A **layout** reader does have that fourth number — the growth percentage
    printed beside every pair — and reads these same cells. See
    ``test_performance_statement.py``. The two behaviours are deliberate: what a
    reader may use depends on what it can check.
    """

    def _document(self) -> Document:
        return Document(
            document_id="doc_test",
            version=1,
            filename="report.pdf",
            title="Coal production by subsidiary",
            doc_class=DocumentClass.TEXT_PDF,
            publisher_entity_id="ent_cil",
            content_hash="0" * 64,
            size_bytes=1024,
            ingested_at=datetime.now(UTC),
        )

    def _read(self, *, recognised: bool):
        return extract_facts(
            self._document(),
            _subsidiary_table(recognised=recognised),
            page_lines={
                1: ["Coal production by subsidiary", "(Figures in Million Tonnes)"]
            },
        )

    def test_a_typeset_table_is_read(self):
        """The control. Every refusal below has to be attributable to provenance
        and to nothing else about this table."""
        report = self._read(recognised=False)
        assert len(report.facts) == 4
        assert not report.skipped

    def test_the_same_table_off_an_ocr_layer_is_declined(self):
        report = self._read(recognised=True)
        assert not report.facts
        assert report.skipped[SkipReason.UNVERIFIABLE_OCR_LAYER] == 4

    def test_the_refusal_is_counted_as_numbers_found_and_not_used(self):
        """The check runs *after* parsing, so the count means "4 figures were
        found here and declined" rather than "4 cells were passed over". A
        reviewer reading ``unverifiable_ocr_layer: 4`` on a document with no facts
        learns that the table was located and its contents are recoverable — a
        different and more tractable problem than no table at all.
        """
        report = self._read(recognised=True)
        examples = report.examples[SkipReason.UNVERIFIABLE_OCR_LAYER]
        assert examples == ["167.00", "193.00", "193.00", "210.50"]

    def test_the_refusal_is_one_a_reviewer_is_shown(self):
        """Table furniture is counted quietly; this is not furniture."""
        report = self._read(recognised=True)
        assert report.actionable_skips == 4
