"""Core domain models for MRIP.

These types are the contract between every layer: ingestion writes them, the fact
store persists them, the query router reads them, and the report generator renders
them. Two choices here are load-bearing and deliberate.

**1. A fact cannot exist without evidence.** :class:`Fact` requires an
:class:`EvidenceRef`. There is no code path that produces a figure with no source,
because the type system forbids it. That is the project's central invariant made
structural rather than aspirational.

**2. Confidence is never one number.** :class:`Confidence` keeps OCR, parser and
answer confidence as separate fields. The research report is explicit that
collapsing them into a single unexplained score is a failure mode: an OCR-perfect
page with a badly-inferred table header is not "80% confident", it is confident
about the characters and unreliable about the structure, and a reviewer needs to
see which.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Grouped by kind rather than alphabetised: a reader wants the enumerations
# and the models separated, which is more useful than sort order.
__all__ = [  # noqa: RUF022
    # enumerations
    "DocumentClass",
    "ExtractionMethod",
    "FactStatus",
    "ReviewState",
    "DocumentState",
    "Sensitivity",
    "Role",
    "AuthSource",
    "JobState",
    "QueryIntent",
    "RefusalReason",
    "ReportState",
    # models
    "BBox",
    "EvidenceRef",
    "Confidence",
    "Document",
    "Fact",
    "ConflictGroup",
    "SeriesPoint",
    "Passage",
    "FigureAnswer",
    "ComparisonAnswer",
    "DiscoveryAnswer",
    "NarrativeAnswer",
    "Refusal",
    "QueryRequest",
    "QueryResponse",
    "PinnedFigure",
    "MissingFigure",
    "ReportManifest",
    "FigureDelta",
    "CloudTerm",
    "TermDocument",
]

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class DocumentClass(StrEnum):
    """Input class, which selects the ingestion pipeline."""

    TEXT_PDF = "text_pdf"
    SCANNED_PDF = "scanned_pdf"
    MIXED_PDF = "mixed_pdf"
    SPREADSHEET = "spreadsheet"
    DOCX = "docx"
    IMAGE = "image"
    UNKNOWN = "unknown"


class ExtractionMethod(StrEnum):
    """How a value was obtained, recorded so accuracy can be measured per method."""

    PDF_TEXT_LAYER = "pdf_text_layer"
    OCR = "ocr"
    TABLE_LATTICE = "table_lattice"
    TABLE_STREAM = "table_stream"
    SPREADSHEET_CELL = "spreadsheet_cell"
    VLM = "vlm"
    MANUAL = "manual"


class FactStatus(StrEnum):
    """Lifecycle of a fact from extraction to trusted (or rejected)."""

    EXTRACTED = "extracted"
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    CONFLICTED = "conflicted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"

    @property
    def is_trustworthy(self) -> bool:
        """Whether a fact in this state may be used in a generated report."""
        return self is FactStatus.VALIDATED


class ReviewState(StrEnum):
    """Human adjudication state, tracked separately from extraction status."""

    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class DocumentState(StrEnum):
    """Where a document version sits in the ingestion state machine.

    Persisted rather than inferred, because the expensive stages must survive a
    worker restart and a half-finished document has to be distinguishable from a
    finished one. See ``docs/ARCHITECTURE.md`` §6.
    """

    RECEIVED = "received"
    CLASSIFIED = "classified"
    DIGITIZED = "digitized"
    EXTRACTED = "extracted"
    NORMALIZED = "normalized"
    VALIDATED = "validated"
    INDEXED = "indexed"
    READY = "ready"
    FAILED = "failed"
    #: Stopped at the boundary — encrypted, malformed, or an expansion bomb. Never
    #: handed to a worker.
    QUARANTINED = "quarantined"

    @property
    def is_terminal(self) -> bool:
        return self in {DocumentState.READY, DocumentState.QUARANTINED}


class Sensitivity(StrEnum):
    """Handling caveat recorded on a document, per the classification on the source.

    **This label is recorded and displayed, but it does not gate access yet.**
    Nothing reads it: not the scope clause, not a repository, not the query path.
    Every read is constrained by the caller's entity scope alone, so a document
    marked ``restricted`` is served to anyone whose grant covers its publisher.

    Saying so plainly is the point. The field previously claimed it "gates
    access, and which processing is permitted", and the upload form offered it as
    a dropdown, so an officer could mark a draft as restricted believing that
    did something — a false assurance about the one property a classification
    exists to provide.

    Enforcing it is a policy decision, not a bug fix: whether ``restricted``
    means admin-only, the uploader's entity, or a separate need-to-know grant is
    something CMPDI has to specify, and inventing an answer would be worse than
    having none. Tracked in ``docs/ROADMAP.md``. Until then the value is
    provenance — what the source document said about its own handling — and
    nothing should be built on the assumption that it is enforced.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class Role(StrEnum):
    """What a principal may do. Orthogonal to *which* entities they may see —
    scope is a separate, row-level concern (``user_scopes``)."""

    VIEWER = "viewer"
    OFFICER = "officer"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        order = [
            Role.VIEWER,
            Role.OFFICER,
            Role.REVIEWER,
            Role.APPROVER,
            Role.ADMIN,
        ]
        return order.index(self)

    def can(self, required: Role) -> bool:
        """Whether this role satisfies a minimum requirement."""
        return self.rank >= required.rank


