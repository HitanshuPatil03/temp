"""Document and evidence repositories.

Both take a :class:`~mrip.auth.scope.Scope` on every read. Documents are scoped by
``owner_entity_id``; evidence is scoped transitively through its document, because
a page's text is exactly as sensitive as the document it came from.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import JSONB

from mrip.auth.scope import Scope
from mrip.db.mappers import document_to_row, row_to_document
from mrip.db.tables import documents, evidence
from mrip.schemas import Document, DocumentClass, DocumentState

__all__ = ["DocumentRepository", "EvidenceRepository", "new_id"]


def new_id(prefix: str) -> str:
    """A short, readable, collision-resistant id.

    Prefixed by kind so an id in a log line or an error message says what it
    refers to without a lookup — ``doc_1f3c…`` rather than a bare UUID.
    """
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class DocumentRepository:
    """The document registry, keyed by content hash."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # -------------------------------------------------------------- lookups

    def find_by_hash(self, content_hash: str) -> Document | None:
        """Identical bytes are the same document.

        Matched case-insensitively: SHA-256 digests get written both ways by
        different tools, and two rows for one file would double-count every fact
        in it. Comparison is on the lowered form rather than by normalizing on
        write, so a hash the source system printed in upper case still reads back
        as it was supplied.

        Unscoped by design: this is the dedup check on the *write* path, and it
        must not be possible to create a duplicate of a document merely because
        the uploader cannot see the original.
        """
        row = (
            self._conn.execute(
                sa.select(documents).where(
                    sa.func.lower(documents.c.content_hash) == content_hash.lower()
                )
            )
            .mappings()
            .first()
        )
        return row_to_document(dict(row)) if row else None

    def get(self, document_id: str, scope: Scope) -> Document | None:
        row = (
            self._conn.execute(
                sa.select(documents).where(
                    documents.c.document_id == document_id,
                    scope.clause(documents.c.owner_entity_id),
                )
            )
            .mappings()
            .first()
        )
        return row_to_document(dict(row)) if row else None

    def list(
        self,
        scope: Scope,
        *,
        limit: int = 100,
        offset: int = 0,
        state: DocumentState | None = None,
    ) -> list[Document]:
        query = (
            sa.select(documents)
            .where(scope.clause(documents.c.owner_entity_id))
            .order_by(documents.c.ingested_at.desc(), documents.c.document_id)
            .limit(limit)
            .offset(offset)
        )
        if state is not None:
            query = query.where(documents.c.state == state.value)
        rows = self._conn.execute(query).mappings().all()
        return [row_to_document(dict(row)) for row in rows]

    # --------------------------------------------------------------- writes

    def register(self, document: Document) -> Document:
        """Insert a document version, or return the existing one on a hash match.

        Idempotent on content hash, so a retried upload after a network failure
        cannot produce two rows for the same bytes.
        """
        existing = self.find_by_hash(document.content_hash)
        if existing is not None:
            return existing
        self._conn.execute(sa.insert(documents).values(**document_to_row(document)))
        return document

    def mark_superseded(self, old_document_id: str, new_document_id: str) -> int:
        """Point a new version at the one it replaces.

        The old row is *not* deleted or hidden — that is the whole point of the
        revision chain. Returns the number of facts moved to ``superseded``.
        """
        self._conn.execute(
            sa.update(documents)
            .where(documents.c.document_id == new_document_id)
            .values(supersedes=old_document_id, updated_at=sa.func.now())
        )
        # Facts carried by the old version stop being active, but stay queryable.
        from mrip.db.tables import facts  # local: avoids a module import cycle

        result = self._conn.execute(
            sa.update(facts)
            .where(
                facts.c.document_id == old_document_id,
                facts.c.status.not_in(["rejected", "superseded"]),
            )
            .values(status="superseded")
        )
        return result.rowcount or 0

    def set_state(
        self,
        document_id: str,
        state: DocumentState,
        *,
        failed_stage: str | None = None,
        failed_reason: str | None = None,
        progress: dict[str, Any] | None = None,
    ) -> None:
        """Advance the ingestion state machine (ARCHITECTURE §6).

        The caller is expected to be inside the same transaction as the stage's
        output, so a document is never marked ``digitized`` without its spans.

        ``progress`` is **merged** into whatever the document already carries, for
        the same reason :meth:`set_progress` merges: each stage reports its own
        key, and no stage's report is another's to discard. Replacing used to lose
        the extraction skip report — "37 cells had no unit, here are three of
        them" — the moment the normalize stage reported its own count, seconds
        later. That report is the work item for whoever owns the filing, and it
        was being destroyed before anyone could read it.
        """
        values: dict[str, Any] = {"state": state.value, "updated_at": sa.func.now()}
        if state is DocumentState.FAILED:
            if not failed_stage:
                raise ValueError("A failed document must name the stage that failed.")
            values["failed_stage"] = failed_stage
            values["failed_reason"] = failed_reason
        else:
            # Advancing past a failure clears it, so a retried document does not
            # keep showing a stale error.
            values["failed_stage"] = None
            values["failed_reason"] = None
        if progress is not None:
            # `||` on jsonb is a shallow merge: this stage's key replaces its own
            # earlier value (a re-run should overwrite its own stale count) and
            # leaves every other stage's alone. One statement, so two stages
            # reporting concurrently cannot read-modify-write over each other.
            values["stage_progress"] = documents.c.stage_progress.concat(
                sa.cast(progress, JSONB)
            )
        self._conn.execute(
            sa.update(documents)
            .where(documents.c.document_id == document_id)
            .values(**values)
        )

    def set_progress(self, document_id: str, stage: str, done: int, total: int) -> None:
        """Publish a per-stage counter, so the UI shows "OCR 142/400".

        Merged into ``stage_progress`` with ``||`` rather than replacing it: the
        digitize stage reporting its page count must not erase what the table
        extractor already recorded. Done in one statement so two stages
        reporting concurrently cannot read-modify-write over each other.
        """
        self._conn.execute(
            sa.update(documents)
            .where(documents.c.document_id == document_id)
            .values(
                stage_progress=documents.c.stage_progress.concat(
                    sa.func.jsonb_build_object(
                        stage,
                        sa.func.jsonb_build_object(
                            "done", done, "total", total, "at", sa.func.now()
                        ),
                    )
                ),
                updated_at=sa.func.now(),
            )
        )

    def set_class(self, document_id: str, doc_class: DocumentClass) -> None:
        """Record what the classifier decided the document actually is.

        The boundary's guess comes from magic bytes ("this is a PDF"); this is
        the finer answer ("its pages have no text layer"), and it selects the
        digitizer. Stored rather than recomputed because re-opening a 400-page
        PDF to ask again is not free.
        """
        self._conn.execute(
            sa.update(documents)
            .where(documents.c.document_id == document_id)
            .values(doc_class=doc_class.value, updated_at=sa.func.now())
        )

    def set_fiscal_year(self, document_id: str, fiscal_year: str) -> None:
        """Fill in a fiscal year the upload did not state.

        Only ever called when the document's own facts agree on exactly one,
        which is the single case where inferring it is not a guess.
        """
        self._conn.execute(
            sa.update(documents)
            .where(documents.c.document_id == document_id)
            .values(fiscal_year=fiscal_year, updated_at=sa.func.now())
        )

    def set_publisher(self, document_id: str, entity_id: str) -> None:
        """Attribute a document to the entity its facts are about.

        Also fills ``owner_entity_id``, which is what row-level scope filters on:
        an unattributed document is visible only to an unrestricted scope, so
        leaving it null would hide a subsidiary's own filing from that
        subsidiary.
        """
        self._conn.execute(
            sa.update(documents)
            .where(documents.c.document_id == document_id)
            .values(
                publisher_entity_id=entity_id,
                owner_entity_id=sa.func.coalesce(documents.c.owner_entity_id, entity_id),
                updated_at=sa.func.now(),
            )
        )

    def count_by_state(self) -> dict[str, int]:
        """Backs the pipeline health endpoint: "the API is up" is not "ingestion
        works"."""
        rows = self._conn.execute(
            sa.select(documents.c.state, sa.func.count())
            .group_by(documents.c.state)
            .order_by(documents.c.state)
        ).all()
        return {str(state): count for state, count in rows}


