"""The database schema, as SQLAlchemy Core tables.

Core rather than the ORM, deliberately. The queries this system runs are explicit
aggregate SQL — production summed per subsidiary per fiscal year, conflict groups
formed by a ``HAVING`` on relative spread — and an identity map buys nothing for an
append-only evidence store. Writing the SQL means being able to read it.

Three properties of this schema are load-bearing:

**Evidence is append-only.** Nothing in the application layer updates or deletes an
``evidence`` row. Superseded document versions stay queryable, which is what makes
"what did the FY23 report say before it was revised?" answerable.

**A fact cannot point at a document version that does not exist.** ``facts`` and
``evidence`` both carry a composite foreign key to ``documents(document_id,
version)``. The invariant that every figure is traceable is enforced by the
database, not by the care of whoever wrote the insert.

**Conflicts are a join table, not an array.** The DuckDB version stored
``fact_ids VARCHAR[]``, which cannot be constrained. Here a conflict's membership
is ``conflict_facts`` rows with a real foreign key, so a conflict can never
reference a fact that was removed.
"""

from __future__ import annotations

import re
from enum import Enum as PyEnum

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from mrip.schemas import (
    AuthSource,
    DocumentClass,
    DocumentState,
    ExtractionMethod,
    FactStatus,
    JobState,
    ReportState,
    ReviewState,
    Role,
    Sensitivity,
)

__all__ = [
    "METADATA",
    "audit_log",
    "conflict_facts",
    "conflicts",
    "document_keyphrases",
    "documents",
    "evidence",
    "facts",
    "jobs",
    "reports",
    "user_scopes",
    "users",
]

#: Deterministic constraint names. Without this, Alembic generates names from the
#: database's own defaults, which differ between backends and make a forward-only
#: migration history unreadable — you cannot drop a constraint you cannot name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