class AuthSource(StrEnum):
    """Where a principal's credentials are verified."""

    LOCAL = "local"
    OIDC = "oidc"
    LDAP = "ldap"


class JobState(StrEnum):
    """Queue state. ``DEAD`` is a job that exhausted its attempts and is retained
    for inspection rather than dropped."""

    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


class BBox(BaseModel):
    """Bounding box in PDF points, origin top-left."""

    model_config = ConfigDict(frozen=True)

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _ordered(self) -> BBox:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bbox corners are out of order (x1<x0 or y1<y0)")
        return self


class EvidenceRef(BaseModel):
    """A pointer back to exactly where a value came from.

    This is what makes "click a number in the report, open the source page" work,
    and what a reviewer reads when adjudicating a conflict.
    """

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_version: int = Field(default=1, ge=1)
    page: int | None = Field(default=None, ge=1, description="1-indexed page number")
    table_id: str | None = None
    cell_ref: str | None = Field(
        default=None, description="Sheet cell ('C14') or table cell ('r3c2')"
    )
    bbox: BBox | None = None
    snippet: str | None = Field(
        default=None, max_length=1000, description="Verbatim source text, for the receipt"
    )

    @property
    def locator(self) -> str:
        """Compact human-readable citation, e.g. ``'doc:ab12cd v2 p.47 t1 r3c2'``."""
        parts = [f"doc:{self.document_id}"]
        if self.document_version > 1:
            parts.append(f"v{self.document_version}")
        if self.page is not None:
            parts.append(f"p.{self.page}")
        if self.table_id:
            parts.append(self.table_id)
        if self.cell_ref:
            parts.append(self.cell_ref)
        return " ".join(parts)


class Confidence(BaseModel):
    """Decomposed confidence. Deliberately not reducible to a single score.

    Each field is ``None`` when that stage did not apply — a spreadsheet cell has
    no OCR confidence, and an un-answered fact has no answer confidence.
    """

    model_config = ConfigDict(frozen=True)

    ocr: Probability | None = None
    parse: Probability | None = None
    answer: Probability | None = None

    @property
    def limiting(self) -> float | None:
        """The weakest applicable stage — a floor, not a blended score.

        Useful for thresholding a review queue. It is named ``limiting`` rather
        than ``overall`` on purpose: it tells you which stage is the bottleneck,
        it does not claim to summarise trustworthiness.
        """
        scores = [s for s in (self.ocr, self.parse, self.answer) if s is not None]
        return min(scores) if scores else None

    @property
    def limiting_stage(self) -> str | None:
        """Name of the weakest stage, so the UI can say *why* confidence is low."""
        staged = {"ocr": self.ocr, "parse": self.parse, "answer": self.answer}
        present = {k: v for k, v in staged.items() if v is not None}
        return min(present, key=lambda k: present[k]) if present else None