class EvidenceRepository:
    """Text spans and table cells. Append-only within a completed stage.

    A stage re-run replaces *its own* output for one document version in a single
    transaction (which is what makes retries idempotent); nothing else ever
    modifies an evidence row.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def insert(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert spans. Ids are minted when absent.

        SQLAlchemy compiles one ``executemany`` statement from the first mapping,
        so a batch whose dicts have different keys — a table cell carrying
        ``cell_ref`` beside a bare text span that does not — fails on the second
        row. Callers describe each span with only the fields it has, which is the
        natural way to write an extractor.

        So the batch is grouped by key signature and inserted one group per
        statement. That keeps ``executemany`` (a 400-page document yields tens of
        thousands of spans, and a round trip each is the difference between
        seconds and minutes) while letting an absent column fall to its server
        default — rather than padding with an explicit ``NULL``, which is how
        this project previously broke a ``NOT NULL`` column that had a perfectly
        good ``DEFAULT``.

        An explicit ``evidence_id`` is honoured, which is what lets
        :meth:`replace_stage` reproduce byte-identical rows on a retry.
        """
        batch = list(rows)
        if not batch:
            return 0

        known = {column.name for column in evidence.columns}
        unknown = {key for row in batch for key in row} - known
        if unknown:
            # Silently dropping a misspelled field would lose evidence without
            # telling anyone — the one failure mode this store must not have.
            raise ValueError(f"Unknown evidence column(s): {', '.join(sorted(unknown))}")

        groups: dict[frozenset[str], list[dict[str, Any]]] = {}
        for row in batch:
            prepared = (
                row if "evidence_id" in row else {**row, "evidence_id": new_id("ev")}
            )
            groups.setdefault(frozenset(prepared), []).append(prepared)

        for group in groups.values():
            self._conn.execute(sa.insert(evidence), group)
        return len(batch)

    def replace_stage(
        self,
        document_id: str,
        document_version: int,
        extraction_method: str,
        rows: Iterable[dict[str, Any]],
    ) -> int:
        """Idempotent stage write, keyed on ``(version, extraction_method)``.

        Delete-then-insert inside the caller's transaction, so a retry after a
        crash produces the same rows rather than a second copy of them. This is
        the mechanism behind the Phase 1 gate: kill a worker mid-ingest and the
        resumed run is byte-identical.
        """
        self._conn.execute(
            sa.delete(evidence).where(
                evidence.c.document_id == document_id,
                evidence.c.document_version == document_version,
                evidence.c.extraction_method == extraction_method,
            )
        )
        return self.insert(rows)

    def for_page(self, document_id: str, page: int, scope: Scope) -> list[dict[str, Any]]:
        """Every span on one page, ordered top-to-bottom then left-to-right.

        Scoped through the parent document — a page is as sensitive as its source.
        """
        query = (
            sa.select(evidence)
            .join(documents, documents.c.document_id == evidence.c.document_id)
            .where(
                evidence.c.document_id == document_id,
                evidence.c.page == page,
                scope.clause(documents.c.owner_entity_id),
            )
            .order_by(
                # NULLS LAST: a span with no geometry (a spreadsheet cell) sorts
                # after the positioned ones rather than jumping to the top.
                sa.nullslast(evidence.c.bbox_y0),
                sa.nullslast(evidence.c.bbox_x0),
                evidence.c.evidence_id,
            )
        )
        rows = self._conn.execute(query).mappings().all()
        return [dict(row) for row in rows]

    def for_document(
        self,
        document_id: str,
        document_version: int,
        *,
        kinds: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Every span of a document version, ordered for reconstruction.

        Unscoped, and pipeline-internal: the caller is a job that was enqueued
        because this document was accepted, not a person who can see it. Ordered
        by page, then table, then row and column, so a consumer can rebuild a
        table by iterating rather than by sorting.
        """
        query = sa.select(evidence).where(
            evidence.c.document_id == document_id,
            evidence.c.document_version == document_version,
        )
        if kinds:
            query = query.where(evidence.c.kind.in_(list(kinds)))
        rows = (
            self._conn.execute(
                query.order_by(
                    sa.nullslast(evidence.c.page),
                    sa.nullslast(evidence.c.table_id),
                    sa.nullslast(evidence.c.row_idx),
                    sa.nullslast(evidence.c.col_idx),
                    # Geometry before id: a text line has no row or column, so
                    # without this the order of lines on a page would fall back
                    # to a random id — and two runs of the same stage would
                    # return the same rows in a different order, which is not
                    # what "idempotent" should mean to a caller.
                    sa.nullslast(evidence.c.bbox_y0),
                    sa.nullslast(evidence.c.bbox_x0),
                    evidence.c.evidence_id,
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def page_text(
        self, document_id: str, document_version: int
    ) -> dict[int | None, list[str]]:
        """Text lines grouped by page. Pipeline-internal.

        The extractor uses this to find a caption near a table — the unit
        ("Figures in lakh tonnes") and often the metric itself — which for a CIL
        table is the only place either is written.

        **``None`` is a real key, not a dropped row.** A Word document has no
        pages (Word computes pagination at render time), so its lines and its
        table cells both carry a null page. Discarding them here is what made a
        .docx produce zero facts while its PDF twin produced six.
        """
        rows = self._conn.execute(
            sa.select(evidence.c.page, evidence.c.text)
            .where(
                evidence.c.document_id == document_id,
                evidence.c.document_version == document_version,
                evidence.c.kind == "line",
                evidence.c.text.is_not(None),
            )
            .order_by(sa.nullslast(evidence.c.page), sa.nullslast(evidence.c.bbox_y0))
        ).all()

        pages: dict[int | None, list[str]] = {}
        for page, line in rows:
            pages.setdefault(page, []).append(line)
        return pages

    def search(
        self,
        query: str,
        scope: Scope,
        *,
        limit: int = 20,
        document_id: str | None = None,
        kinds: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search over evidence, scoped to what the caller may read.

        Three decisions worth stating.

        **``websearch_to_tsquery``, not ``to_tsquery``.** The input is whatever a
        reviewer typed. ``to_tsquery`` raises a syntax error on an unbalanced
        quote or a stray ampersand — turning a search box into a source of 500s —
        while ``websearch_to_tsquery`` accepts the Google-ish syntax people
        already use (quoted phrases, ``or``, a leading ``-`` to exclude) and never
        raises.

        **The snippet comes from PostgreSQL, with markers rather than HTML.**
        ``ts_headline`` knows which lexemes matched, including stemmed forms that
        a client-side highlighter would miss. The markers are ``[[`` and ``]]``
        rather than ``<mark>`` because this string is rendered by React: handing
        the browser HTML from the database is how a stored payload becomes an
        injection.

        **Ranked by ``ts_rank_cd``, which accounts for term proximity.** For a
        query like "SECL production 2024" the row where those three words sit
        together is the one worth reading, and plain ``ts_rank`` cannot tell.
        """
        if not query.strip():
            return []

        tsquery = sa.func.websearch_to_tsquery("english", query)
        rank = sa.func.ts_rank_cd(evidence.c.search_vector, tsquery)
        headline = sa.func.ts_headline(
            "english",
            sa.func.coalesce(evidence.c.text, ""),
            tsquery,
            sa.literal(
                "StartSel=[[, StopSel=]], MaxWords=28, MinWords=8, MaxFragments=2"
            ),
        )

        statement = (
            sa.select(
                evidence.c.evidence_id,
                evidence.c.document_id,
                evidence.c.document_version,
                evidence.c.page,
                evidence.c.kind,
                evidence.c.table_id,
                evidence.c.cell_ref,
                evidence.c.text,
                evidence.c.extraction_method,
                evidence.c.ocr_confidence,
                documents.c.filename,
                documents.c.title,
                documents.c.fiscal_year,
                documents.c.publisher_entity_id,
                headline.label("snippet"),
                rank.label("rank"),
            )
            .select_from(
                evidence.join(
                    documents, documents.c.document_id == evidence.c.document_id
                )
            )
            .where(
                evidence.c.search_vector.op("@@")(tsquery),
                scope.clause(documents.c.owner_entity_id),
            )
            .order_by(rank.desc(), evidence.c.document_id, evidence.c.page)
            .limit(limit)
        )
        if document_id is not None:
            statement = statement.where(evidence.c.document_id == document_id)
        if kinds:
            statement = statement.where(evidence.c.kind.in_(list(kinds)))

        return [dict(row) for row in self._conn.execute(statement).mappings().all()]

    def count(self, scope: Scope) -> int:
        """Total spans visible under this scope.

        Joined through ``documents`` rather than counted directly: an unscoped
        count would tell an SECL officer how much evidence exists across every
        other subsidiary, which is a smaller leak than reading the rows but a
        leak all the same.
        """
        return (
            self._conn.execute(
                sa.select(sa.func.count())
                .select_from(
                    evidence.join(
                        documents, documents.c.document_id == evidence.c.document_id
                    )
                )
                .where(scope.clause(documents.c.owner_entity_id))
            ).scalar()
            or 0
        )
