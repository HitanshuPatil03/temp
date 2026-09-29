"""The store facade.

Composes the repositories behind the method surface the API layer already uses,
so the routers and the existing test assertions survive the move off DuckDB with
a scope argument added rather than a rewrite (ARCHITECTURE §12).

A ``Store`` is bound to one **transaction**, not to a process. That is the change
that matters: the DuckDB version held a single long-lived connection because it
had to, and every caller shared it. Here each request and each job gets its own
transaction, which is what makes concurrent reviewers and workers safe.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from mrip.auth.scope import Scope
from mrip.config import Settings, get_settings
from mrip.db.engine import read_only, transaction
from mrip.db.repositories.conflicts import ConflictRepository
from mrip.db.repositories.documents import (
    DocumentRepository,
    EvidenceRepository,
    new_id,
)
from mrip.db.repositories.facts import FactRepository
from mrip.db.repositories.users import AuditRepository, UserRepository
from mrip.db.tables import METADATA
from mrip.schemas import ConflictGroup, Document, DocumentState, Fact, FactStatus

__all__ = ["Store", "new_id", "read_only_store", "store_session"]


class Store:
    """Evidence, facts and conflicts, over one transaction."""

    def __init__(self, conn: Connection, settings: Settings | None = None) -> None:
        self._conn = conn
        self._settings = settings or get_settings()
        self.documents = DocumentRepository(conn)
        self.evidence = EvidenceRepository(conn)
        self.facts = FactRepository(conn)
        self.conflicts = ConflictRepository(conn)
        self.users = UserRepository(conn)
        self.audit = AuditRepository(conn)

    @property
    def settings(self) -> Settings:
        """The settings this store was built with — thresholds, mostly.

        Exposed so a job handler can read a threshold without reaching for the
        process-wide singleton, which a test would then have to patch globally.
        """
        return self._settings

    @property
    def connection(self) -> Connection:
        """For callers that need to join their own write into this transaction —
        the ingestion state machine does, so a stage's output and its state
        transition commit together."""
        return self._conn

    # ------------------------------------------------------------- documents

    def find_document_by_hash(self, content_hash: str) -> Document | None:
        return self.documents.find_by_hash(content_hash)

    def register_document(self, document: Document) -> Document:
        return self.documents.register(document)

    def get_document(self, document_id: str, scope: Scope) -> Document | None:
        return self.documents.get(document_id, scope)

    def list_documents(
        self,
        scope: Scope,
        *,
        limit: int = 100,
        offset: int = 0,
        state: DocumentState | None = None,
    ) -> list[Document]:
        return self.documents.list(scope, limit=limit, offset=offset, state=state)

    def mark_superseded(self, old_document_id: str, new_document_id: str) -> int:
        return self.documents.mark_superseded(old_document_id, new_document_id)

    # -------------------------------------------------------------- evidence

    def insert_evidence(self, rows: Iterable[dict[str, Any]]) -> int:
        return self.evidence.insert(rows)

    def page_evidence(
        self, document_id: str, page: int, scope: Scope
    ) -> list[dict[str, Any]]:
        return self.evidence.for_page(document_id, page, scope)

    # ----------------------------------------------------------------- facts

    def insert_facts(self, batch: Sequence[Fact]) -> int:
        return self.facts.insert(batch)

    def get_fact(self, fact_id: str, scope: Scope) -> Fact | None:
        return self.facts.get(fact_id, scope)

    def query_facts(
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
        return self.facts.query(
            scope,
            entity_id=entity_id,
            metric=metric,
            fiscal_year=fiscal_year,
            document_id=document_id,
            status=status,
            include_inactive=include_inactive,
            limit=limit,
        )

    def set_fact_status(self, fact_ids: Sequence[str], status: FactStatus) -> int:
        return self.facts.set_status(fact_ids, status)

    def flag_low_confidence(self, threshold: float | None = None) -> int:
        return self.facts.flag_low_confidence(
            self._settings.review_confidence_threshold if threshold is None else threshold
        )

    def entity_metric_series(
        self, metric: str, scope: Scope, *, unit: str | None = None
    ) -> list[dict[str, Any]]:
        return self.facts.entity_metric_series(metric, scope, unit=unit)

    # ------------------------------------------------------------- conflicts

    def detect_conflicts(
        self, scope: Scope, *, material_spread: float | None = None
    ) -> list[ConflictGroup]:
        return self.conflicts.detect(
            self._settings.conflict_material_spread
            if material_spread is None
            else material_spread,
            scope,
        )

    def open_conflicts(self, scope: Scope) -> list[ConflictGroup]:
        return self.conflicts.open(scope)

    def resolve_conflict(
        self,
        conflict_id: str,
        winning_fact_id: str,
        scope: Scope,
        *,
        note: str | None = None,
        actor_user_id: str | None = None,
    ) -> bool:
        return self.conflicts.resolve(
            conflict_id, winning_fact_id, note, scope, actor_user_id=actor_user_id
        )

    # --------------------------------------------------------------- summary

    def summary(self, scope: Scope) -> dict[str, Any]:
        """Headline counts for the dashboard.

        Scoped like everything else: an SECL officer's dashboard counts SECL's
        corpus. A total that silently includes rows the viewer may not open would
        leak the size of another subsidiary's data.
        """
        counts = self.facts.counts(scope)
        docs = self.facts.document_counts(scope)
        return {
            "documents": docs["documents"],
            "pages": docs["pages"],
            "evidence_spans": self.evidence.count(scope),
            "facts": counts["facts"],
            "facts_validated": counts["facts_validated"],
            "facts_needs_review": counts["facts_needs_review"],
            "open_conflicts": self.conflicts.count_open(scope),
            "entities": counts["entities"],
            "metrics": counts["metrics"],
        }

    # ----------------------------------------------------------- maintenance

    def reset(self) -> None:
        """Empty every table. Test support only.

        ``TRUNCATE … CASCADE`` rather than per-table ``DELETE`` so foreign keys do
        not dictate an ordering, and ``ALTER TABLE … DISABLE TRIGGER`` because the
        audit log's append-only trigger blocks ``TRUNCATE`` by design — the one
        legitimate exception is tearing down a throwaway test database.
        """
        if self._settings.is_production:
            raise RuntimeError("Store.reset() is not available in production.")
        names = ", ".join(f'"{table}"' for table in reversed(METADATA.sorted_tables))
        self._conn.execute(sa.text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        try:
            self._conn.execute(sa.text(f"TRUNCATE {names} CASCADE"))
        finally:
            self._conn.execute(sa.text("ALTER TABLE audit_log ENABLE TRIGGER USER"))


@contextmanager
def store_session(settings: Settings | None = None) -> Iterator[Store]:
    """A store over a read-write transaction: commits on success."""
    with transaction() as conn:
        yield Store(conn, settings)


@contextmanager
def read_only_store(settings: Settings | None = None) -> Iterator[Store]:
    """A store that cannot write, enforced by the database.

    The exact-figure query path uses this, which is how "this route only reads"
    stops being a claim about intent (ARCHITECTURE §7).
    """
    with read_only() as conn:
        yield Store(conn, settings)
