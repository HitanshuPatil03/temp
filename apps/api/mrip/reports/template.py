"""Report templates — data, not code (ARCHITECTURE §11.1).

A template declares *what* a report contains — which figures are required, which
are optional, and the sections to render — as a versioned document that a
report-format owner can diff and review without touching Python. It never says
*how* to fetch a number; that is the generator's job, and it always goes to the
fact store.

Templates load from YAML. Field values may be literal (``entity: secl``) or a
``{{entity}}`` / ``{{period}}`` placeholder filled from the render context, so one
template serves every subsidiary and period.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from mrip.normalize.entities import canonical_id
from mrip.normalize.periods import UnknownPeriodError, normalize_period

__all__ = [
    "ReportTemplate",
    "TemplateField",
    "TemplateSection",
    "default_template",
    "fill",
    "load_template",
    "normalize_context",
]

SectionKind = Literal["figures", "chart", "narrative", "evidence_appendix"]


def fill(text: str, context: dict[str, str]) -> str:
    """Substitute ``{{key}}`` placeholders from the context, leaving the rest."""
    result = text
    for key, value in context.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


class TemplateField(BaseModel):
    """One measurement the report wants: a metric for an entity in a period."""

    metric: str
    entity: str
    period: str
    label: str | None = None

    def resolved_label(self, context: dict[str, str]) -> str:
        if self.label:
            return fill(self.label, context)
        entity = fill(self.entity, context)
        period = fill(self.period, context)
        return f"{entity} {self.metric} {period}"


class TemplateSection(BaseModel):
    """A block to render. ``narrative`` carries a prompt; the rest are structural."""

    kind: SectionKind
    title: str
    prompt: str | None = None


class ReportTemplate(BaseModel):
    """A versioned report definition. The manifest records which version rendered."""

    id: str
    version: int = Field(ge=1)
    title: str
    required: list[TemplateField] = Field(default_factory=list)
    optional: list[TemplateField] = Field(default_factory=list)
    sections: list[TemplateSection] = Field(default_factory=list)


def load_template(path: Path) -> ReportTemplate:
    """Load and validate a template from a YAML file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ReportTemplate.model_validate(data)


def default_template() -> ReportTemplate:
    """A built-in production-summary template, so a report can render with no file.

    Uses ``{{entity}}`` and ``{{period}}`` placeholders; the generator fills them
    from the render context. Coal production is required — a summary with no
    production figure is not the summary that was asked for — while offtake and
    despatch are optional and simply omitted when absent.
    """
    return ReportTemplate(
        id="production-summary",
        version=1,
        title="Production Summary — {{entity}} {{period}}",
        required=[
            TemplateField(
                metric="coal_production", entity="{{entity}}", period="{{period}}"
            ),
        ],
        optional=[
            TemplateField(
                metric="coal_offtake", entity="{{entity}}", period="{{period}}"
            ),
            TemplateField(
                metric="overburden_removal", entity="{{entity}}", period="{{period}}"
            ),
        ],
        sections=[
            TemplateSection(kind="figures", title="Key figures"),
            TemplateSection(
                kind="narrative",
                title="Summary",
                prompt="Summarise the production figures for the period.",
            ),
            TemplateSection(kind="evidence_appendix", title="Sources"),
        ],
    )


def normalize_context(*, entity: str, period: str) -> dict[str, str]:
    """Canonicalise the render context, or raise ``ValueError`` on a bad value.

    A template is only as trustworthy as the entity and period it is rendered for,
    so both are resolved through the normalizers up front rather than passed
    through raw.
    """
    entity_id = canonical_id(entity)
    if entity_id is None:
        raise ValueError(f"unknown entity {entity!r}")
    try:
        period_label = normalize_period(period).label
    except UnknownPeriodError as exc:
        raise ValueError(f"unknown period {period!r}") from exc
    return {"entity": entity_id, "period": period_label}
