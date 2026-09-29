"""document provenance for fetched corpus documents

Revision ID: 0004
Revises: 0003
Created: 2026-09-26

A ``source_url`` on a document: **where a document that we did not receive from a
person came from**.

The platform's whole claim is that a figure traces back to its source. For an
uploaded file that chain ends at the filename and the uploader's account. For a
document fetched from a public website the chain is longer and more interesting —
it ends at a URL on coal.gov.in — and until now that URL existed only in the
corpus manifest on disk, not in the database a reviewer queries.

Two columns, not one. ``source_url`` says where it came from; ``is_synthetic``,
which already exists, says whether *we* made it up. A fetched document is neither
an ordinary upload nor a generated one, and conflating the two in either direction
is the mistake that matters: badging a government PDF as synthetic would be as
wrong as letting a generated table pass as a government source.

The index is partial. Almost every document in a real deployment is an upload and
has a null here; only the corpus rows are worth looking up by URL, and a partial
index on the non-null ones is a fraction of the size.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_url", sa.Text, nullable=True))
    op.create_index(
        "ix_documents_source_url",
        "documents",
        ["source_url"],
        unique=False,
        postgresql_where=sa.text("source_url IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_source_url", table_name="documents")
    op.drop_column("documents", "source_url")
