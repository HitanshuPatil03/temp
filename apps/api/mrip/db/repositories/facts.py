"""Fact repository.

Two SQL details here carry the product's argument.

**The active predicate is built from the enum, not hardcoded.** Renaming a fact
status cannot silently un-filter a query, because the ``NOT IN`` list is derived
from :class:`~mrip.schemas.FactStatus` at import time.

**The review queue is driven by the *limiting* stage.** PostgreSQL's ``LEAST``
ignores nulls, so ``LEAST(conf_ocr, conf_parse, conf_answer)`` is exactly the
minimum over the stages that actually ran — and a fact where no stage scored is
left alone rather than treated as zero-confidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from mrip.auth.scope import Scope
from mrip.db.mappers import fact_to_row, row_to_fact
from mrip.db.tables import documents, facts
from mrip.schemas import Fact, FactStatus

__all__ = ["INACTIVE_STATES", "LIMITING_CONFIDENCE", "FactRepository"]

#: Fact states excluded from analytics, reports and conflict detection. Neither
#: is deleted — a rejected figure stays on the record with its reason.
INACTIVE_STATES: tuple[FactStatus, ...] = (FactStatus.REJECTED, FactStatus.SUPERSEDED)

#: The honest headline confidence: the weakest stage that ran.
LIMITING_CONFIDENCE = sa.func.least(
    facts.c.conf_ocr, facts.c.conf_parse, facts.c.conf_answer
)


def _active() -> sa.ColumnElement[bool]:
    return facts.c.status.not_in([state.value for state in INACTIVE_STATES])


class FactRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # --------------------------------------------------------------- writes

    def insert(self, batch: Sequence[Fact]) -> int:
        if not batch:
            return 0
        self._conn.execute(sa.insert(facts), [fact_to_row(fact) for fact in batch])
        return len(batch)

    def set_status(self, fact_ids: Sequence[str], status: FactStatus) -> int:
        if not fact_ids:
            return 0
        result = self._conn.execute(
            sa.update(facts)
            .where(facts.c.fact_id.in_(list(fact_ids)))
            .values(status=status.value)
        )
        return result.rowcount or 0

    def flag_low_confidence(self, threshold: float) -> int:
        """Move facts below the confidence threshold into the review queue.

        Only ``extracted`` facts are touched. A fact a human has already looked at
        does not get pulled back into the queue by a threshold change, and a
        ``conflicted`` fact is already flagged for a different and stronger reason.
        """
        result = self._conn.execute(
            sa.update(facts)
            .where(
                facts.c.status == FactStatus.EXTRACTED.value,
                threshold > LIMITING_CONFIDENCE,
            )
            .values(status=FactStatus.NEEDS_REVIEW.value)
        )
        return result.rowcount or 0

    def flag_for_review(self, fact_ids: list[str], *, note: str | None = None) -> int:
        """Route named facts to review, appending why.

        The note is appended rather than replaced: a fact can fail two rules, and
        the second reason is not less useful than the first. Facts a human has
        already adjudicated are left alone — a validation rule does not reopen a
        decision a reviewer has made.
        """
        if not fact_ids:
            return 0
        result = self._conn.execute(
            sa.update(facts)
            .where(
                facts.c.fact_id.in_(list(fact_ids)),
                facts.c.status.in_(
                    [FactStatus.EXTRACTED.value, FactStatus.NEEDS_REVIEW.value]
                ),
            )
            .values(
                status=FactStatus.NEEDS_REVIEW.value,
                notes=sa.case(
                    (facts.c.notes.is_(None), sa.literal(note)),
                    else_=facts.c.notes.concat(sa.literal(f" | {note}")),
                )
                if note
                else facts.c.notes,
            )
        )
        return result.rowcount or 0

    def delete_for_document(self, document_id: str, document_version: int) -> int:
        """Remove a document version's facts, for an idempotent re-extraction.

        The one place facts are deleted rather than superseded, and it is not an
        exception to the append-only principle: these rows are this stage's own
        output for this version, being replaced by the same stage on a re-run. A
        fact from a *different* version is never touched — that is what
        supersession is for.
        """
        result = self._conn.execute(
            sa.delete(facts).where(
                facts.c.document_id == document_id,
                facts.c.document_version == document_version,
            )
        )
        return result.rowcount or 0

    def flag_low_confidence_for_document(
        self, document_id: str, document_version: int, threshold: float
    ) -> int:
        """The review sweep, restricted to one document version."""
        result = self._conn.execute(
            sa.update(facts)
            .where(
                facts.c.document_id == document_id,
                facts.c.document_version == document_version,
                facts.c.status == FactStatus.EXTRACTED.value,
                threshold > LIMITING_CONFIDENCE,
            )
            .values(status=FactStatus.NEEDS_REVIEW.value)
        )
        return result.rowcount or 0

    # ---------------------------------------------------------------- reads

    def get(self, fact_id: str, scope: Scope) -> Fact | None:
        row = (
            self._conn.execute(
                sa.select(facts).where(
                    facts.c.fact_id == fact_id, scope.clause(facts.c.entity_id)
                )
            )
            .mappings()
            .first()
        )
        return row_to_fact(dict(row)) if row else None

    def by_ids(self, fact_ids: Sequence[str], scope: Scope) -> list[Fact]:
        if not fact_ids:
            return []
        rows = (
            self._conn.execute(
                sa.select(facts)
                .where(
                    facts.c.fact_id.in_(list(fact_ids)),
                    scope.clause(facts.c.entity_id),
                )
                .order_by(facts.c.value)
            )
            .mappings()
            .all()
        )
        return [row_to_fact(dict(row)) for row in rows]

    def query(
        self,
        scope: Scope,
        *,
        entity_id: str | None = None,
        metric: str | None = None,
        fiscal_year: str | None = None,
        document_id: str | None = None,
        status: FactStatus | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[Fact]:
        query = sa.select(facts).where(scope.clause(facts.c.entity_id))

        if not include_inactive:
            query = query.where(_active())
        if entity_id is not None:
            query = query.where(facts.c.entity_id == entity_id)
        if metric is not None:
            query = query.where(facts.c.metric == metric)
        if fiscal_year is not None:
            query = query.where(facts.c.fiscal_year == fiscal_year)
        if document_id is not None:
            query = query.where(facts.c.document_id == document_id)
        if status is not None:
            query = query.where(facts.c.status == status.value)

        rows = (
            self._conn.execute(
                query.order_by(
                    facts.c.entity_id,
                    facts.c.metric,
                    facts.c.period_start,
                    facts.c.fact_id,
                ).limit(limit)
            )
            .mappings()
            .all()
        )
        return [row_to_fact(dict(row)) for row in rows]

    def entity_metric_series(
        self, metric: str, scope: Scope, *, unit: str | None = None
    ) -> list[dict[str, Any]]:
        """Per-entity fiscal-year totals for one metric, shaped for the charts.

        ``fiscal_year IS NOT NULL`` is not an optimisation — it is a refusal.
        A calendar-year figure summed onto a fiscal-year axis is precisely the
        comparison the period normalizer declines to make, and it would be
        invisible once rendered as a bar.
        """
        query = (
            sa.select(
                facts.c.entity_id,
                facts.c.fiscal_year,
                facts.c.unit,
                sa.func.sum(facts.c.value).label("value"),
                sa.func.count().label("fact_count"),
            )
            .where(
                facts.c.metric == metric,
                facts.c.fiscal_year.is_not(None),
                _active(),
                scope.clause(facts.c.entity_id),
            )
            .group_by(facts.c.entity_id, facts.c.fiscal_year, facts.c.unit)
            .order_by(facts.c.fiscal_year, facts.c.entity_id)
        )
        if unit is not None:
            query = query.where(facts.c.unit == unit)

        return [dict(row) for row in self._conn.execute(query).mappings().all()]

    def document_facets(
        self, document_id: str, document_version: int
    ) -> dict[str, list[str]]:
        """What this document version turned out to be about.

        Derived from its facts rather than from its filename, which is why the
        document list can be filtered by entity and fiscal year at all: a file
        called "final_v3_REVISED.pdf" says nothing; its figures say everything.
        """
        rows = self._conn.execute(
            sa.select(facts.c.entity_id, facts.c.metric, facts.c.fiscal_year)
            .where(
                facts.c.document_id == document_id,
                facts.c.document_version == document_version,
                _active(),
            )
            .distinct()
        ).all()

        return {
            "entities": sorted({row[0] for row in rows if row[0]}),
            "metrics": sorted({row[1] for row in rows if row[1]}),
            "fiscal_years": sorted({row[2] for row in rows if row[2]}),
        }

    def answerable_measurements(
        self, scope: Scope, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Measurements this corpus can actually answer, for the caller.

        The question "what can I ask?" has to be answered from the data rather
        than from a list of examples someone wrote once. A fixed example that no
        longer matches the corpus does not degrade — it tells the user the
        system is broken, because a refusal looks the same whether the question
        was unanswerable or the product is.

        Only ``validated`` facts count. A figure still in review is one the
        exact-figure path would decline, so offering it as a suggestion would
        send the user straight into a refusal.
        """
        rows = (
            self._conn.execute(
                sa.select(
                    facts.c.entity_id,
                    facts.c.metric,
                    facts.c.period_label,
                    facts.c.fiscal_year,
                    sa.func.count().label("fact_count"),
                )
                .where(
                    facts.c.status == FactStatus.VALIDATED.value,
                    scope.clause(facts.c.entity_id),
                )
                .group_by(
                    facts.c.entity_id,
                    facts.c.metric,
                    facts.c.period_label,
                    facts.c.fiscal_year,
                )
                # Most-corroborated first: a measurement several documents agree on
                # is the one most likely to satisfy whoever is trying the system.
                .order_by(
                    sa.func.count().desc(),
                    facts.c.entity_id,
                    facts.c.metric,
                )
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def comparable_metrics(self, scope: Scope, *, minimum_entities: int = 2) -> list[str]:
        """Metrics held by enough entities for a comparison to say anything.

        A "compare across subsidiaries" question over a metric only one
        subsidiary reports produces a one-bar chart, which looks like a bug.
        """
        rows: Sequence[str | None] = (
            self._conn.execute(
                sa.select(facts.c.metric)
                .where(
                    facts.c.fiscal_year.is_not(None),
                    _active(),
                    scope.clause(facts.c.entity_id),
                )
                .group_by(facts.c.metric)
                .having(sa.func.count(sa.distinct(facts.c.entity_id)) >= minimum_entities)
                .order_by(sa.func.count(sa.distinct(facts.c.entity_id)).desc())
            )
            .scalars()
            .all()
        )
        return [str(metric) for metric in rows]

    # -------------------------------------------------------------- counting

    def counts(self, scope: Scope) -> dict[str, int]:
        """Status tallies plus distinct entity and metric counts, in one round trip."""
        scoped = scope.clause(facts.c.entity_id)

        def when(status: FactStatus) -> sa.ColumnElement[int]:
            return sa.func.count().filter(facts.c.status == status.value)

        row = (
            self._conn.execute(
                sa.select(
                    sa.func.count().label("facts"),
                    when(FactStatus.VALIDATED).label("facts_validated"),
                    when(FactStatus.NEEDS_REVIEW).label("facts_needs_review"),
                    when(FactStatus.CONFLICTED).label("facts_conflicted"),
                    sa.func.count(sa.distinct(facts.c.entity_id)).label("entities"),
                    sa.func.count(sa.distinct(facts.c.metric)).label("metrics"),
                ).where(scoped, _active())
            )
            .mappings()
            .one()
        )
        return {key: int(value or 0) for key, value in row.items()}

    def document_counts(self, scope: Scope) -> dict[str, int]:
        row = (
            self._conn.execute(
                sa.select(
                    sa.func.count().label("documents"),
                    sa.func.coalesce(sa.func.sum(documents.c.page_count), 0).label(
                        "pages"
                    ),
                ).where(scope.clause(documents.c.owner_entity_id))
            )
            .mappings()
            .one()
        )
        return {key: int(value or 0) for key, value in row.items()}
