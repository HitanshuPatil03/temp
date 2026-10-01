"""Narrative sections for reports (ARCHITECTURE §11.2, §13.4).

This module exists so that :mod:`mrip.reports.render` and
:mod:`mrip.reports.writers` can stay on the figure path. They lay out prose; this
is the only part of the report pipeline that *produces* it, and it is therefore
the only part that may import a model client. ``tests/test_architecture.py``
enforces the split — adding a model call to a writer fails the build.

What the model is handed is deliberately narrow: the figures the deterministic
layer already pinned, and nothing else. It never sees the fact store, so it
cannot introduce a figure, and every numeral it writes is checked against those
pinned values by the *same* verifier the query path uses
(:func:`mrip.query.narrative.verify_numbers`). A sentence carrying a number from
nowhere is dropped and the section flagged.

The reason that matters more here than on the query path: a parliamentary answer
with a plausible invented figure is the worst output this system could produce.
A query's prose is read by one officer who can see the citations beside it; a
report is signed and sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mrip.llm import LLMClient, LLMUnavailableError
from mrip.query.narrative import verify_numbers

if TYPE_CHECKING:
    from mrip.reports.template import ReportTemplate
    from mrip.schemas import PinnedFigure, ReportManifest

__all__ = ["NarrativeResult", "narrate"]

_SYSTEM = (
    "You are drafting a section of an official Ministry of Coal report. Write "
    "only from the FIGURES provided. Every number in your text must appear "
    "verbatim in those figures — never estimate, convert, round, or introduce a "
    "figure. Do not add citations. Two or three sentences, formal and factual."
)


class NarrativeResult:
    """Prose per section title, plus which sections had a sentence dropped.

    ``flagged`` is a set rather than a boolean because a report may have several
    narrative sections and the reviewer needs to know *which* one was edited,
    not merely that something was.
    """

    __slots__ = ("flagged", "sections")

    def __init__(self, sections: dict[str, str], flagged: set[str]) -> None:
        self.sections = sections
        self.flagged = flagged

    @property
    def any_flagged(self) -> bool:
        return bool(self.flagged)


def _allowed(figures: list[PinnedFigure]) -> list[float]:
    """Every number the prose is permitted to contain.

    Both the canonical and the printed value, because either is a legitimate way
    to state the same figure and the model was shown both. Nothing else: a
    percentage the model computed from two of these is not in this list, and will
    be dropped — which is the intended behaviour. Arithmetic is the
    deterministic layer's job.
    """
    allowed: list[float] = []
    for figure in figures:
        allowed.extend((figure.value, figure.raw_value))
    return allowed


def _prompt(prompt: str, figures: list[PinnedFigure]) -> str:
    lines = ["FIGURES:"]
    if figures:
        lines.extend(
            f"- {figure.label}: {figure.raw_value:g} {figure.raw_unit} "
            f"({figure.value:,.0f} {figure.unit}) [{figure.locator}]"
            for figure in figures
        )
    else:
        lines.append("- (none)")
    lines.append(f"\nSECTION: {prompt}")
    return "\n".join(lines)


def narrate(
    template: ReportTemplate, manifest: ReportManifest, llm: LLMClient
) -> NarrativeResult:
    """Write and verify every narrative section of a report.

    Returns empty sections when the model is unavailable, which the writers
    render as a stated note (§7) — a report without prose is still a report with
    every figure, table and source in it.
    """
    if not llm.available:
        return NarrativeResult({}, set())

    allowed = _allowed(manifest.figures)
    sections: dict[str, str] = {}
    flagged: set[str] = set()

    for section in template.sections:
        if section.kind != "narrative" or not section.prompt:
            continue
        try:
            raw = llm.generate(_prompt(section.prompt, manifest.figures), system=_SYSTEM)
        except LLMUnavailableError:
            # The runtime went away mid-render. Every figure is already pinned,
            # so the report is still publishable without this section's prose.
            break
        cleaned, was_flagged = verify_numbers(raw, allowed)
        if cleaned:
            sections[section.title] = cleaned
        if was_flagged:
            flagged.add(section.title)

    return NarrativeResult(sections, flagged)
