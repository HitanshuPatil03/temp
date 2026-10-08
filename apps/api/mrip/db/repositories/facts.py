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
from mrip.db.tables import conflict_facts, conflicts, documents, facts
from mrip.schemas import Fact, FactStatus, ReviewState

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

    def review(
        self,
        fact_id: str,
        decision: str,
        scope: Scope,
        *,
        note: str | None = None,
        corrected_value: float | None = None,
    ) -> Fact | None:
        """A reviewer's adjudication of a single ``needs_review`` fact.

        This is the action the review queue was missing. Low-confidence and
        unit-ambiguous facts route to ``needs_review``, the dashboard counts
        them, and until now nothing could clear one: only *conflict* resolution
        promoted a fact to validated, so a plain low-confidence figure was a dead
        end a reviewer could see but not act on.

        Three decisions, mirroring conflict resolution's vocabulary so the audit
        trail reads consistently:

        - ``validate`` — the figure is right as extracted. ``needs_review →
          validated`` (``review_state = approved``).
        - ``correct`` — the figure was misread; the reviewer supplies the right
          canonical value. Stored validated with ``review_state = corrected``,
          the correction noted, and the original raw value kept beside it as the
          receipt — the evidence still quotes what the page printed.
        - ``reject`` — wrong with no correct reading. ``→ rejected`` (not
          deleted, not superseded: a person judged it wrong, and the trail says
          so).

        Returns the updated fact, or ``None`` when it does not exist, is out of
        scope, or is not in a reviewable state — a settled fact is not silently
        reopened. Scope is enforced.
        """
        current = self.get(fact_id, scope)
        if current is None or current.status not in (
            FactStatus.NEEDS_REVIEW,
            FactStatus.EXTRACTED,
        ):
            return None

        values: dict[str, Any] = {}
        if decision == "validate":
            values = {
                "status": FactStatus.VALIDATED.value,
                "review_state": ReviewState.APPROVED.value,
            }
        elif decision == "correct":
            if corrected_value is None:
                return None
            values = {
                "status": FactStatus.VALIDATED.value,
                "review_state": ReviewState.CORRECTED.value,
                "value": corrected_value,
            }
        elif decision == "reject":
            values = {
                "status": FactStatus.REJECTED.value,
                "review_state": ReviewState.REJECTED.value,
            }
        else:
            return None

        if note:
            values["notes"] = sa.case(
                (facts.c.notes.is_(None), sa.literal(note)),
                else_=facts.c.notes.concat(sa.literal(f" | {note}")),
            )

        self._conn.execute(
            sa.update(facts).where(facts.c.fact_id == fact_id).values(**values)
        )
        return self.get(fact_id, scope)

    def delete_for_document(self, document_id: str, document_version: int) -> int:
        """Remove a document version's facts, for an idempotent re-extraction.

        The one place facts are deleted rather than superseded, and it is not an
        exception to the append-only principle: these rows are this stage's own
        output for this version, being replaced by the same stage on a re-run. A
        fact from a *different* version is never touched — that is what
        supersession is for.

        Conflict rows that reference these facts are torn down first. ``facts``
        is referenced by ``conflict_facts.fact_id`` and ``conflicts.resolved_fact_id``,
        both ``ON DELETE RESTRICT``, so once ``validate`` has placed any of this
        document's facts into a conflict group the raw ``DELETE`` is blocked and
        the documented re-extraction path (a corrected extractor, a new metric)
        dead-letters. Any conflict touching a doomed fact — as a member or as the
        resolved winner — is a conflict about figures that are being replaced, so
        it is removed whole and the ``validate`` stage recomputes the radar from
        the new facts. The resolution *decision* is not lost: it remains in the
        append-only ``audit_log`` as the ``conflict.resolved`` event, which is the
        permanent record. The conflicts table is a derived working set.
        """
        doomed = sa.select(facts.c.fact_id).where(
            facts.c.document_id == document_id,
            facts.c.document_version == document_version,
        )
        affected = (
            sa.select(conflict_facts.c.conflict_id)
            .where(conflict_facts.c.fact_id.in_(doomed))
            .union(
                sa.select(conflicts.c.conflict_id).where(
                    conflicts.c.resolved_fact_id.in_(doomed)
                )
            )
        )
        affected_ids = [row[0] for row in self._conn.execute(affected).all()]
        if affected_ids:
            # Order matters for the RESTRICT keys: memberships, then the conflict
            # rows (clearing resolved_fact_id references), then the facts below.
            self._conn.execute(
                sa.delete(conflict_facts).where(
                    conflict_facts.c.conflict_id.in_(affected_ids)
                )
            )
            self._conn.execute(
                sa.delete(conflicts).where(conflicts.c.conflict_id.in_(affected_ids))
            )

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

    def promote_high_confidence_for_document(
        self, document_id: str, document_version: int, threshold: float
    ) -> int:
        """The other half of the sweep: accept what passed every check.

        Demotion was implemented three times over — at creation for a fuzzy
        entity or an ambiguous unit, per document at the normalize stage, and
        corpus-wide from the scheduler — and nothing ever promoted. So a fact
        that passed every automated check stayed ``extracted`` for good, and
        ``extracted`` is not a state anything uses: ``FactStatus.is_trustworthy``
        admits only ``validated``, so the figure could not be pinned in a report,
        could not appear in a comparison series, and was not in the review queue
        either, because that queue is ``needs_review``.

        The effect was backwards. A clean, unambiguous government statement
        produced figures that vanished — the generator refused with
        ``no_validated_fact`` while the review queue showed nothing to act on —
        and the only way a fact ever reached ``validated`` was by *disagreeing*
        with another one and having a reviewer pick it. The better the
        extraction, the more completely the number disappeared.

        ``.env.example`` has always described the intended rule: facts below the
        threshold "go to the review queue instead of being treated as validated".
        This is the "treated as validated" half.

        Three conditions, each of which has to hold on its own:

        ``status == extracted``
            A human's decision is never overwritten, and ``conflicted`` is a
            stronger flag that outranks confidence.
        ``limiting confidence >= threshold``
            Re-tested here rather than assumed from the normalize stage, so the
            promotion is safe whatever order the stages ran in — and ``least()``
            returns NULL when no stage reported a confidence, which fails this
            comparison and correctly leaves such a fact alone.
        ``not unit_ambiguous``
            ``MT`` is million tonnes in CIL reporting and a metric tonne
            everywhere else, a factor of a million. Extraction already routes an
            ambiguous unit to review, so this is belt and braces — but it is the
            one error that would put a figure a million times too large into a
            Ministry report, so it is worth asserting twice.
        """
        result = self._conn.execute(
            sa.update(facts)
            .where(
                facts.c.document_id == document_id,
                facts.c.document_version == document_version,
                facts.c.status == FactStatus.EXTRACTED.value,
                threshold <= LIMITING_CONFIDENCE,
                facts.c.unit_ambiguous.is_(False),
            )
            .values(status=FactStatus.VALIDATED.value)
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
        """Per-entity full-fiscal-year figures for one metric, shaped for charts.

        This is the comparison/trend path, and it has one job the single-figure
        path does not: it must not **overcount**. The CIL corpus is mostly
        monthly performance statements, so one subsidiary's FY2024-25 is stored
        as ~12 monthly facts *plus* ~12 overlapping cumulative (`Apr–…`) facts,
        every one tagged ``fiscal_year="FY2024-25"``. A naive ``SUM`` over them
        reports seven or eight times the real production — on a bar chart headed
        for a Ministry slide. Summing corroborating duplicates of one annual
        figure does the same.

        So this does not sum periods. It takes the **one authoritative
        full-year figure** per entity: a *validated* fact whose period spans the
        whole fiscal year (≈365 days). That is the same figure the exact path
        would return for "SECL coal production FY2024-25", so the chart and the
        exact answer cannot disagree.

        Three consequences, each deliberate:

        - **Validated only.** A figure still in review or in conflict is one the
          exact path refuses, so it must not appear in a comparison either.
        - **A subsidiary with only monthly facts and no annual total does not
          appear** for that year, rather than appearing with a summed-months
          number this system never validated as an annual total. Honest absence
          over a plausible wrong bar.
        - After conflict resolution there is exactly one validated full-year
          fact per ``(entity, metric, unit, period)``, so the aggregate below is
          over a single row; ``fact_count`` surfaces it if that ever changes.
        """
        # Date subtraction yields integer days in PostgreSQL. A full fiscal year
        # is 364–366 days; a month is ~30 and a part-year cumulative (Apr–Sep)
        # ~180, so the floor cleanly admits the annual fact and nothing else.
        #
        # This predicate and `status == VALIDATED` below are **repeated in the
        # partial index** `ix_facts_series` (migration 0007), which is what keeps
        # this query off a full table scan — 3.8 ms against 125 ms over a
        # 1M-fact corpus, measured HQ-wide. Widening either condition silently
        # stops the index matching and puts the dashboard back on a sequential
        # scan, so a change here needs a migration beside it.
        full_year = (facts.c.period_end - facts.c.period_start) >= 350

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
                facts.c.status == FactStatus.VALIDATED.value,
                full_year,
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
