"""The exact-figure path: a single validated number, answered by SQL over facts.

This is the heart of ARCHITECTURE §7. There is **no model client in scope here**
— not that it goes uncalled, but that it is not importable, and the architecture
test in ``tests/test_architecture.py`` fails CI if that ever changes. The answer
is a stored :class:`~mrip.schemas.Fact`, which carries its own evidence; this path
never computes, averages or invents a value.

Every way of *not* being able to answer is a structured refusal (§13.5), carrying
the evidence that caused it, so the caller learns why — "these two documents
disagree and no one has adjudicated" is a true answer, not an error.

:func:`resolve_figure` is the shared core: it maps ``(entity, metric, period)`` to
one validated fact or one refusal reason, and is reused verbatim by the report
generator (§11.2), so a figure in a report obeys exactly the same discipline as a
figure in a query — no second implementation to drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mrip.schemas import (
    ConflictGroup,
    Fact,
    FigureAnswer,
    QueryIntent,
    QueryResponse,
    Refusal,
    RefusalReason,
)

if TYPE_CHECKING:
    from mrip.auth.scope import Scope
    from mrip.db.store import Store
    from mrip.query.router import RoutedQuery

__all__ = ["FigureResolution", "answer_figure", "resolve_figure"]


@dataclass(frozen=True, slots=True)
class FigureResolution:
    """The outcome of resolving one measurement to a single figure.

    Exactly one of :attr:`fact` (the validated answer) or :attr:`reason` (why not)
    is set. The evidence behind a refusal travels with it, as everywhere else.
    """

    fact: Fact | None = None
    reason: RefusalReason | None = None
    message: str = ""
    conflict: ConflictGroup | None = None
    candidates: list[Fact] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.fact is not None


def resolve_figure(
    store: Store,
    scope: Scope,
    *,
    entity_id: str,
    metric: str,
    period_label: str,
    fiscal_year: str | None,
) -> FigureResolution:
    """Resolve one ``(entity, metric, period)`` to a validated fact or a refusal.

    The rules, in order: the measurement must exist in the corpus, carry no open
    conflict, sit in a single unambiguous unit, be validated, and have one
    distinct validated value. Any failure is a reason, never a guess.
    """
    candidates = store.facts.query(
        scope, entity_id=entity_id, metric=metric, fiscal_year=fiscal_year
    )
    matched = [fact for fact in candidates if fact.period_label == period_label]

    if not matched:
        anywhere = store.facts.query(
            scope, entity_id=entity_id, metric=metric, include_inactive=True, limit=1
        )
        if not anywhere:
            return FigureResolution(
                reason=RefusalReason.OUT_OF_CORPUS,
                message=f"No {metric} figures for {entity_id} are in your corpus.",
            )
        return FigureResolution(
            reason=RefusalReason.NO_VALIDATED_FACT,
            message=f"No figure for {entity_id} {metric} {period_label} is recorded.",
        )

    for group in store.conflicts.open(scope):
        if (
            group.entity_id == entity_id
            and group.metric == metric
            and group.period_label == period_label
        ):
            return FigureResolution(
                reason=RefusalReason.OPEN_CONFLICT,
                message="Two or more sources disagree on this figure and no "
                "reviewer has chosen between them. The system will not pick.",
                conflict=group,
            )

    units = {fact.unit for fact in matched}
    if len(units) > 1 or any(fact.unit_ambiguous for fact in matched):
        return FigureResolution(
            reason=RefusalReason.AMBIGUOUS_UNIT,
            message="The recorded figures are in more than one unit, so a single "
            "value cannot be given without choosing a unit.",
            candidates=matched,
        )

    validated = [fact for fact in matched if fact.status.is_trustworthy]
    if not validated:
        return FigureResolution(
            reason=RefusalReason.NO_VALIDATED_FACT,
            message=f"A figure for {entity_id} {metric} {period_label} exists but "
            "has not been validated, so it is not offered as an answer.",
            candidates=matched,
        )

    distinct_values = {round(fact.value, 6) for fact in validated}
    if len(distinct_values) > 1:
        return FigureResolution(
            reason=RefusalReason.OPEN_CONFLICT,
            message="Validated figures for this measurement disagree. This needs "
            "adjudication before a single value can be returned.",
            candidates=validated,
        )

    return FigureResolution(fact=validated[0])


def answer_figure(store: Store, scope: Scope, routed: RoutedQuery) -> QueryResponse:
    """Answer a single-figure question, or refuse with the reason and evidence."""
    slots = routed.slots
    entity, metric, period = slots.entity_id, slots.metric_key, slots.period
    if not (entity and metric and period is not None):
        # The router only sends fully-specified questions here; if one slips
        # through, decline conservatively rather than guessing.
        return QueryResponse(
            question=routed.question,
            intent=routed.intent,
            model_used=False,
            refusal=Refusal(
                reason=RefusalReason.OUT_OF_CORPUS,
                message="The question does not name an entity, metric and period "
                "precisely enough to pin a single figure.",
            ),
        )

    resolution = resolve_figure(
        store,
        scope,
        entity_id=entity,
        metric=metric,
        period_label=period.label,
        fiscal_year=period.fiscal_year,
    )
    if resolution.fact is not None:
        return QueryResponse(
            question=routed.question,
            intent=QueryIntent.EXACT_FIGURE,
            model_used=False,
            figure=FigureAnswer(fact=resolution.fact),
        )

    assert resolution.reason is not None  # ok is False ⇒ a reason is set
    return QueryResponse(
        question=routed.question,
        intent=routed.intent,
        model_used=False,
        refusal=Refusal(
            reason=resolution.reason,
            message=resolution.message,
            conflict=resolution.conflict,
            facts=resolution.candidates,
        ),
    )
