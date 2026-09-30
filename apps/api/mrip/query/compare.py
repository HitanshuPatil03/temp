"""The comparison path: a metric across entities and fiscal years.

Also on the figure path (ARCHITECTURE §7) — no model client, no arithmetic beyond
the ``SUM`` the fact store itself computes. It refuses to place a calendar-year
figure on a fiscal-year axis, which is the comparison the period normalizer
declines to make; that refusal lives in the repository query, not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mrip.schemas import (
    ComparisonAnswer,
    QueryIntent,
    QueryResponse,
    Refusal,
    RefusalReason,
    SeriesPoint,
)

if TYPE_CHECKING:
    from mrip.auth.scope import Scope
    from mrip.db.store import Store
    from mrip.query.router import RoutedQuery

__all__ = ["answer_series"]


def answer_series(store: Store, scope: Scope, routed: RoutedQuery) -> QueryResponse:
    """Answer a comparison/trend question as a per-entity, per-year series."""
    metric = routed.slots.metric_key
    if not metric:
        return QueryResponse(
            question=routed.question,
            intent=routed.intent,
            model_used=False,
            refusal=Refusal(
                reason=RefusalReason.OUT_OF_CORPUS,
                message="The question does not name a metric to compare.",
            ),
        )

    rows = store.entity_metric_series(metric, scope)

    # If the question named one entity, narrow to it — the series is then that
    # entity's trend over years rather than a cross-entity comparison.
    entity = routed.slots.entity_id
    if entity is not None:
        rows = [row for row in rows if row["entity_id"] == entity]

    if not rows:
        return QueryResponse(
            question=routed.question,
            intent=routed.intent,
            model_used=False,
            refusal=Refusal(
                reason=RefusalReason.OUT_OF_CORPUS,
                message=f"No fiscal-year {metric} figures are in your corpus"
                + (f" for {entity}." if entity else "."),
            ),
        )

    points = [
        SeriesPoint(
            entity_id=row["entity_id"],
            fiscal_year=row["fiscal_year"],
            value=float(row["value"]),
            unit=row["unit"],
            fact_count=int(row["fact_count"]),
        )
        for row in rows
    ]
    units = {point.unit for point in points}
    return QueryResponse(
        question=routed.question,
        intent=QueryIntent.COMPARISON,
        model_used=False,
        comparison=ComparisonAnswer(
            metric=metric,
            unit=next(iter(units)) if len(units) == 1 else None,
            points=points,
        ),
    )