METADATA = sa.MetaData(naming_convention=NAMING_CONVENTION)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def enum_col(py_enum: type[PyEnum]) -> sa.Enum:
    """A ``VARCHAR`` + ``CHECK`` column constrained to a Python enum's *values*.

    Not a native PostgreSQL ``ENUM`` type: altering one requires ``ALTER TYPE`` and
    cannot be done inside a transaction on older servers, which makes adding a
    lifecycle state a migration hazard for no benefit.

    ``values_callable`` matters more than it looks. SQLAlchemy persists a PEP-435
    enum by its *name* by default, so :class:`FactStatus.NEEDS_REVIEW` would be
    stored as ``NEEDS_REVIEW`` while every Pydantic model, API response and test
    fixture says ``needs_review``. Passing the values explicitly keeps one spelling
    from the database to the browser.
    """
    return sa.Enum(
        py_enum,
        name=_snake(py_enum.__name__),
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


#: Timezone-aware throughout. A system whose whole job is fiscal-period arithmetic
#: cannot afford a naive timestamp: "2026-04-01 00:00" is in a different fiscal
#: year depending on the reader's assumption about the zone.
TS = sa.DateTime(timezone=True)

_ID = sa.Text
_NOW = sa.func.now()


# --------------------------------------------------------------------- documents

#: A registered source document *version*. Each row is one version; the revision
#: chain is the ``supersedes`` self-reference. Identified by content hash, so
#: re-uploading identical bytes is a no-op rather than a duplicate.
documents = sa.Table(
    "documents",
    METADATA,
    sa.Column("document_id", _ID, primary_key=True),
    sa.Column("content_hash", sa.Text, nullable=False),
    #: Where the bytes live in the content-addressed blob store. The source
    #: document *is* the evidence, so this is retained for the corpus's lifetime.
    sa.Column("blob_key", sa.Text, nullable=True),
    sa.Column("filename", sa.Text, nullable=False),
    sa.Column("doc_class", enum_col(DocumentClass), nullable=False),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column(
        "supersedes",
        _ID,
        sa.ForeignKey("documents.document_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column("page_count", sa.Integer, nullable=True),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("title", sa.Text, nullable=True),
    sa.Column("publisher_entity_id", sa.Text, nullable=True),
    sa.Column("fiscal_year", sa.Text, nullable=True),
    sa.Column("ingested_at", TS, nullable=False, server_default=_NOW),
    sa.Column(
        "is_synthetic", sa.Boolean, nullable=False, server_default=sa.text("false")
    ),
    sa.Column("notes", sa.Text, nullable=True),
    # Where a document we did not receive from a person came from — the URL a
    # corpus fetch downloaded it from. Null for an ordinary upload, whose chain
    # of custody ends at the uploader's account. Kept separate from
    # `is_synthetic`, which answers a different question: whether *we* made the
    # document up. A file fetched from coal.gov.in is neither an upload nor a
    # generated table, and conflating those in either direction is the mistake
    # that matters.
    sa.Column("source_url", sa.Text, nullable=True),
    # ---- production lifecycle (ARCHITECTURE §6) ----
    sa.Column(
        "state",
        enum_col(DocumentState),
        nullable=False,
        server_default=sa.text("'received'"),
    ),
    sa.Column(
        "sensitivity",
        enum_col(Sensitivity),
        nullable=False,
        server_default=sa.text("'internal'"),
    ),
    #: Which stage failed, and why, kept rather than logged — a document that
    #: failed OCR is a work item, not a log line.
    sa.Column("failed_stage", sa.Text, nullable=True),
    sa.Column("failed_reason", sa.Text, nullable=True),
    #: Per-stage counters, so the UI shows "OCR 142/400" instead of a spinner.
    sa.Column(
        "stage_progress",
        pg.JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("owner_entity_id", sa.Text, nullable=True),
    sa.Column(
        "uploaded_by",
        _ID,
        sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("updated_at", TS, nullable=False, server_default=_NOW),
    # Evidence and facts both foreign-key to this pair.
    sa.UniqueConstraint("document_id", "version", name="document_version"),
    sa.CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
    sa.CheckConstraint("version >= 1", name="version_positive"),
    sa.CheckConstraint(
        "page_count IS NULL OR page_count >= 0", name="page_count_nonnegative"
    ),
    # A failed document must say why. Prevents a silent dead end in the queue.
    sa.CheckConstraint(
        "state <> 'failed' OR failed_stage IS NOT NULL",
        name="failure_names_its_stage",
    ),
    sa.Index("ix_documents_state", "state"),
    sa.Index("ix_documents_owner_entity", "owner_entity_id"),
    sa.Index("ix_documents_fiscal_year", "fiscal_year"),
    # Partial index on the non-null source URLs only. Almost every document in a
    # real deployment is an upload with a null here; only the fetched-corpus rows
    # are looked up by URL, so indexing just those is a fraction of the size.
    # Declared here to match migration 0004 — the metadata and the migrations
    # must agree, or the drift gate fails.
    sa.Index(
        "ix_documents_source_url",
        "source_url",
        postgresql_where=sa.text("source_url IS NOT NULL"),
    ),
    # Unique on the *lowered* hash, not on the raw column. Different tools print
    # SHA-256 digests in different cases, and a plain unique constraint would
    # happily accept both spellings of one file — double-counting every fact in
    # it. This index is also what makes the case-insensitive dedup lookup in
    # DocumentRepository.find_by_hash an index scan rather than a full scan.
    sa.Index(
        "uq_documents_content_hash_lower",
        sa.func.lower(sa.column("content_hash")),
        unique=True,
    ),
)


# ---------------------------------------------------------------------- evidence

#: One row per text span or table cell. **Append-only**: no application code path
#: updates or deletes these, which is what keeps a superseded version's reading of
#: a figure recoverable years later.
evidence = sa.Table(
    "evidence",
    METADATA,
    sa.Column("evidence_id", _ID, primary_key=True),
    sa.Column("document_id", _ID, nullable=False),
    sa.Column(
        "document_version", sa.Integer, nullable=False, server_default=sa.text("1")
    ),
    sa.Column("page", sa.Integer, nullable=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("table_id", sa.Text, nullable=True),
    sa.Column("cell_ref", sa.Text, nullable=True),
    sa.Column("row_idx", sa.Integer, nullable=True),
    sa.Column("col_idx", sa.Integer, nullable=True),
    sa.Column("bbox_x0", sa.Double, nullable=True),
    sa.Column("bbox_y0", sa.Double, nullable=True),
    sa.Column("bbox_x1", sa.Double, nullable=True),
    sa.Column("bbox_y1", sa.Double, nullable=True),
    sa.Column("text", sa.Text, nullable=True),
    #: Lexical search vector. A **generated** column rather than something a
    #: stage maintains: a search index that can drift out of step with its rows
    #: is a search index that quietly stops finding recent documents, and there
    #: is no way to notice. PostgreSQL recomputes this on every write, for free.
    sa.Column(
        "search_vector",
        pg.TSVECTOR,
        sa.Computed("to_tsvector('english', coalesce(text, ''))", persisted=True),
        nullable=True,
    ),
    sa.Column("ocr_confidence", sa.Double, nullable=True),
    # Whether this row's text came off a PDF text layer that is itself OCR output.
    #
    # Distinct from ``ocr_confidence``, and it has to be: NULL there means "native
    # text, no recogniser involved, trustworthy", so it cannot also mean
    # "recognised by whoever produced the PDF, confidence unknowable". A cell from
    # such a page has real geometry — the grid is the publisher's — while its
    # glyphs may read ``635`` where the paper says ``63.5``. Readers that verify a
    # figure against the page may use these; readers that cannot must decline.
    sa.Column(
        "text_layer_recognised",
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("extraction_method", enum_col(ExtractionMethod), nullable=False),
    sa.Column("created_at", TS, nullable=False, server_default=_NOW),
    sa.ForeignKeyConstraint(
        ["document_id", "document_version"],
        ["documents.document_id", "documents.version"],
        name="document_version",
        # RESTRICT, not CASCADE: deleting a document version out from under its
        # evidence is exactly the data loss this schema exists to prevent.
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)",
        name="ocr_confidence_is_probability",
    ),
    sa.CheckConstraint("page IS NULL OR page >= 1", name="page_positive"),
    sa.Index("ix_evidence_doc_page", "document_id", "page"),
    sa.Index("ix_evidence_table", "document_id", "table_id"),
    # Partial: "show me every figure that came off an OCR text layer" is a review
    # query over the minority of rows, and should not cost a scan of the rest.
    sa.Index(
        "ix_evidence_recognised_layer",
        "document_id",
        "page",
        postgresql_where=sa.text("text_layer_recognised"),
    ),
    # GIN over the generated vector: the index that makes "which document said
    # 193 million tonnes" answerable across a corpus rather than per document.
    sa.Index("ix_evidence_search", "search_vector", postgresql_using="gin"),
)


# ------------------------------------------------------------------------- facts

#: A normalized, evidence-backed numeric fact. Both the canonical value and the
#: value as literally printed are stored: canonical makes comparison possible, raw
#: keeps the receipt honest.
facts = sa.Table(
    "facts",
    METADATA,
    sa.Column("fact_id", _ID, primary_key=True),
    sa.Column("entity_id", sa.Text, nullable=False),
    sa.Column("mine_or_block", sa.Text, nullable=True),
    sa.Column("metric", sa.Text, nullable=False),
    sa.Column("value", sa.Double, nullable=False),
    sa.Column("unit", sa.Text, nullable=False),
    sa.Column("dimension", sa.Text, nullable=False),
    sa.Column("raw_value", sa.Double, nullable=False),
    sa.Column("raw_unit", sa.Text, nullable=False),
    sa.Column("period_start", sa.Date, nullable=False),
    sa.Column("period_end", sa.Date, nullable=False),
    sa.Column("period_label", sa.Text, nullable=False),
    sa.Column("fiscal_year", sa.Text, nullable=True),
    # ---- evidence ref, flattened ----
    sa.Column("document_id", _ID, nullable=False),
    sa.Column(
        "document_version", sa.Integer, nullable=False, server_default=sa.text("1")
    ),
    sa.Column("page", sa.Integer, nullable=True),
    sa.Column("table_id", sa.Text, nullable=True),
    sa.Column("cell_ref", sa.Text, nullable=True),
    sa.Column("bbox_x0", sa.Double, nullable=True),
    sa.Column("bbox_y0", sa.Double, nullable=True),
    sa.Column("bbox_x1", sa.Double, nullable=True),
    sa.Column("bbox_y1", sa.Double, nullable=True),
    sa.Column("snippet", sa.Text, nullable=True),
    sa.Column("extraction_method", enum_col(ExtractionMethod), nullable=False),
    # ---- decomposed confidence: three columns, never one ----
    sa.Column("conf_ocr", sa.Double, nullable=True),
    sa.Column("conf_parse", sa.Double, nullable=True),
    sa.Column("conf_answer", sa.Double, nullable=True),
    sa.Column("status", enum_col(FactStatus), nullable=False),
    sa.Column("review_state", enum_col(ReviewState), nullable=False),
    sa.Column(
        "unit_ambiguous", sa.Boolean, nullable=False, server_default=sa.text("false")
    ),
    sa.Column("notes", sa.Text, nullable=True),
    sa.Column("extracted_at", TS, nullable=False, server_default=_NOW),
    sa.ForeignKeyConstraint(
        ["document_id", "document_version"],
        ["documents.document_id", "documents.version"],
        name="document_version",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint("period_end >= period_start", name="period_ordered"),
    sa.CheckConstraint(
        "(conf_ocr    IS NULL OR (conf_ocr    BETWEEN 0 AND 1)) AND "
        "(conf_parse  IS NULL OR (conf_parse  BETWEEN 0 AND 1)) AND "
        "(conf_answer IS NULL OR (conf_answer BETWEEN 0 AND 1))",
        name="confidences_are_probabilities",
    ),
    # The series endpoint's shape: entity × metric × fiscal year.
    sa.Index("ix_facts_lookup", "entity_id", "metric", "fiscal_year"),
    sa.Index("ix_facts_document", "document_id"),
    sa.Index("ix_facts_status", "status"),
    # The conflict radar groups on exactly this key.
    sa.Index("ix_facts_conflict_key", "entity_id", "metric", "unit", "period_label"),
)


# --------------------------------------------------------------------- conflicts

#: Two or more facts describing the same measurement that disagree. Surfaced side
#: by side and **never merged** — resolution names a winner and records who chose.
conflicts = sa.Table(
    "conflicts",
    METADATA,
    sa.Column("conflict_id", _ID, primary_key=True),
    sa.Column("entity_id", sa.Text, nullable=False),
    sa.Column("metric", sa.Text, nullable=False),
    sa.Column("unit", sa.Text, nullable=False),
    sa.Column("period_label", sa.Text, nullable=False),
    sa.Column("value_low", sa.Double, nullable=False),
    sa.Column("value_high", sa.Double, nullable=False),
    sa.Column("detected_at", TS, nullable=False, server_default=_NOW),
    sa.Column(
        "resolved_fact_id",
        _ID,
        sa.ForeignKey("facts.fact_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column("resolution_note", sa.Text, nullable=True),
    #: Who adjudicated. A resolution with no author is not an audit trail.
    sa.Column(
        "resolved_by",
        _ID,
        sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("resolved_at", TS, nullable=True),
    sa.CheckConstraint("value_high >= value_low", name="values_ordered"),
    sa.CheckConstraint(
        "(resolved_fact_id IS NULL) = (resolved_at IS NULL)",
        name="resolution_is_all_or_nothing",
    ),
    # One open conflict per measurement key; re-running detection updates it
    # rather than stacking duplicates.
    sa.Index(
        "ix_conflicts_open_key",
        "entity_id",
        "metric",
        "unit",
        "period_label",
        unique=True,
        postgresql_where=sa.text("resolved_fact_id IS NULL"),
    ),
)


#: Conflict membership. A join table rather than an array column, so a conflict
#: cannot reference a fact that no longer exists.
conflict_facts = sa.Table(
    "conflict_facts",
    METADATA,
    sa.Column(
        "conflict_id",
        _ID,
        sa.ForeignKey("conflicts.conflict_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "fact_id",
        _ID,
        sa.ForeignKey("facts.fact_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Index("ix_conflict_facts_fact", "fact_id"),
)


# ------------------------------------------------------------------------- users

users = sa.Table(
    "users",
    METADATA,
    sa.Column("user_id", _ID, primary_key=True),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("email", sa.Text, nullable=True),
    sa.Column("display_name", sa.Text, nullable=True),
    #: Null for OIDC and LDAP principals — their credentials are never held here.
    sa.Column("password_hash", sa.Text, nullable=True),
    sa.Column(
        "auth_source",
        enum_col(AuthSource),
        nullable=False,
        server_default=sa.text("'local'"),
    ),
    sa.Column("role", enum_col(Role), nullable=False, server_default=sa.text("'viewer'")),
    sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
    #: Forces a password change before anything else is readable. Set on every
    #: admin-created account, because a temporary password an administrator knows
    #: is not a credential the account holder owns yet.
    sa.Column(
        "must_change_password",
        sa.Boolean,
        nullable=False,
        server_default=sa.text("false"),
    ),
    #: Bumped whenever credentials or status change. Access tokens carry the epoch
    #: they were issued under, so changing a password or disabling an account
    #: invalidates tokens already out there instead of waiting for them to expire.
    sa.Column("session_epoch", sa.Integer, nullable=False, server_default=sa.text("1")),
    #: Consecutive failed logins, and the lockout they earned. Counted in the
    #: database rather than in process memory so two API replicas cannot each
    #: grant a fresh allowance of guesses.
    sa.Column(
        "failed_login_count", sa.Integer, nullable=False, server_default=sa.text("0")
    ),
    sa.Column("locked_until", TS, nullable=True),
    sa.Column("created_at", TS, nullable=False, server_default=_NOW),
    sa.Column("last_login_at", TS, nullable=True),
    sa.CheckConstraint(
        "auth_source <> 'local' OR password_hash IS NOT NULL",
        name="local_accounts_have_a_hash",
    ),
    #: Usernames are stored lowercased, so "S.Kumar" and "s.kumar" cannot become
    #: two accounts with two different scopes. Enforced here rather than only in
    #: the repository, because a CLI or a migration writes this table too.
    sa.CheckConstraint("username = lower(username)", name="usernames_are_lowercased"),
)


#: Which entities a principal may see. Scope is row-level, so it lives in its own
#: table rather than as a column on ``users``: an officer may cover two
#: subsidiaries, and HQ covers all of them (represented by the sentinel below).
user_scopes = sa.Table(
    "user_scopes",
    METADATA,
    sa.Column(
        "user_id",
        _ID,
        sa.ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("entity_id", sa.Text, primary_key=True),
    sa.Column("granted_at", TS, nullable=False, server_default=_NOW),
    sa.Column(
        "granted_by",
        _ID,
        sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    ),
)

#: Scope sentinel meaning "every entity". Stored as a row like any other grant so
#: that revoking HQ access is the same operation as revoking any other.
SCOPE_ALL = "*"


# --------------------------------------------------------------------- audit log

#: Append-only. ``UPDATE`` and ``DELETE`` are revoked at the role level in the
#: migration, not merely avoided by convention — an audit log the application can
#: rewrite is not an audit log.
audit_log = sa.Table(
    "audit_log",
    METADATA,
    sa.Column("audit_id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("occurred_at", TS, nullable=False, server_default=_NOW),
    sa.Column(
        "actor_user_id",
        _ID,
        sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    ),
    #: Denormalized copy of the username, so the trail stays readable after an
    #: account is removed.
    sa.Column("actor_username", sa.Text, nullable=True),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("subject_type", sa.Text, nullable=True),
    sa.Column("subject_id", sa.Text, nullable=True),
    sa.Column("entity_scope", sa.Text, nullable=True),
    sa.Column("detail", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("request_id", sa.Text, nullable=True),
    sa.Column("source_ip", sa.Text, nullable=True),
    sa.Index("ix_audit_log_occurred_at", "occurred_at"),
    sa.Index("ix_audit_log_actor", "actor_user_id", "occurred_at"),
    sa.Index("ix_audit_log_subject", "subject_type", "subject_id"),
)


# ---------------------------------------------------------------------- reports

#: A generated report, and the manifest that pins its evidence (ARCHITECTURE
#: §8.5, §11.3). The manifest is stored whole as JSONB rather than shredded into
#: figure rows: it is read back as one document, it is never queried field by
#: field, and keeping it verbatim is what lets a two-year-old report re-render
#: *exactly* as approved. The columns beside it are the ones worth indexing —
#: scope, lifecycle and authorship.
#:
#: ``entity_id`` is duplicated out of the manifest deliberately. Every read in
#: this system is scoped, and a scope filter cannot run against a JSONB field
#: without giving up the index.
reports = sa.Table(
    "reports",
    METADATA,
    sa.Column("report_id", _ID, primary_key=True),
    sa.Column("template_id", sa.Text, nullable=False),
    sa.Column("template_version", sa.Integer, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    #: What the report is *about* — the scope key. Not nullable: a report nobody
    #: can be scoped against is a report that leaks.
    sa.Column("entity_id", sa.Text, nullable=False),
    sa.Column("period_label", sa.Text, nullable=False),
    sa.Column(
        "state", enum_col(ReportState), nullable=False, server_default=sa.text("'draft'")
    ),
    #: The pinned-evidence manifest: every fact_id and document@version behind
    #: every figure. This is the reproducibility guarantee, stored.
    sa.Column("manifest", pg.JSONB, nullable=False),
    sa.Column("generated_at", TS, nullable=False, server_default=_NOW),
    sa.Column(
        "generated_by",
        _ID,
        sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    ),
    #: Who approved it. §11.3 — an approval with no approver is not an approval.
    sa.Column(
        "approved_by",
        _ID,
        sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("approved_at", TS, nullable=True),
    sa.Column("published_at", TS, nullable=True),
    sa.Column("updated_at", TS, nullable=False, server_default=_NOW),
    # An approval is a person and a time together, or neither. The same shape as
    # the conflict table's resolution check, for the same reason.
    sa.CheckConstraint(
        "(approved_by IS NULL) = (approved_at IS NULL)",
        name="approval_is_all_or_nothing",
    ),
    # Published and approved are not independent: nothing is published that was
    # not first approved by someone.
    sa.CheckConstraint(
        "published_at IS NULL OR approved_at IS NOT NULL",
        name="published_implies_approved",
    ),
    sa.CheckConstraint("template_version >= 1", name="template_version_positive"),
    sa.Index("ix_reports_entity_id", "entity_id"),
    sa.Index("ix_reports_state", "state"),
    sa.Index("ix_reports_generated_at", "generated_at"),
)


# ------------------------------------------------------------------ keyphrases

#: Deterministic keyphrases per document version (ARCHITECTURE §12.1).
#:
#: Stored rather than computed on read, so the word cloud is a table read and not
#: a scan of the whole evidence corpus — and so the same corpus produces the same
#: cloud twice, which is what lets a reviewer tell a data change from a code
#: change.
#:
#: ``score`` is TF-IDF and ranks terms *within* a document. The cloud does not
#: size by it: §12.2 sizes by how many documents use a term, which is a
#: ``COUNT(DISTINCT document_id)`` over this table. ``occurrences`` keeps the raw
#: count for the hover, because the two numbers answer different questions.
document_keyphrases = sa.Table(
    "document_keyphrases",
    METADATA,
    sa.Column("document_id", _ID, primary_key=True),
    sa.Column("document_version", sa.Integer, primary_key=True),
    sa.Column("term", sa.Text, primary_key=True),
    sa.Column("occurrences", sa.Integer, nullable=False),
    sa.Column("score", sa.Double, nullable=False),
    sa.Column("computed_at", TS, nullable=False, server_default=_NOW),
    sa.ForeignKeyConstraint(
        ["document_id", "document_version"],
        ["documents.document_id", "documents.version"],
        name="document_version",
        # CASCADE, unlike evidence and facts. A keyphrase is a derived index
        # entry, not evidence: it can be recomputed from the document at any
        # time, so it must not be the thing that blocks a deletion.
        ondelete="CASCADE",
    ),
    sa.CheckConstraint("occurrences >= 1", name="occurrences_positive"),
    sa.CheckConstraint("score >= 0", name="score_nonnegative"),
    # The cloud groups by term across documents; the drill-through goes the other
    # way, from a term to the documents that used it. Both are this index.
    sa.Index("ix_document_keyphrases_term", "term"),
)


# ----------------------------------------------------------------------- jobs

#: The work queue. Claimed with ``SELECT … FOR UPDATE SKIP LOCKED``, which is why
#: there is no Redis in this architecture: enqueueing a job is part of the same
#: transaction as the write that requires it, so a crash cannot leave a document
#: registered with nothing scheduled to process it.
jobs = sa.Table(
    "jobs",
    METADATA,
    sa.Column("job_id", _ID, primary_key=True),
    sa.Column("queue", sa.Text, nullable=False, server_default=sa.text("'default'")),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("payload", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column(
        "state", enum_col(JobState), nullable=False, server_default=sa.text("'pending'")
    ),
    #: Higher runs first. Interactive re-extraction should not queue behind a
    #: thousand-page backfill.
    sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("3")),
    sa.Column("available_at", TS, nullable=False, server_default=_NOW),
    sa.Column("claimed_at", TS, nullable=True),
    sa.Column("claimed_by", sa.Text, nullable=True),
    #: A claim is a lease. If a worker dies mid-job the lease expires and another
    #: worker may reclaim it — which is only safe because every stage is idempotent.
    sa.Column("lease_expires_at", TS, nullable=True),
    sa.Column("heartbeat_at", TS, nullable=True),
    sa.Column("finished_at", TS, nullable=True),
    sa.Column("error", sa.Text, nullable=True),
    #: Makes enqueueing idempotent: re-requesting "digitize version 3 of this
    #: document" collapses onto the existing job instead of duplicating the work.
    sa.Column("idempotency_key", sa.Text, nullable=True, unique=True),
    sa.Column("created_at", TS, nullable=False, server_default=_NOW),
    sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
    sa.CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    sa.CheckConstraint(
        "state <> 'claimed' OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)",
        name="claimed_jobs_have_a_lease",
    ),
    # The claim query's access path: pending work in one queue, oldest and
    # highest-priority first.
    sa.Index(
        "ix_jobs_claimable",
        "queue",
        sa.text("priority DESC"),
        "available_at",
        postgresql_where=sa.text("state = 'pending'"),
    ),
    # Reclaiming expired leases scans exactly these.
    sa.Index(
        "ix_jobs_expired_leases",
        "lease_expires_at",
        postgresql_where=sa.text("state = 'claimed'"),
    ),
    sa.Index("ix_jobs_state", "state"),
)
