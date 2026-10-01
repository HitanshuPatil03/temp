"""Keyphrase repository — the word cloud's storage and its drill-through.

Two reads matter here and they are deliberately different shapes.

**The cloud** groups across documents: a term's weight is
``COUNT(DISTINCT document_id)``, not ``SUM(occurrences)``. ARCHITECTURE §12.2 is
explicit about why — one repetitive annexure would otherwise dominate a
corpus-wide cloud with its own boilerplate. The raw count travels alongside for
the hover, because "appears in 14 documents" and "appears 300 times" answer
different questions and collapsing them loses one.

**The drill-through** goes the other way, from a term back to the documents and
pages that produced it, which is what makes the cloud an index rather than
decoration (§12.3).

Both are scoped through the owning document, so a term appearing only in another
subsidiary's unpublished filings does not leak the existence of those filings —
the cloud is built *from the caller's corpus*, not filtered after the fact.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from mrip.auth.scope import Scope
from mrip.db.tables import document_keyphrases, documents
from mrip.schemas import CloudTerm, TermDocument

__all__ = ["KeyphraseRepository"]


class KeyphraseRepository:
    """Stored keyphrases, read as a cloud or as a drill-through."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # ----------------------------------------------------------------- writes

    def replace_for_document(
        self, document_id: str, document_version: int, rows: Sequence[dict[str, Any]]
    ) -> int:
        """Replace one document version's keyphrases with a fresh extraction.

        Replace rather than append, keyed on the version, for the same reason the
        digitizer replaces its own spans: re-running extraction must produce the
        same table, not a second copy of it. A different version's rows are never
        touched.
        """
        self._conn.execute(
            sa.delete(document_keyphrases).where(
                document_keyphrases.c.document_id == document_id,
                document_keyphrases.c.document_version == document_version,
            )
        )
        if not rows:
            return 0
        self._conn.execute(sa.insert(document_keyphrases), list(rows))
        return len(rows)

    # ------------------------------------------------------------------ reads

    def cloud(
        self,
        scope: Scope,
        *,
        fiscal_year: str | None = None,
        entity_id: str | None = None,
        doc_class: str | None = None,
        limit: int = 120,
    ) -> list[CloudTerm]:
        """The word cloud, sized by document frequency (§12.2).

        Filters are part of the query rather than a post-filter on a rendered
        image (§12.3): narrowing to a fiscal year re-weights the cloud, because
        document frequency within that year is a different number from document
        frequency across the corpus.
        """
        document_count = sa.func.count(sa.distinct(document_keyphrases.c.document_id))
        query = (
            sa.select(
                document_keyphrases.c.term,
                document_count.label("document_count"),
                sa.func.sum(document_keyphrases.c.occurrences).label("occurrences"),
                sa.func.max(document_keyphrases.c.score).label("top_score"),
            )
            .select_from(
                document_keyphrases.join(
                    documents,
                    sa.and_(
                        documents.c.document_id == document_keyphrases.c.document_id,
                        documents.c.version == document_keyphrases.c.document_version,
                    ),
                )
            )
            .where(scope.clause(documents.c.owner_entity_id))
            .group_by(document_keyphrases.c.term)
        )
        query = self._filtered(
            query, fiscal_year=fiscal_year, entity_id=entity_id, doc_class=doc_class
        )

        rows = (
            self._conn.execute(
                # Document frequency first — that is the size on screen. Score
                # breaks ties so the ordering is stable rather than arbitrary,
                # which matters for a cloud that must render the same twice.
                query.order_by(
                    document_count.desc(),
                    sa.func.max(document_keyphrases.c.score).desc(),
                    document_keyphrases.c.term,
                ).limit(limit)
            )
            .mappings()
            .all()
        )
        return [
            CloudTerm(
                term=row["term"],
                document_count=int(row["document_count"]),
                occurrences=int(row["occurrences"]),
                score=float(row["top_score"]),
            )
            for row in rows
        ]

    def documents_for_term(
        self,
        term: str,
        scope: Scope,
        *,
        fiscal_year: str | None = None,
        entity_id: str | None = None,
        doc_class: str | None = None,
        limit: int = 100,
    ) -> list[TermDocument]:
        """Which documents used a term — the click-through target (§12.3).

        Every term in the cloud reaches a specific document here, and the
        document's own evidence view takes it the rest of the way to the page.
        A term with no reachable document is a dead end, which the gate forbids.
        """
        query = (
            sa.select(
                documents.c.document_id,
                documents.c.version,
                documents.c.title,
                documents.c.filename,
                documents.c.fiscal_year,
                documents.c.doc_class,
                document_keyphrases.c.occurrences,
                document_keyphrases.c.score,
            )
            .select_from(
                document_keyphrases.join(
                    documents,
                    sa.and_(
                        documents.c.document_id == document_keyphrases.c.document_id,
                        documents.c.version == document_keyphrases.c.document_version,
                    ),
                )
            )
            .where(
                document_keyphrases.c.term == term,
                scope.clause(documents.c.owner_entity_id),
            )
        )
        query = self._filtered(
            query, fiscal_year=fiscal_year, entity_id=entity_id, doc_class=doc_class
        )

        rows = (
            self._conn.execute(
                query.order_by(
                    document_keyphrases.c.score.desc(), documents.c.document_id
                ).limit(limit)
            )
            .mappings()
            .all()
        )
        return [
            TermDocument(
                document_id=row["document_id"],
                document_version=int(row["version"]),
                title=row["title"],
                filename=row["filename"],
                fiscal_year=row["fiscal_year"],
                doc_class=row["doc_class"],
                occurrences=int(row["occurrences"]),
                score=float(row["score"]),
            )
            for row in rows
        ]

    def prevalence(
        self, term: str, scope: Scope, *, entity_id: str | None = None
    ) -> list[dict[str, Any]]:
        """How many documents used a term in each fiscal year (§12.7).

        Longitudinal prevalence: the shape that answers "is this subject getting
        more attention or less", which a single cloud cannot show.
        """
        query = (
            sa.select(
                documents.c.fiscal_year,
                sa.func.count(sa.distinct(document_keyphrases.c.document_id)).label(
                    "document_count"
                ),
            )
            .select_from(
                document_keyphrases.join(
                    documents,
                    sa.and_(
                        documents.c.document_id == document_keyphrases.c.document_id,
                        documents.c.version == document_keyphrases.c.document_version,
                    ),
                )
            )
            .where(
                document_keyphrases.c.term == term,
                documents.c.fiscal_year.is_not(None),
                scope.clause(documents.c.owner_entity_id),
            )
            .group_by(documents.c.fiscal_year)
            .order_by(documents.c.fiscal_year)
        )
        if entity_id is not None:
            query = query.where(documents.c.owner_entity_id == entity_id)
        rows = self._conn.execute(query).mappings().all()
        return [
            {
                "fiscal_year": row["fiscal_year"],
                "document_count": int(row["document_count"]),
            }
            for row in rows
        ]

    def count_terms(self, scope: Scope) -> int:
        """Distinct terms in the caller's corpus — for the empty-state copy."""
        return (
            self._conn.execute(
                sa.select(sa.func.count(sa.distinct(document_keyphrases.c.term)))
                .select_from(
                    document_keyphrases.join(
                        documents,
                        sa.and_(
                            documents.c.document_id == document_keyphrases.c.document_id,
                            documents.c.version == document_keyphrases.c.document_version,
                        ),
                    )
                )
                .where(scope.clause(documents.c.owner_entity_id))
            ).scalar()
            or 0
        )

    # -------------------------------------------------------------- internals

    def _filtered(
        self,
        query: sa.Select[Any],
        *,
        fiscal_year: str | None,
        entity_id: str | None,
        doc_class: str | None,
    ) -> sa.Select[Any]:
        """Apply the three §12.3 filters, each against the document."""
        if fiscal_year is not None:
            query = query.where(documents.c.fiscal_year == fiscal_year)
        if entity_id is not None:
            query = query.where(documents.c.owner_entity_id == entity_id)
        if doc_class is not None:
            query = query.where(documents.c.doc_class == doc_class)
        return query
