"""index the comparison series

Revision ID: 0007
Revises: 0006
Created: 2026-10-04

A partial index for :meth:`mrip.db.repositories.facts.FactRepository.entity_metric_series`,
which is the query behind every comparison chart.

**Why it was slow.** The series filters on ``(metric, fiscal_year IS NOT NULL,
status, full-year span)`` and groups by ``(entity_id, fiscal_year, unit)``. Both
existing indexes on ``facts`` lead with ``entity_id`` — ``ix_facts_lookup`` and
``ix_facts_conflict_key`` — and an HQ-wide scope does not constrain that column,
because an HQ officer sees every subsidiary. So the planner had nothing to use and
read the whole table. Measured on 1,000,000 facts across 3,404 documents, which is
the shape of the catalogued corpus: a parallel sequential scan, **125 ms**, to find
the 3,957 rows that mattered. It is the default view on the dashboard, it grows
linearly with the corpus, and a subsidiary officer's scoped version of the same
chart took 10 ms — so the person with the widest remit had the slowest screen.

**Why partial.** Only a *validated* fact whose period spans a whole fiscal year can
appear in a series; that is 8% of the corpus. Restricting the index to exactly
those rows makes it 720 kB — against 9 MB for ``ix_facts_lookup`` and 11 MB for
``ix_facts_conflict_key`` — and means eleven inserts in twelve skip it entirely,
since a monthly or cumulative row does not match the predicate. The same measured
query drops to **3.8 ms**, and the scoped case improves too, to 0.3 ms.

The predicate repeats the repository's definition of "a figure a series may
return", so the two must change together. That is stated in
``entity_metric_series``' docstring rather than only here, because the person who
widens the filter will be reading the Python.

``period_end - period_start`` is immutable on dates, which is what lets it appear
in a partial-index predicate at all.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Column order is the query's: `metric` is the only equality filter an
    # HQ-wide call provides, so it leads; `fiscal_year` and `entity_id` follow
    # because the result is grouped and ordered by them, which lets the planner
    # take the grouping from the index rather than sorting afterwards.
    op.execute(
        """
        CREATE INDEX ix_facts_series
            ON facts (metric, fiscal_year, entity_id, unit)
         WHERE status = 'validated'
           AND (period_end - period_start) >= 350
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_facts_series")
