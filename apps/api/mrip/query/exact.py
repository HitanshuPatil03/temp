"""The exact-figure path: a single validated number, answered by SQL over facts.

This is the heart of ARCHITECTURE §7. There is **no model client in scope here**
— not that it goes uncalled, but that it is not importable, and the architecture
test in ``tests/test_architecture.py`` fails CI if that ever changes. The answer
is a stored :class:`~mrip.schemas.Fact`, which carries its own evidence; this path
never computes, averages or invents a value.

Every way of *not* being able to answer is a structured refusal (§13.5), carrying
the evidence that caused it, so the caller learns why — "these two documents
disagree and no one has adjudicated" is a true answer, not an error.
"""

from __future__ import annotations

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

__all__ = ["answer_figure"]


def _refuse(
    routed: RoutedQuery,
    reason: RefusalReason,
    message: str,
    *,
    conflict: ConflictGroup | None = None,
    facts: list[Fact] | None = None,
) -> QueryResponse:
    return QueryResponse(
        question=routed.question,
        intent=routed.intent,
        model_used=False,
        refusal=Refusal(
            reason=reason, message=message, conflict=conflict, facts=facts or []
        ),
    )


def answer_figure(store: Store, scope: Scope, routed: RoutedQuery) -> QueryResponse:
    """Answer a single-figure question, or refuse with the reason and evidence."""
    slots = routed.slots
    entity, metric, period = slots.entity_id, slots.metric_key, slots.period
    if not (entity and metric and period is not None):
        # The router only sends fully-specified questions here; if one slips
        # through, decline conservatively rather than guessing.
        return _refuse(
            routed,
            RefusalReason.OUT_OF_CORPUS,
            "The question does not name an entity, metric and period precisely "
            "enough to pin a single figure.",
        )

    candidates = store.facts.query(
        scope, entity_id=entity, metric=metric, fiscal_year=period.fiscal_year
    )
    matched = [fact for fact in candidates if fact.period_label == period.label]

    if not matched:
        anywhere = store.facts.query(
            scope, entity_id=entity, metric=metric, include_inactive=True, limit=1
        )
        if not anywhere:
            return _refuse(
                routed,
                RefusalReason.OUT_OF_CORPUS,
                f"No {metric} figures for {entity} are in your corpus.",
            )
        return _refuse(
            routed,
            RefusalReason.NO_VALIDATED_FACT,
            f"No figure for {entity} {metric} {period.label} has been recorded.",
        )

    for group in store.conflicts.open(scope):
        if (
            group.entity_id == entity
            and group.metric == metric
            and group.period_label == period.label
        ):
            return _refuse(
                routed,
                RefusalReason.OPEN_CONFLICT,
                "Two or more sources disagree on this figure and no reviewer has "
                "chosen between them. Both are shown; the system will not pick.",
                conflict=group,
            )

    units = {fact.unit for fact in matched}
    if len(units) > 1 or any(fact.unit_ambiguous for fact in matched):
        return _refuse(
            routed,
            RefusalReason.AMBIGUOUS_UNIT,
            "The recorded figures are in more than one unit, so a single value "
            "cannot be given without choosing a unit.",
            facts=matched,
        )

    validated = [fact for fact in matched if fact.status.is_trustworthy]
    if not validated:
        return _refuse(
            routed,
            RefusalReason.NO_VALIDATED_FACT,
            f"A figure for {entity} {metric} {period.label} exists but has not "
            "been validated, so it is not offered as an answer.",
            facts=matched,
        )

    distinct_values = {round(fact.value, 6) for fact in validated}
    if len(distinct_values) > 1:
        return _refuse(
            routed,
            RefusalReason.OPEN_CONFLICT,
            "Validated figures for this measurement disagree. This needs "
            "adjudication before a single value can be returned.",
            facts=validated,
        )

    return QueryResponse(
        question=routed.question,
        intent=QueryIntent.EXACT_FIGURE,
        model_used=False,
        figure=FigureAnswer(fact=validated[0]),
    )
