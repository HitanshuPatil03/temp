"""Rendering a report manifest to a document (ARCHITECTURE §11.4).

This module is on the **figure path**: ``tests/test_architecture.py`` forbids it
from importing a model client. It only lays out figures that were already pinned
and prose that was already produced — it never fetches a number and never calls a
model. Narrative text is passed in as strings; when the model was disabled, the
caller passes nothing and the section renders a plain note saying so (§7).

Markdown is the first target: dependency-free, diffable, and enough to prove the
pipeline end to end. The ``.docx`` / ``.xlsx`` / ``.pptx`` writers (§11.4) layer
on top of the same manifest without changing how figures are pinned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mrip.reports.template import ReportTemplate
    from mrip.schemas import PinnedFigure, ReportManifest

__all__ = ["ReportIncompleteError", "render_markdown"]

_MODEL_DISABLED = "_(Narrative unavailable — the local model is disabled.)_"


class ReportIncompleteError(RuntimeError):
    """A required figure could not be pinned, so the report will not render.

    Carries the missing field labels: §11.2 forbids emitting a report with a
    blank where a required number should be.
    """


def _format_value(figure: PinnedFigure) -> str:
    return (
        f"{figure.value:,.0f} {figure.unit} (raw {figure.raw_value:g} {figure.raw_unit})"
    )


def _figures_table(figures: list[PinnedFigure]) -> list[str]:
    if not figures:
        return ["_No figures in scope._"]
    lines = ["| Figure | Value | Source |", "| --- | --- | --- |"]
    lines.extend(
        f"| {fig.label} | {_format_value(fig)} | `{fig.locator}` |" for fig in figures
    )
    return lines


def _evidence_appendix(figures: list[PinnedFigure]) -> list[str]:
    # One row per distinct source version, so the appendix lists documents, not
    # repeated citations of the same one.
    seen: dict[tuple[str, int], str] = {}
    for fig in figures:
        seen.setdefault((fig.document_id, fig.document_version), fig.locator)
    if not seen:
        return ["_No sources cited._"]
    lines = ["| Document | Version | First locator |", "| --- | --- | --- |"]
    lines.extend(
        f"| {doc} | v{version} | `{locator}` |"
        for (doc, version), locator in sorted(seen.items())
    )
    return lines


def render_markdown(
    template: ReportTemplate,
    manifest: ReportManifest,
    *,
    narratives: dict[str, str] | None = None,
) -> str:
    """Render a complete manifest to Markdown, or raise if it is incomplete."""
    if not manifest.complete:
        missing = ", ".join(item.label for item in manifest.missing_required)
        raise ReportIncompleteError(f"required figures could not be pinned: {missing}")

    narratives = narratives or {}
    out: list[str] = [f"# {manifest.title}", ""]

    for section in template.sections:
        out.append(f"## {section.title}")
        out.append("")
        if section.kind in ("figures", "chart"):
            out.extend(_figures_table(manifest.figures))
        elif section.kind == "narrative":
            out.append(narratives.get(section.title, _MODEL_DISABLED))
        elif section.kind == "evidence_appendix":
            out.extend(_evidence_appendix(manifest.figures))
        out.append("")

    if manifest.missing_optional:
        out.append("## Omitted (optional, not in corpus)")
        out.append("")
        out.extend(
            f"- {item.label}: {item.reason.value}" for item in manifest.missing_optional
        )
        out.append("")

    footer = (
        f"_Generated {manifest.generated_at.isoformat()} from template "
        f"{manifest.template_id} v{manifest.template_version}._"
    )
    out.append(footer)
    return "\n".join(out)
