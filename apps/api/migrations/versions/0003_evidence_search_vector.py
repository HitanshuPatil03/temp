"""evidence search vector

Revision ID: 0003
Revises: 0002
Created: 2026-09-24

Lexical search over evidence text, as a **generated** column.

Generated rather than maintained by an indexing stage, because a search index a
stage keeps in step is a search index that silently falls behind the moment that
stage fails — and nobody notices until a reviewer says "the figure is in the
document but search cannot find it". PostgreSQL recomputes this on every insert
and update, so it cannot drift.

The column is `STORED`, which costs disk and saves the recomputation on every
query. For a corpus of hundreds of thousands of evidence rows that trade is the
right way round: rows are written once and searched many times.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidence",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(text, ''))", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_evidence_search",
        "evidence",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_search", table_name="evidence", postgresql_using="gin")
    op.drop_column("evidence", "search_vector")
