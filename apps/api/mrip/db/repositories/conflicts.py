"""Conflict repository — the radar, and the only write that settles it.

There is deliberately **no method that merges, averages or prefers** two
disagreeing figures. :meth:`ConflictRepository.resolve` takes the id of the fact a
named person chose, and the losing facts become ``rejected`` rather than
disappearing. If a future requirement asks for automatic reconciliation, it needs
a new method and an argument for why a machine should make that call — it cannot
arrive as a quiet change to an existing one.

Detection is a **recompute**: it re-derives the open radar from current facts,
reconciles membership, and clears the ``conflicted`` flag from facts that no
longer disagree. A stale conflict is as harmful as a missed one — a reviewer who
finds the queue full of already-settled disagreements stops reading it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mrip.auth.scope import Scope
from mrip.db.repositories.documents import new_id
from mrip.db.repositories.facts import INACTIVE_STATES, FactRepository
from mrip.db.tables import conflict_facts, conflicts, facts
from mrip.schemas import ConflictGroup, FactStatus

__all__ = ["ConflictRepository"]

#: Relative spread: how far apart the extremes are, as a fraction of the larger
#: magnitude. Scale-free, so a 0.5% threshold means the same thing for a mine
#: producing 3 MT and a subsidiary producing 180 MT.
_SPREAD = (sa.func.max(facts.c.value) - sa.func.min(facts.c.value)) / sa.func.nullif(
    sa.func.greatest(
        sa.func.abs(sa.func.max(facts.c.value)),
        sa.func.abs(sa.func.min(facts.c.value)),
    ),
    0,
)

_GROUP_KEY = (
    facts.c.entity_id,
    facts.c.metric,
    facts.c.unit,
    facts.c.period_label,
)


class ConflictRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._facts = FactRepository(conn)

    # ------------------------------------------------------------- detection

    def detect(self, material_spread: float, scope: Scope) -> list[ConflictGroup]:
        """Re-derive the open radar. Returns the groups now awaiting adjudication."""
        groups = (
            self._conn.execute(
                sa.select(
                    *_GROUP_KEY,
                    sa.func.min(facts.c.value).label("value_low"),
                    sa.func.max(facts.c.value).label("value_high"),
                    sa.func.array_agg(
                        aggregate_order_by(facts.c.fact_id, facts.c.value)
                    ).label("fact_ids"),
                )
                .where(
                    facts.c.status.not_in([state.value for state in INACTIVE_STATES]),
                    scope.clause(facts.c.entity_id),
                )
                .group_by(*_GROUP_KEY)
                .having(sa.func.count(sa.distinct(facts.c.value)) > 1)
                .having(material_spread <= _SPREAD)
                .order_by(_SPREAD.desc())
            )
            .mappings()
            .all()
        )

        live_conflict_ids: list[str] = []
        conflicted_fact_ids: list[str] = []

        for group in groups:
            # Upsert against the partial unique index on unresolved conflicts, so
            # re-running detection updates the existing group and preserves its
            # original `detected_at` rather than resetting the clock.
            statement = (
                pg_insert(conflicts)
                .values(
                    conflict_id=new_id("cfl"),
                    entity_id=group["entity_id"],
                    metric=group["metric"],
                    unit=group["unit"],
                    period_label=group["period_label"],
                    value_low=group["value_low"],
                    value_high=group["value_high"],
                )
                .on_conflict_do_update(
                    index_elements=[
                        "entity_id",
                        "metric",
                        "unit",
                        "period_label",
                    ],
                    index_where=conflicts.c.resolved_fact_id.is_(None),
                    set_={
                        "value_low": sa.text("excluded.value_low"),
                        "value_high": sa.text("excluded.value_high"),
                    },
                )
                .returning(conflicts.c.conflict_id)
            )
            conflict_id: str = self._conn.execute(statement).scalar_one()
            live_conflict_ids.append(conflict_id)

            member_ids = list(group["fact_ids"])
            conflicted_fact_ids.extend(member_ids)

            # Membership is recomputed wholesale: a fact that left the group must
            # stop being listed in it.
            self._conn.execute(
                sa.delete(conflict_facts).where(
                    conflict_facts.c.conflict_id == conflict_id
                )
            )
            self._conn.execute(
                sa.insert(conflict_facts),
                [
                    {"conflict_id": conflict_id, "fact_id": fact_id}
                    for fact_id in member_ids
                ],
            )

        self._reconcile_flags(conflicted_fact_ids, scope)
        self._drop_stale(live_conflict_ids, scope)
        return self.open(scope)

    def _reconcile_flags(self, conflicted_fact_ids: list[str], scope: Scope) -> None:
        """Flag current members, and un-flag facts that no longer disagree."""
        if conflicted_fact_ids:
            self._conn.execute(
                sa.update(facts)
                .where(
                    facts.c.fact_id.in_(conflicted_fact_ids),
                    facts.c.status.not_in([state.value for state in INACTIVE_STATES]),
                )
                .values(status=FactStatus.CONFLICTED.value)
            )

        # Back to `extracted`, not to `validated`: clearing a conflict returns a
        # fact to the pipeline, it does not vouch for it. The confidence sweep
        # decides whether it still needs review.
        self._conn.execute(
            sa.update(facts)
            .where(
                facts.c.status == FactStatus.CONFLICTED.value,
                facts.c.fact_id.not_in(conflicted_fact_ids or [""]),
                scope.clause(facts.c.entity_id),
            )
            .values(status=FactStatus.EXTRACTED.value)
        )

    def _drop_stale(self, live_conflict_ids: list[str], scope: Scope) -> None:
        """Remove unresolved groups that no longer reflect a real disagreement.

        Only *unresolved* ones. A resolved conflict is a decision record and is
        never removed by a recompute.
        """
        self._conn.execute(
            sa.delete(conflicts).where(
                conflicts.c.resolved_fact_id.is_(None),
                conflicts.c.conflict_id.not_in(live_conflict_ids or [""]),
                scope.clause(conflicts.c.entity_id),
            )
        )

    # ------------------------------------------------------------------ reads

    def open(self, scope: Scope) -> list[ConflictGroup]:
        """Conflicts awaiting adjudication, each carrying every disagreeing fact."""
        rows = (
            self._conn.execute(
                sa.select(conflicts)
                .where(
                    conflicts.c.resolved_fact_id.is_(None),
                    scope.clause(conflicts.c.entity_id),
                )
                # Widest disagreement first — that is the one worth a reviewer's
                # attention, not the one detected most recently.
                .order_by(
                    (
                        (conflicts.c.value_high - conflicts.c.value_low)
                        / sa.func.nullif(sa.func.abs(conflicts.c.value_high), 0)
                    ).desc(),
                    conflicts.c.detected_at.desc(),
                )
            )
            .mappings()
            .all()
        )
        return [self._hydrate(dict(row), scope) for row in rows]

    def get(self, conflict_id: str, scope: Scope) -> ConflictGroup | None:
        row = (
            self._conn.execute(
                sa.select(conflicts).where(
                    conflicts.c.conflict_id == conflict_id,
                    scope.clause(conflicts.c.entity_id),
                )
            )
            .mappings()
            .first()
        )
        return self._hydrate(dict(row), scope) if row else None

    def count_open(self, scope: Scope) -> int:
        return (
            self._conn.execute(
                sa.select(sa.func.count())
                .select_from(conflicts)
                .where(
                    conflicts.c.resolved_fact_id.is_(None),
                    scope.clause(conflicts.c.entity_id),
                )
            ).scalar()
            or 0
        )

    def _hydrate(self, row: dict[str, Any], scope: Scope) -> ConflictGroup:
        member_ids: Sequence[str] = (
            self._conn.execute(
                sa.select(conflict_facts.c.fact_id).where(
                    conflict_facts.c.conflict_id == row["conflict_id"]
                )
            )
            .scalars()
            .all()
        )
        return ConflictGroup(
            conflict_id=row["conflict_id"],
            entity_id=row["entity_id"],
            metric=row["metric"],
            unit=row["unit"],
            period_label=row["period_label"],
            facts=self._facts.by_ids(list(member_ids), scope),
            detected_at=row["detected_at"],
            resolved_fact_id=row["resolved_fact_id"],
            resolution_note=row["resolution_note"],
        )

    # ---------------------------------------------------------------- resolve

    def resolve(
        self,
        conflict_id: str,
        winning_fact_id: str,
        note: str | None,
        scope: Scope,
        *,
        actor_user_id: str | None = None,
    ) -> bool:
        """Record which figure a reviewer accepted.

        Returns ``False`` if the conflict does not exist, is out of scope, or the
        nominated fact is not one of the facts in dispute — the last of which is
        the important one. Accepting an arbitrary fact id would let a caller
        "resolve" a disagreement with a number that was never part of it.
        """
        member_ids: set[str] = set(
            self._conn.execute(
                sa.select(conflict_facts.c.fact_id)
                .join(conflicts, conflicts.c.conflict_id == conflict_facts.c.conflict_id)
                .where(
                    conflict_facts.c.conflict_id == conflict_id,
                    conflicts.c.resolved_fact_id.is_(None),
                    scope.clause(conflicts.c.entity_id),
                )
            )
            .scalars()
            .all()
        )
        if winning_fact_id not in member_ids:
            return False

        self._conn.execute(
            sa.update(conflicts)
            .where(conflicts.c.conflict_id == conflict_id)
            .values(
                resolved_fact_id=winning_fact_id,
                resolution_note=note,
                resolved_by=actor_user_id,
                resolved_at=sa.func.now(),
            )
        )
        self._conn.execute(
            sa.update(facts)
            .where(facts.c.fact_id == winning_fact_id)
            .values(
                status=FactStatus.VALIDATED.value,
                review_state="approved",
            )
        )
        losers = member_ids - {winning_fact_id}
        if losers:
            # REJECTED, not SUPERSEDED. The two are deliberately distinct: a fact
            # is *superseded* when a newer document version replaced it, and
            # *rejected* when a person judged it wrong. Collapsing them would lose
            # the difference between "the source was revised" and "the source was
            # mistaken" — which is exactly what a later reader of the audit trail
            # needs to know. Neither is deleted.
            self._conn.execute(
                sa.update(facts)
                .where(facts.c.fact_id.in_(sorted(losers)))
                .values(
                    status=FactStatus.REJECTED.value,
                    review_state="rejected",
                )
            )
        return True
