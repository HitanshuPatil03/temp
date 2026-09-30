"""The report generator: resolve a template's figures, pinning each to evidence.

Every number comes from the fact store via :func:`~mrip.query.exact.resolve_figure`
— the *same* resolver the query path uses, so a figure in a report obeys the same
validated/conflict/unit discipline as a figure in an answer (ARCHITECTURE §11.2).
There is no model here and none is importable: figures are deterministic.

A required field with no validated fact does not blank and does not guess — it
lands in :attr:`~mrip.schemas.ReportManifest.missing_required`, which makes the
manifest incomplete and the render fail loudly with the field named.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mrip.db.repositories.documents import new_id
from mrip.normalize.periods import UnknownPeriodError, normalize_period
from mrip.query.exact import resolve_figure
from mrip.reports.template import ReportTemplate, TemplateField, fill, normalize_context
from mrip.schemas import (
    FigureDelta,
    MissingFigure,
    PinnedFigure,
    RefusalReason,
    ReportManifest,
)

if TYPE_CHECKING:
    from mrip.auth.scope import Scope
    from mrip.db.store import Store

__all__ = ["diff", "generate", "reproduce"]


def _resolve_field(
    store: Store, scope: Scope, field: TemplateField, context: dict[str, str]
) -> tuple[PinnedFigure | None, MissingFigure | None]:
    entity_id = fill(field.entity, context)
    label = field.resolved_label(context)
    period_text = fill(field.period, context)
    try:
        period = normalize_period(period_text)
    except UnknownPeriodError:
        return None, MissingFigure(
            label=label,
            entity_id=entity_id,
            metric=field.metric,
            period_label=period_text,
            reason=RefusalReason.OUT_OF_CORPUS,
            message=f"{period_text!r} is not a period this system recognises.",
        )

    resolution = resolve_figure(
        store,
        scope,
        entity_id=entity_id,
        metric=field.metric,
        period_label=period.label,
        fiscal_year=period.fiscal_year,
    )
    if resolution.fact is not None:
        fact = resolution.fact
        return (
            PinnedFigure(
                label=label,
                entity_id=entity_id,
                metric=field.metric,
                period_label=period.label,
                fact_id=fact.fact_id,
                value=fact.value,
                unit=fact.unit,
                raw_value=fact.raw_value,
                raw_unit=fact.raw_unit,
                document_id=fact.evidence.document_id,
                document_version=fact.evidence.document_version,
                locator=fact.evidence.locator,
            ),
            None,
        )

    assert resolution.reason is not None  # ok is False ⇒ a reason is set
    return None, MissingFigure(
        label=label,
        entity_id=entity_id,
        metric=field.metric,
        period_label=period.label,
        reason=resolution.reason,
        message=resolution.message,
    )


def generate(
    store: Store,
    scope: Scope,
    template: ReportTemplate,
    *,
    entity: str,
    period: str,
    report_id: str | None = None,
) -> ReportManifest:
    """Render a template for one entity and period into a pinned-evidence manifest.

    Raises ``ValueError`` if the entity or period is unrecognised. Otherwise always
    returns a manifest — an incomplete one (``missing_required`` non-empty) when a
    required figure could not be pinned, so the caller decides how to fail.
    """
    context = normalize_context(entity=entity, period=period)

    figures: list[PinnedFigure] = []
    missing_required: list[MissingFigure] = []
    missing_optional: list[MissingFigure] = []

    for field, required in (
        *((item, True) for item in template.required),
        *((item, False) for item in template.optional),
    ):
        pinned, missing = _resolve_field(store, scope, field, context)
        if pinned is not None:
            figures.append(pinned)
        elif required:
            assert missing is not None
            missing_required.append(missing)
        else:
            assert missing is not None
            missing_optional.append(missing)

    return ReportManifest(
        report_id=report_id or new_id("rpt"),
        template_id=template.id,
        template_version=template.version,
        title=fill(template.title, context),
        generated_at=datetime.now(UTC),
        figures=figures,
        missing_required=missing_required,
        missing_optional=missing_optional,
    )


def reproduce(manifest: ReportManifest) -> list[PinnedFigure]:
    """The figures a published manifest reproduces — from the manifest alone.

    Reproduction needs no store: the manifest pinned each figure's value and its
    ``document@version`` at approval, so a two-year-old report re-renders exactly
    as approved even after every source has been revised (§11.3).
    """
    return list(manifest.figures)


def diff(store: Store, scope: Scope, manifest: ReportManifest) -> list[FigureDelta]:
    """Compare a manifest's approved figures against what the corpus says now.

    For each pinned figure, re-resolve the current validated figure for the same
    measurement. A changed value, a newer source version, or a figure that no
    longer resolves (superseded with no validated replacement, or newly
    conflicted) all surface as ``changed`` deltas — the list of what moved and
    what moved it (§11.3).
    """
    deltas: list[FigureDelta] = []
    for figure in manifest.figures:
        period = normalize_period(figure.period_label)
        resolution = resolve_figure(
            store,
            scope,
            entity_id=figure.entity_id,
            metric=figure.metric,
            period_label=period.label,
            fiscal_year=period.fiscal_year,
        )
        if resolution.fact is None:
            deltas.append(
                FigureDelta(
                    label=figure.label,
                    entity_id=figure.entity_id,
                    metric=figure.metric,
                    period_label=figure.period_label,
                    approved_value=figure.value,
                    approved_document_version=figure.document_version,
                    changed=True,
                    note=f"no longer resolves: {resolution.message}",
                )
            )
            continue

        current = resolution.fact
        changed = (
            round(current.value, 6) != round(figure.value, 6)
            or current.evidence.document_version != figure.document_version
        )
        note = ""
        if current.evidence.document_version != figure.document_version:
            note = (
                f"source moved v{figure.document_version} → "
                f"v{current.evidence.document_version}"
            )
        elif changed:
            note = "value changed"
        deltas.append(
            FigureDelta(
                label=figure.label,
                entity_id=figure.entity_id,
                metric=figure.metric,
                period_label=figure.period_label,
                approved_value=figure.value,
                approved_document_version=figure.document_version,
                current_value=current.value,
                current_document_version=current.evidence.document_version,
                changed=changed,
                note=note,
            )
        )
    return deltas