class Document(BaseModel):
    """A registered source document, identified by content hash.

    ``content_hash`` is the identity: re-uploading the same bytes is a no-op.
    ``supersedes`` builds the revision chain that makes "what did the earlier
    version say?" answerable, which the conflict radar depends on.
    """

    document_id: str
    content_hash: str = Field(min_length=64, max_length=64, description="SHA-256 hex")
    filename: str
    doc_class: DocumentClass
    version: int = Field(default=1, ge=1)
    supersedes: str | None = Field(
        default=None, description="document_id of the version this replaces"
    )
    page_count: int | None = Field(default=None, ge=0)
    size_bytes: int = Field(ge=0)
    title: str | None = None
    publisher_entity_id: str | None = None
    fiscal_year: str | None = None
    ingested_at: datetime
    is_synthetic: bool = Field(
        default=False,
        description="True for demo/test data. Surfaced in the UI so synthetic "
        "documents are never mistaken for authentic government sources.",
    )
    #: Where a document we did not receive from a person came from — the URL a
    #: corpus fetch downloaded it from. Null for an ordinary upload. Distinct
    #: from ``is_synthetic``, which says whether *we* wrote the document.
    source_url: str | None = Field(
        default=None,
        max_length=2000,
        description="Public URL this document was fetched from, if it did not "
        "arrive as an upload. Provenance for the real-document corpus.",
    )
    notes: str | None = None

    # ---- lifecycle (ARCHITECTURE §6) ----
    state: DocumentState = Field(
        default=DocumentState.RECEIVED,
        description="Where this version sits in the ingestion state machine.",
    )
    sensitivity: Sensitivity = Field(
        default=Sensitivity.INTERNAL,
        description="Need-to-know label. Defaults to internal: a document nobody "
        "has classified is not public.",
    )
    #: The blob store key — the SHA-256 of the *stored* bytes, which differ from
    #: the uploaded ones when active content was stripped at the boundary.
    blob_key: str | None = None
    uploaded_by: str | None = None
    failed_stage: str | None = None
    failed_reason: str | None = None
    #: Per-stage counters (``{"digitize": {"done": 142, "total": 400}}``), so a
    #: reviewer watching a 400-page report sees progress rather than a spinner.
    stage_progress: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content_hash")
    @classmethod
    def _lower_hex(cls, value: str) -> str:
        lowered = value.lower()
        if not all(c in "0123456789abcdef" for c in lowered):
            raise ValueError("content_hash must be hexadecimal")
        return lowered


class Fact(BaseModel):
    """A single normalized, evidence-backed numeric fact.

    ``value``/``unit`` are canonical (see :mod:`mrip.normalize.units`) so facts are
    comparable across documents. ``raw_value``/``raw_unit`` preserve what the
    document printed, so the evidence receipt quotes the source rather than our
    conversion of it.
    """

    fact_id: str
    entity_id: str = Field(description="Canonical entity id, e.g. 'secl'")
    mine_or_block: str | None = None

    metric: str = Field(description="Canonical metric key, e.g. 'coal_production'")
    value: float = Field(description="Canonical value, in `unit`")
    unit: str = Field(description="Canonical base unit, e.g. 't'")
    dimension: str = Field(description="Physical dimension, blocks cross-dimension maths")

    raw_value: float = Field(description="Value exactly as printed in the source")
    raw_unit: str = Field(description="Unit exactly as printed in the source")

    period_start: date
    period_end: date
    period_label: str = Field(description="Canonical label, e.g. 'FY2024-25'")
    fiscal_year: str | None = Field(
        default=None,
        description="Set only for fiscal periods. None for calendar years, which is "
        "what prevents fiscal/calendar figures being compared silently.",
    )

    evidence: EvidenceRef
    extraction_method: ExtractionMethod
    confidence: Confidence = Field(default_factory=Confidence)
    status: FactStatus = FactStatus.EXTRACTED
    review_state: ReviewState = ReviewState.UNREVIEWED

    unit_ambiguous: bool = Field(
        default=False, description="Source unit had more than one plausible reading"
    )
    notes: str | None = None
    extracted_at: datetime

    @model_validator(mode="after")
    def _period_ordered(self) -> Fact:
        if self.period_end < self.period_start:
            raise ValueError(
                f"period_end {self.period_end} precedes period_start {self.period_start}"
            )
        return self

    @property
    def dedup_key(self) -> tuple[str, str, str, date, date]:
        """Identity for conflict detection.

        Two facts sharing this key describe the same measurement. If their values
        disagree, that is a conflict to surface — never a value to pick between.
        """
        return (
            self.entity_id,
            self.metric,
            self.unit,
            self.period_start,
            self.period_end,
        )


class ConflictGroup(BaseModel):
    """Two or more facts that describe the same measurement but disagree.

    Modelled as a first-class object rather than a transient query result, because
    a conflict has its own lifecycle: it is detected, shown, adjudicated and
    recorded. Silently choosing a winner is the behaviour this type exists to
    prevent.
    """

    conflict_id: str
    entity_id: str
    metric: str
    unit: str
    period_label: str
    facts: list[Fact] = Field(min_length=2)
    detected_at: datetime
    resolved_fact_id: str | None = None
    resolution_note: str | None = None

    @property
    def is_resolved(self) -> bool:
        """Whether a reviewer has chosen an authoritative value."""
        return self.resolved_fact_id is not None

    @property
    def spread(self) -> float:
        """Absolute difference between the largest and smallest claimed value."""
        values = [f.value for f in self.facts]
        return max(values) - min(values)

    @property
    def relative_spread(self) -> float | None:
        """Spread as a fraction of the largest claimed value.

        More useful than absolute spread for triage: a 2-tonne disagreement is
        noise on a 700-million-tonne figure and alarming on a 10-tonne one.
        """
        largest = max(abs(f.value) for f in self.facts)
        return self.spread / largest if largest else None


