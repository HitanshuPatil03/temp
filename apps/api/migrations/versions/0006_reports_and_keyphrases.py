"""persist report manifests and document keyphrases

Revision ID: 0006
Revises: 0005
Created: 2026-10-01

Two tables, one for each of the problem statement's first two deliverables.
They land together because they are the same kind of change — a derived artefact
that was being computed and thrown away now gets a place to live.

**``reports``** makes a generated report survive the request that made it. Until
now :func:`mrip.reports.generate.generate` returned a manifest and the process
dropped it, which left the central claim of §11.3 — *re-render a two-year-old
report and get the figures as approved* — true of a function and false of the
system. The manifest is stored whole, as JSONB: it is read back as one document,
never queried field by field, and keeping it verbatim is precisely what makes the
reproduction exact. ``entity_id`` is lifted out of it into its own indexed column
because every read in this system is scoped, and a scope filter over JSONB gives
up the index.

**``document_keyphrases``** gives §12.1's deterministic pass somewhere to put its
output, so the word cloud is a table read rather than a scan of the evidence
corpus on every page load. The foreign key cascades, unlike the one on
``evidence`` and ``facts``: a keyphrase is a derived index entry that can be
recomputed from the document at any time, so it must never be the row that blocks
a deletion. Evidence is the opposite, and the difference in ``ondelete`` is the
whole statement of which is which.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("report_id", sa.Text, primary_key=True),
        sa.Column("template_id", sa.Text, nullable=False),
        sa.Column("template_version", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("entity_id", sa.Text, nullable=False),
        sa.Column("period_label", sa.Text, nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "draft",
                "in_review",
                "approved",
                "published",
                name="report_state",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("manifest", pg.JSONB, nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("generated_by", sa.Text, nullable=True),
        sa.Column("approved_by", sa.Text, nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["generated_by"],
            ["users.user_id"],
            name="fk_reports_generated_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.user_id"],
            name="fk_reports_approved_by",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL)",
            name="ck_reports_approval_is_all_or_nothing",
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR approved_at IS NOT NULL",
            name="ck_reports_published_implies_approved",
        ),
        sa.CheckConstraint(
            "template_version >= 1", name="ck_reports_template_version_positive"
        ),
    )
    op.create_index("ix_reports_entity_id", "reports", ["entity_id"])
    op.create_index("ix_reports_state", "reports", ["state"])
    op.create_index("ix_reports_generated_at", "reports", ["generated_at"])

    op.create_table(
        "document_keyphrases",
        sa.Column("document_id", sa.Text, primary_key=True),
        sa.Column("document_version", sa.Integer, primary_key=True),
        sa.Column("term", sa.Text, primary_key=True),
        sa.Column("occurrences", sa.Integer, nullable=False),
        sa.Column("score", sa.Double, nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "document_version"],
            ["documents.document_id", "documents.version"],
            name="fk_document_keyphrases_document_version",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "occurrences >= 1", name="ck_document_keyphrases_occurrences_positive"
        ),
        sa.CheckConstraint("score >= 0", name="ck_document_keyphrases_score_nonnegative"),
    )
    op.create_index("ix_document_keyphrases_term", "document_keyphrases", ["term"])


def downgrade() -> None:
    op.drop_index("ix_document_keyphrases_term", table_name="document_keyphrases")
    op.drop_table("document_keyphrases")
    op.drop_index("ix_reports_generated_at", table_name="reports")
    op.drop_index("ix_reports_state", table_name="reports")
    op.drop_index("ix_reports_entity_id", table_name="reports")
    op.drop_table("reports")
