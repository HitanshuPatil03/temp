"""mark evidence whose text layer is itself OCR output

Revision ID: 0005
Revises: 0004
Created: 2026-09-27

A boolean on ``evidence``: **was this row's text recognised by whoever produced
the PDF, rather than typeset into it?**

Until now the digitizer answered one question, "does this page have a text
layer", and used it for two purposes that turn out to be different:

1. *may I read this page's words as the document's own?* — and
2. *does this page have positioned text objects I can cluster into a table grid?*

A page saved with an OCR layer answers no to the first and yes to the second.
Collapsing them cost real documents: over the corpus's 191 statement-like PDFs,
60 — 31% — carry at least one such page, and on the CIL monthly production
statements that is the single page holding the table, so the document yielded
nothing at all. ``Provisional_Production_May_2026.pdf`` extracted zero facts for
exactly this reason while the same file, read directly, yields eighteen.

So table detection now runs over those pages too, and the cells it finds are
marked here. The mark is what makes that safe. ``ocr_confidence`` could not carry
it: NULL there means "native text, no recogniser, trustworthy", and reusing it for
"recognised, confidence unknowable" would rebuild the conflation this migration
exists to remove. The grid on such a page is the publisher's and is sound; the
glyphs may read ``635`` where the paper says ``63.5``. A reader that checks each
figure against the page's own printed growth column can use these cells safely; a
reader that checks nothing must not silently gain trust in them.

Backfilled ``false``, which is correct rather than merely convenient: every row
written before this revision came from a page the digitizer had already decided
had a native text layer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidence",
        sa.Column(
            "text_layer_recognised",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Partial: the recognised rows are the minority and the interesting ones —
    # "show me every figure that came off an OCR layer" is a review query, and a
    # reviewer should not pay for a scan of the native rows to ask it.
    op.create_index(
        "ix_evidence_recognised_layer",
        "evidence",
        ["document_id", "page"],
        unique=False,
        postgresql_where=sa.text("text_layer_recognised"),
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_recognised_layer", table_name="evidence")
    op.drop_column("evidence", "text_layer_recognised")