# --------------------------------------------------------------------- query
#
# The AI query and response system (ARCHITECTURE §13). These types are the
# contract between the router, the deterministic figure paths, and the API. The
# invariant they encode: a numeric answer is a stored :class:`Fact` (which cannot
# exist without its evidence), never a value the router computed on the fly, and a
# refusal is a structured object carrying the evidence that caused it — not an
# error string (§13.5).


class QueryIntent(StrEnum):
    """The shape of a question, which decides *whether a model is involved*.

    Classified by deterministic patterns over the normalizers' vocabularies
    (§13.1). The first three are answered by SQL over facts or by retrieval, with
    no model client in scope; the last two are the only intents a model touches,
    and even then only to write prose around figures it was handed.
    """

    EXACT_FIGURE = "exact_figure"
    COMPARISON = "comparison"
    DISCOVERY = "discovery"
    NARRATIVE = "narrative"
    DRAFT = "draft"


class RefusalReason(StrEnum):
    """Why a question was declined, as a value the client can branch on.

    Each is rendered as an answer, not raised as an error: "there is an open
    conflict here" is a true and useful response, and the disagreement travels
    with it so the reader can act.
    """

    AMBIGUOUS_UNIT = "ambiguous_unit"
    OPEN_CONFLICT = "open_conflict"
    OUT_OF_CORPUS = "out_of_corpus"
    NO_VALIDATED_FACT = "no_validated_fact"
    #: The question wanted prose and the model could not supply it — the runtime
    #: is disabled, unreachable, or slower than the generation ceiling.
    #:
    #: Distinct from ``OUT_OF_CORPUS`` on purpose, and the distinction is the
    #: whole point: a prose question whose figures *were* found used to fall back
    #: to a lexical search and, finding no matching text, answer "nothing in your
    #: corpus matches this question". That told an officer their data was absent
    #: when it was present and the model was merely off — which is exactly the
    #: silent degradation the README promises does not happen.
    MODEL_UNAVAILABLE = "model_unavailable"


class SeriesPoint(BaseModel):
    """One entity's total for a metric in one fiscal year.

    The value is a ``SUM`` over the contributing facts, so a point is a summary,
    not itself a fact. ``fact_count`` says how many figures stand behind it; the
    facts themselves are one scoped ``/facts`` query away, which is how the
    summary stays traceable without inlining every row.
    """

    entity_id: str
    fiscal_year: str
    value: float
    unit: str
    fact_count: int


class Passage(BaseModel):
    """A retrieved evidence snippet, with the locator that produced it.

    The discovery path returns these rather than prose: every hit points at the
    document, version and page it came from, so "documents mentioning Gevra" is
    answered with places to look, not a paraphrase.
    """

    evidence_id: str
    document_id: str
    document_version: int
    page: int | None = None
    title: str | None = None
    filename: str | None = None
    snippet: str
    rank: float


class FigureAnswer(BaseModel):
    """A single exact figure: the validated fact that answers the question.

    The fact carries its own :class:`EvidenceRef` and :class:`Confidence`, so the
    answer is the evidence receipt — there is nothing to cite separately.
    """

    fact: Fact


class ComparisonAnswer(BaseModel):
    """A metric compared across entities and fiscal years."""

    metric: str
    unit: str | None = None
    points: list[SeriesPoint]


class DiscoveryAnswer(BaseModel):
    """Places in the corpus that match the question."""

    passages: list[Passage]


class NarrativeAnswer(BaseModel):
    """Prose the model wrote *around* figures it was handed.

    ``facts`` are the pinned figures the prose is grounded on and ``passages`` the
    retrieved context; both were given to the model, which never saw the fact
    store itself (§13.4). ``flagged`` is set when a sentence was dropped because it
    carried a numeral present in neither — a figure the model invented rather than
    one the deterministic layer supplied.
    """

    prose: str
    passages: list[Passage] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    flagged: bool = False


