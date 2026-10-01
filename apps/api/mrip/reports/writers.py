"""Report writers: ``.docx``, ``.xlsx`` and ``.pptx`` (ARCHITECTURE §11.4).

All three are pure Python and work offline — no converter binary, no headless
browser, nothing that needs the network. A government host that cannot reach the
internet must still produce a file that can be mailed to the Ministry.

This module is on the **figure path**: ``tests/test_architecture.py`` forbids it
from importing a model client, and it never fetches a number. It lays out figures
that :mod:`mrip.reports.generate` already pinned and prose that was already
produced and already verified. Like :mod:`mrip.reports.render`, it refuses an
incomplete manifest rather than emitting a document with a blank where a required
figure should be.

The three formats carry the same content, shaped for how each is actually used:
Word is the document that gets signed, Excel is the one whose figures get
re-sorted and pasted into something else, and PowerPoint is the one shown in a
meeting. Every one of them carries the evidence appendix, because a figure
without its source is the thing this project exists to prevent.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from mrip.reports.render import ReportIncompleteError

if TYPE_CHECKING:
    from mrip.reports.template import ReportTemplate
    from mrip.schemas import PinnedFigure, ReportManifest

__all__ = ["CONTENT_TYPES", "render_docx", "render_pptx", "render_xlsx"]

#: What a narrative section says when the model was disabled. The Markdown
#: renderer wraps its own copy in italic markers; these formats carry styling out
#: of band, so the text is kept plain here rather than importing a string with
#: another syntax's punctuation baked into it (§7).
MODEL_DISABLED = "Narrative unavailable — the local model is disabled."

#: MIME types for the three Office formats, plus Markdown. Kept here next to the
#: writers so a new format cannot be added without declaring how it is served.
CONTENT_TYPES: dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}

_FOOTER = "Generated {at} from template {template} v{version}."
_PROVENANCE = (
    "Every figure in this report is pinned to the fact and the document version "
    "it came from. The locator column is that citation."
)


def _guard(manifest: ReportManifest) -> None:
    """Refuse an incomplete manifest, naming what is missing.

    The same guard :func:`mrip.reports.render.render_markdown` applies, repeated
    rather than shared through a decorator so that each writer's first statement
    says what it will not do.
    """
    if not manifest.complete:
        missing = ", ".join(item.label for item in manifest.missing_required)
        raise ReportIncompleteError(f"required figures could not be pinned: {missing}")


def _value(figure: PinnedFigure) -> str:
    return f"{figure.value:,.0f} {figure.unit}"


def _raw(figure: PinnedFigure) -> str:
    return f"{figure.raw_value:g} {figure.raw_unit}"


def _sources(figures: list[PinnedFigure]) -> list[tuple[str, int, str]]:
    """One row per distinct source version, in a stable order.

    Deduplicated so the appendix lists documents rather than repeating one
    citation per figure that came from the same page.
    """
    seen: dict[tuple[str, int], str] = {}
    for figure in figures:
        seen.setdefault((figure.document_id, figure.document_version), figure.locator)
    return [(doc, version, loc) for (doc, version), loc in sorted(seen.items())]


def _narrative(section_title: str, narratives: dict[str, str] | None) -> str:
    """The prose for a section, or the stated note when the model was off.

    §7: with ``MRIP_LLM_ENABLED=false`` a report still renders every figure,
    table and source — what is lost is prose, and it is *replaced by a sentence
    saying so* rather than silently omitted.
    """
    return (narratives or {}).get(section_title) or MODEL_DISABLED


# ------------------------------------------------------------------------ docx


def render_docx(
    template: ReportTemplate,
    manifest: ReportManifest,
    *,
    narratives: dict[str, str] | None = None,
) -> bytes:
    """Render to Word — the document that gets signed."""
    _guard(manifest)
    from docx import Document as DocxDocument
    from docx.shared import Pt

    document = DocxDocument()
    document.add_heading(manifest.title, level=0)
    note = document.add_paragraph(_PROVENANCE)
    note.runs[0].font.size = Pt(9)
    note.runs[0].font.italic = True

    for section in template.sections:
        document.add_heading(section.title, level=1)
        if section.kind in ("figures", "chart"):
            if not manifest.figures:
                document.add_paragraph("No figures in scope.")
                continue
            table = document.add_table(rows=1, cols=4)
            table.style = "Light Grid Accent 1"
            for cell, label in zip(
                table.rows[0].cells,
                ("Figure", "Value", "As printed", "Source"),
                strict=True,
            ):
                cell.text = label
            for figure in manifest.figures:
                row = table.add_row().cells
                row[0].text = figure.label
                row[1].text = _value(figure)
                row[2].text = _raw(figure)
                row[3].text = figure.locator
        elif section.kind == "narrative":
            document.add_paragraph(_narrative(section.title, narratives))
        elif section.kind == "evidence_appendix":
            sources = _sources(manifest.figures)
            if not sources:
                document.add_paragraph("No sources cited.")
                continue
            table = document.add_table(rows=1, cols=3)
            table.style = "Light Grid Accent 1"
            for cell, label in zip(
                table.rows[0].cells,
                ("Document", "Version", "First locator"),
                strict=True,
            ):
                cell.text = label
            for doc_id, version, locator in sources:
                row = table.add_row().cells
                row[0].text = doc_id
                row[1].text = f"v{version}"
                row[2].text = locator

    if manifest.missing_optional:
        document.add_heading("Omitted (optional, not in corpus)", level=1)
        for item in manifest.missing_optional:
            document.add_paragraph(
                f"{item.label}: {item.reason.value}", style="List Bullet"
            )

    footer = document.add_paragraph(
        _FOOTER.format(
            at=manifest.generated_at.isoformat(),
            template=manifest.template_id,
            version=manifest.template_version,
        )
    )
    footer.runs[0].font.size = Pt(8)
    footer.runs[0].font.italic = True

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------------ xlsx


def render_xlsx(
    template: ReportTemplate,
    manifest: ReportManifest,
    *,
    narratives: dict[str, str] | None = None,
) -> bytes:
    """Render to Excel — the one whose figures get re-sorted and reused.

    Two sheets rather than one. The canonical value and the value the document
    literally printed are both present and in separate columns, because a
    spreadsheet is where someone will sum a column, and summing a mix of tonnes
    and million-tonnes is exactly the error the fact store exists to prevent.
    """
    _guard(manifest)
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    figures_sheet = workbook.active
    assert figures_sheet is not None
    figures_sheet.title = "Figures"

    bold = Font(bold=True)
    headers = (
        "Figure",
        "Entity",
        "Metric",
        "Period",
        "Value",
        "Unit",
        "As printed",
        "Printed unit",
        "Fact id",
        "Document",
        "Version",
        "Locator",
    )
    figures_sheet.append(list(headers))
    for cell in figures_sheet[1]:
        cell.font = bold

    for figure in manifest.figures:
        figures_sheet.append(
            [
                figure.label,
                figure.entity_id,
                figure.metric,
                figure.period_label,
                figure.value,
                figure.unit,
                figure.raw_value,
                figure.raw_unit,
                figure.fact_id,
                figure.document_id,
                figure.document_version,
                figure.locator,
            ]
        )

    # Widths sized to the header, which is the shortest sensible column. Nobody
    # should have to drag a column to read a citation.
    for column, header in enumerate(headers, start=1):
        letter = figures_sheet.cell(row=1, column=column).column_letter
        figures_sheet.column_dimensions[letter].width = max(len(header) + 4, 14)
    figures_sheet.freeze_panes = "A2"

    sources_sheet = workbook.create_sheet("Sources")
    sources_sheet.append(["Document", "Version", "First locator"])
    for cell in sources_sheet[1]:
        cell.font = bold
    for doc_id, version, locator in _sources(manifest.figures):
        sources_sheet.append([doc_id, f"v{version}", locator])
    for column, header in enumerate(("Document", "Version", "First locator"), start=1):
        letter = sources_sheet.cell(row=1, column=column).column_letter
        sources_sheet.column_dimensions[letter].width = max(len(header) + 4, 18)

    about = workbook.create_sheet("About")
    about.append(["Report", manifest.title])
    about.append(["Template", f"{manifest.template_id} v{manifest.template_version}"])
    about.append(["Generated", manifest.generated_at.isoformat()])
    about.append(["State", manifest.state.value])
    about.append(["Provenance", _PROVENANCE])
    for section in template.sections:
        if section.kind == "narrative":
            about.append([section.title, _narrative(section.title, narratives)])
    for item in manifest.missing_optional:
        about.append([f"Omitted — {item.label}", item.reason.value])
    about.column_dimensions["A"].width = 26
    about.column_dimensions["B"].width = 90

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------------ pptx


def render_pptx(
    template: ReportTemplate,
    manifest: ReportManifest,
    *,
    narratives: dict[str, str] | None = None,
) -> bytes:
    """Render to PowerPoint — the one shown in a meeting.

    The figures slide carries the locator column like every other format. A deck
    is where a number is most likely to be quoted without its source, so this is
    the format where the citation matters most, not least.
    """
    _guard(manifest)
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    blank = presentation.slide_layouts[6]
    title_layout = presentation.slide_layouts[0]

    opening = presentation.slides.add_slide(title_layout)
    opening.shapes.title.text = manifest.title
    opening.placeholders[1].text = (
        f"{manifest.template_id} v{manifest.template_version} · "
        f"{manifest.generated_at.date().isoformat()}"
    )

    for section in template.sections:
        slide = presentation.slides.add_slide(blank)
        heading = slide.shapes.add_textbox(
            Inches(0.5), Inches(0.35), Inches(9.0), Inches(0.8)
        )
        heading.text_frame.text = section.title
        heading.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
        heading.text_frame.paragraphs[0].runs[0].font.bold = True

        if section.kind in ("figures", "chart"):
            if not manifest.figures:
                _text_slide(slide, "No figures in scope.")
                continue
            rows = len(manifest.figures) + 1
            table = slide.shapes.add_table(
                rows, 3, Inches(0.5), Inches(1.3), Inches(9.0), Inches(0.4 * rows)
            ).table
            for index, label in enumerate(("Figure", "Value", "Source")):
                table.cell(0, index).text = label
            for row, figure in enumerate(manifest.figures, start=1):
                table.cell(row, 0).text = figure.label
                table.cell(row, 1).text = _value(figure)
                table.cell(row, 2).text = figure.locator
        elif section.kind == "narrative":
            _text_slide(slide, _narrative(section.title, narratives))
        elif section.kind == "evidence_appendix":
            sources = _sources(manifest.figures)
            if not sources:
                _text_slide(slide, "No sources cited.")
                continue
            lines = [f"{doc} v{version} — {loc}" for doc, version, loc in sources]
            _text_slide(slide, "\n".join(lines))

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _text_slide(slide: Any, text: str) -> None:
    """Body text on a blank slide, wrapped.

    python-pptx is imported here rather than at module scope so that importing
    this module does not require it — the docx and xlsx writers must stay usable
    on a host that installed neither.
    """
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.3), Inches(9.0), Inches(4.5))
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = text
    for paragraph in frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(14)
