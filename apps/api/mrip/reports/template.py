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

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from mrip.normalize.entities import canonical_id
from mrip.normalize.periods import UnknownPeriodError, normalize_period

__all__ = [
    "BUILTIN_TEMPLATES",
    "ReportTemplate",
    "TemplateField",
    "TemplateSection",
    "builtin_template",
    "default_template",
    "fill",
    "load_template",
    "normalize_context",
    "parliamentary_response_template",
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


def parliamentary_response_template() -> ReportTemplate:
    """The PS's named high-priority case: a reply to a parliamentary question.

    Three things differ from the production summary, and each is the reason this
    is a separate template rather than a flag on that one.

    **Offtake is required, not optional.** A PQ about production is almost always
    also about despatch, and a reply that silently omits it invites the follow-up
    question. If the figure is not in the corpus, this template fails rather than
    answering half the question.

    **The narrative section comes first.** A parliamentary answer opens with the
    position and supports it with figures; a management report opens with the
    table. The section order is the document's argument, so it is data here.

    **The evidence appendix is not optional.** A reply that is challenged in the
    House is defended by naming the document, version and page each figure came
    from, and that is exactly what the appendix prints.
    """
    return ReportTemplate(
        id="parliamentary-response",
        version=1,
        title="Parliamentary Response — {{entity}} {{period}}",
        required=[
            TemplateField(
                metric="coal_production", entity="{{entity}}", period="{{period}}"
            ),
            TemplateField(
                metric="coal_offtake", entity="{{entity}}", period="{{period}}"
            ),
        ],
        optional=[
            TemplateField(
                metric="overburden_removal", entity="{{entity}}", period="{{period}}"
            ),
        ],
        sections=[
            TemplateSection(
                kind="narrative",
                title="Reply",
                prompt=(
                    "State the production and offtake position for the period, "
                    "in the measured register of a reply to a parliamentary "
                    "question. Do not speculate on causes."
                ),
            ),
            TemplateSection(kind="figures", title="Figures referred to"),
            TemplateSection(kind="evidence_appendix", title="Sources"),
        ],
    )


#: The templates that ship with the system, by id. A template is data, so adding
#: one is adding an entry here or a YAML file — not a branch in the generator.
BUILTIN_TEMPLATES: dict[str, Callable[[], ReportTemplate]] = {
    "production-summary": default_template,
    "parliamentary-response": parliamentary_response_template,
}


def builtin_template(template_id: str) -> ReportTemplate:
    """One of the built-in templates, or raise ``KeyError`` naming what exists."""
    try:
        return BUILTIN_TEMPLATES[template_id]()
    except KeyError:
        known = ", ".join(sorted(BUILTIN_TEMPLATES))
        raise KeyError(
            f"no template {template_id!r}; built-in templates: {known}"
        ) from None


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