class Refusal(BaseModel):
    """A declined question, with the evidence behind the refusal.

    ``conflict`` accompanies :attr:`RefusalReason.OPEN_CONFLICT` and ``facts`` the
    ambiguous cases, so the client can show *why* rather than just *that* the
    system declined.
    """

    reason: RefusalReason
    message: str
    conflict: ConflictGroup | None = None
    facts: list[Fact] = Field(default_factory=list)


class QueryRequest(BaseModel):
    """A natural-language question over the corpus."""

    question: str = Field(min_length=1, max_length=2000)


class QueryResponse(BaseModel):
    """The envelope every query returns.

    Exactly one of :attr:`figure`, :attr:`comparison`, :attr:`discovery` or
    :attr:`refusal` is set. :attr:`model_used` records whether a language model
    was involved in producing this answer — always ``False`` for the figure,
    comparison and discovery paths, which is the §7 guarantee made visible to the
    caller rather than merely asserted in a design document.
    """

    question: str
    intent: QueryIntent
    model_used: bool = False
    figure: FigureAnswer | None = None
    comparison: ComparisonAnswer | None = None
    discovery: DiscoveryAnswer | None = None
    narrative: NarrativeAnswer | None = None
    refusal: Refusal | None = None


# -------------------------------------------------------------------- reports
#
# Automated report generation (ARCHITECTURE §11). The load-bearing idea is that a
# published report *pins its evidence*: every figure records the fact_id and the
# document@version it came from, so the same report re-renders a year later with
# the figures as approved, and diffs against what the corpus says now. A required
# figure with no validated fact fails the render loudly, naming the field — never
# a blank, never a model-filled number (§11.2).


class ReportState(StrEnum):
    """A report version's place in the publish lifecycle (§11.3)."""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    PUBLISHED = "published"


class PinnedFigure(BaseModel):
    """One resolved figure, with the evidence it pins.

    ``fact_id`` and ``document_id@document_version`` are the pin: they identify
    the exact fact and the exact source version behind the number, so the figure
    can be reproduced independently of whatever the corpus later becomes.
    """

    label: str
    entity_id: str
    metric: str
    period_label: str
    fact_id: str
    value: float
    unit: str
    raw_value: float
    raw_unit: str
    document_id: str
    document_version: int
    locator: str


class MissingFigure(BaseModel):
    """A field that could not be pinned, and why (a structured refusal reason)."""

    label: str
    entity_id: str
    metric: str
    period_label: str
    reason: RefusalReason
    message: str


class ReportManifest(BaseModel):
    """The pinned-evidence record of one report render.

    ``missing_required`` being non-empty means the render *failed*: a required
    figure had no validated fact, and §11.2 forbids emitting the report anyway.
    """

    report_id: str
    template_id: str
    template_version: int
    title: str
    generated_at: datetime
    state: ReportState = ReportState.DRAFT
    figures: list[PinnedFigure] = Field(default_factory=list)
    missing_required: list[MissingFigure] = Field(default_factory=list)
    missing_optional: list[MissingFigure] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether every required figure was pinned — the render may be emitted."""
        return not self.missing_required


class FigureDelta(BaseModel):
    """How one pinned figure compares to what the corpus says now (§11.3 diff)."""

    label: str
    entity_id: str
    metric: str
    period_label: str
    approved_value: float
    approved_document_version: int
    current_value: float | None = None
    current_document_version: int | None = None
    changed: bool = False
    note: str = ""


# --------------------------------------------------------------------- topics
#
# Word cloud and topic identification (ARCHITECTURE §12). The load-bearing idea
# is that a term is an *index entry*, not decoration: every one reaches the
# documents and pages that produced it. Hence two types rather than one — the
# cloud point, and what a click on it resolves to.


class CloudTerm(BaseModel):
    """One term in the word cloud.

    ``document_count`` is the size on screen and ``occurrences`` is the hover
    (§12.2): a term is big because many documents use it, not because one
    repetitive annexure repeats it. Keeping both is the point — collapsing them
    would let boilerplate dominate the corpus.
    """

    term: str
    document_count: int = Field(ge=1, description="Documents using the term — the size")
    occurrences: int = Field(ge=1, description="Raw count across those documents")
    score: float = Field(ge=0.0, description="Best TF-IDF score the term achieved")


class TermDocument(BaseModel):
    """A document a cloud term reaches — the click-through target (§12.3)."""

    document_id: str
    document_version: int
    title: str | None = None
    filename: str | None = None
    fiscal_year: str | None = None
    doc_class: str | None = None
    occurrences: int
    score: float
