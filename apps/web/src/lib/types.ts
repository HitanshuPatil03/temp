/**
 * TypeScript mirrors of `mrip.schemas`.
 *
 * Kept hand-written rather than generated from the OpenAPI document, because the
 * generator would also reproduce the parts of the schema the UI has no business
 * knowing about, and because a mismatch here should be a compile error in a small
 * reviewable file rather than a diff in a large generated one.
 *
 * Note what is *absent*: the Pydantic `@property` accessors (`locator`,
 * `limiting`, `spread`) are not serialised, so the equivalents live in
 * `lib/format.ts` as pure functions over these shapes.
 */

export type DocumentClass =
  | "text_pdf"
  | "scanned_pdf"
  | "mixed_pdf"
  | "spreadsheet"
  | "docx"
  | "image"
  | "unknown";

export type ExtractionMethod =
  | "pdf_text_layer"
  | "ocr"
  | "table_lattice"
  | "table_stream"
  | "spreadsheet_cell"
  | "vlm"
  | "manual";

export type FactStatus =
  | "extracted"
  | "validated"
  | "needs_review"
  | "conflicted"
  | "superseded"
  | "rejected";

export type ReviewState = "unreviewed" | "approved" | "corrected" | "rejected";

export interface BBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface EvidenceRef {
  document_id: string;
  document_version: number;
  page: number | null;
  table_id: string | null;
  cell_ref: string | null;
  bbox: BBox | null;
  snippet: string | null;
}

/**
 * Three independent stages, never collapsed into one score. `null` means the
 * stage did not apply — a spreadsheet cell has no OCR pass to be confident about.
 */
export interface Confidence {
  ocr: number | null;
  parse: number | null;
  answer: number | null;
}

/** Where a document sits in the ingestion pipeline (ARCHITECTURE §6). */
export type DocumentState =
  | "received"
  | "classified"
  | "digitized"
  | "extracted"
  | "normalized"
  | "validated"
  | "indexed"
  | "ready"
  | "failed"
  | "quarantined";

export type Sensitivity = "public" | "internal" | "restricted";

/**
 * The six ingestion stages, in order.
 *
 * Mirrors `STAGES` in `mrip/ingest/lifecycle.py`. Note these are *stage* names,
 * not states: retrying stage `digitize` moves the document back to `classified`
 * — the state before that stage — so the stage runs again.
 */
export type IngestStage =
  | "classify"
  | "digitize"
  | "extract"
  | "normalize"
  | "validate"
  | "index";

export const INGEST_STAGES: readonly IngestStage[] = [
  "classify",
  "digitize",
  "extract",
  "normalize",
  "validate",
  "index",
] as const;

export interface MripDocument {
  document_id: string;
  content_hash: string;
  filename: string;
  doc_class: DocumentClass;
  version: number;
  supersedes: string | null;
  page_count: number | null;
  size_bytes: number;
  title: string | null;
  publisher_entity_id: string | null;
  fiscal_year: string | null;
  ingested_at: string;
  is_synthetic: boolean;
  notes: string | null;
  state: DocumentState;
  sensitivity: Sensitivity;
  /** Set only on a failure, and it names the stage — a failed document is a
   *  work item, not a log line. */
  failed_stage: string | null;
  failed_reason: string | null;
  /**
   * Per-stage counters, e.g. `{ digitize: { done: 142, total: 400, at: "…" } }`.
   *
   * Loosely typed on purpose: `extract` writes extra keys of its own (`tables`,
   * `skipped`, `examples`) and a future stage may write more. Read it through
   * {@link latestStageProgress}, which validates the two fields every stage is
   * guaranteed to write rather than trusting the shape.
   */
  stage_progress: Record<string, unknown>;
  blob_key: string | null;
  uploaded_by: string | null;
}

/** One stage's counter, after validation. */
export interface StageProgress {
  stage: string;
  done: number;
  total: number;
}

/**
 * The most recently reported stage counter, or null if none is readable.
 *
 * Picks by the `at` timestamp each stage writes rather than by deriving which
 * stage *should* be running from the document's state. Deriving it would mean
 * keeping a copy of the pipeline's state→stage table here, and a second copy of
 * a mapping is a copy that drifts. The latest report is also the more honest
 * answer: it is what the worker actually said.
 */
export function latestStageProgress(
  progress: Record<string, unknown>,
): StageProgress | null {
  let best: StageProgress | null = null;
  let bestAt = "";

  for (const [stage, value] of Object.entries(progress ?? {})) {
    if (typeof value !== "object" || value === null) continue;
    const entry = value as Record<string, unknown>;
    const { done, total, at } = entry;
    // A total of 0 is not progress, it is a division by zero waiting to happen.
    if (typeof done !== "number" || typeof total !== "number" || total <= 0) continue;

    const stamp = typeof at === "string" ? at : "";
    if (best === null || stamp > bestAt) {
      best = { stage, done, total };
      bestAt = stamp;
    }
  }
  return best;
}

export interface Fact {
  fact_id: string;
  entity_id: string;
  mine_or_block: string | null;
  metric: string;
  value: number;
  unit: string;
  dimension: string;
  raw_value: number;
  raw_unit: string;
  period_start: string;
  period_end: string;
  period_label: string;
  fiscal_year: string | null;
  evidence: EvidenceRef;
  extraction_method: ExtractionMethod;
  confidence: Confidence;
  status: FactStatus;
  review_state: ReviewState;
  unit_ambiguous: boolean;
  notes: string | null;
  extracted_at: string;
}

export interface ConflictGroup {
  conflict_id: string;
  entity_id: string;
  metric: string;
  unit: string;
  period_label: string;
  facts: Fact[];
  detected_at: string;
  resolved_fact_id: string | null;
  resolution_note: string | null;
}

/** A row of `evidence`, flat, as the page-evidence endpoint returns it. */
export interface EvidenceSpan {
  evidence_id: string;
  document_id: string;
  document_version: number;
  page: number | null;
  kind: string | null;
  table_id: string | null;
  cell_ref: string | null;
  row_idx: number | null;
  col_idx: number | null;
  bbox_x0: number | null;
  bbox_y0: number | null;
  bbox_x1: number | null;
  bbox_y1: number | null;
  text: string | null;
  ocr_confidence: number | null;
  extraction_method: string | null;
}

export interface DashboardSummary {
  counts: {
    documents: number;
    pages: number;
    evidence_spans: number;
    facts: number;
    facts_validated: number;
    facts_needs_review: number;
    open_conflicts: number;
    entities: number;
    metrics: number;
  };
  /** Ships with the counts so a review badge can state the rule that produced it. */
  thresholds: {
    review_confidence: number;
    conflict_material_spread: number;
  };
}

/** One point of `/series/{metric}`, already summed per entity and fiscal year. */
export interface SeriesPoint {
  entity_id: string;
  fiscal_year: string;
  unit: string;
  value: number;
  fact_count: number;
}

export interface Health {
  status: string;
  app: string;
  problem_statement: string;
}

/**
 * Whether ingestion is actually progressing. Admin only — these counters span
 * every subsidiary, and a corpus-wide document count discloses the scale of
 * other subsidiaries' holdings.
 */
export interface PipelineHealth {
  queue: {
    pending: number;
    in_flight: number;
    dead: number;
    failed: number;
    oldest_pending_at: string | null;
    /**
     * How long the oldest pending job has been waiting. **The number that
     * matters** — a depth of 40 is healthy at twenty seconds and an outage at
     * four hours.
     *
     * Computed by the database rather than subtracted from `oldest_pending_at`
     * here on purpose: a browser with a wrong clock would otherwise report an
     * outage that is not happening, or miss one that is, and a reading that
     * decides whether someone is paged must not depend on whose watch is right.
     * It also keeps the component pure — no clock read during render.
     */
    oldest_pending_age_seconds: number | null;
    earliest_lease_expiry: string | null;
  };
  documents_by_state: Record<string, number>;
  /** Documents and jobs that need a person, as one number an alert can watch. */
  needs_attention: number;
  registered_job_kinds: string[];
}

// ------------------------------------------------------------------ admin

export type Role = "viewer" | "officer" | "reviewer" | "approver" | "admin";
export type AuthSource = "local" | "oidc" | "ldap";

/** An account as an administrator sees it. No password hash, ever. */
export interface UserAccount {
  user_id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  role: Role;
  auth_source: AuthSource;
  is_active: boolean;
  must_change_password: boolean;
  entities: string[];
  failed_login_count: number;
  locked_until: string | null;
  created_at: string;
  last_login_at: string | null;
}

/** One row of the append-only audit trail, as the admin endpoint returns it. */
export interface AuditEntry {
  audit_id: number;
  occurred_at: string;
  actor: string | null;
  action: string;
  subject_type: string | null;
  subject_id: string | null;
  entity_scope: string | null;
  detail: Record<string, unknown>;
  request_id: string | null;
  source_ip: string | null;
}


// --------------------------------------------------------------- normalizer

export type MTConvention = "million_tonnes" | "metric_tonne";

export interface NormalizedQuantity {
  value: number;
  unit: string;
  dimension: string;
  raw_value: number;
  raw_unit: string;
  ambiguous: boolean;
  note: string | null;
}

export interface NormalizedPeriod {
  label: string;
  kind: string;
  start: string;
  end: string;
  days: number;
  fiscal_year: string | null;
  is_fiscal: boolean;
  raw: string;
  ambiguous: boolean;
  note: string | null;
}

export interface Comparability {
  comparable: boolean;
  reason: string | null;
  left: string;
  right: string;
}

export interface ResolvedEntity {
  entity_id: string;
  code: string;
  name: string;
  kind: string;
  parent: string | null;
  is_cil_group: boolean;
  state: string | null;
  headquarters: string | null;
  note: string | null;
  matched_on: string;
  exact: boolean;
  score: number;
  needs_review: boolean;
}

// ------------------------------------------------------------ query (ARCHITECTURE 13)

export type QueryIntent =
  | "exact_figure"
  | "comparison"
  | "discovery"
  | "narrative"
  | "draft";

export type RefusalReason =
  | "ambiguous_unit"
  | "open_conflict"
  | "out_of_corpus"
  | "no_validated_fact";

export interface Passage {
  evidence_id: string;
  document_id: string;
  document_version: number;
  page: number | null;
  title: string | null;
  filename: string | null;
  snippet: string;
  rank: number;
}

export interface FigureAnswer {
  fact: Fact;
}

export interface ComparisonAnswer {
  metric: string;
  unit: string | null;
  points: SeriesPoint[];
}

export interface DiscoveryAnswer {
  passages: Passage[];
}

export interface NarrativeAnswer {
  prose: string;
  passages: Passage[];
  facts: Fact[];
  flagged: boolean;
}

export interface Refusal {
  reason: RefusalReason;
  message: string;
  conflict: ConflictGroup | null;
  facts: Fact[];
}

export interface QueryResponse {
  question: string;
  intent: QueryIntent;
  model_used: boolean;
  figure: FigureAnswer | null;
  comparison: ComparisonAnswer | null;
  discovery: DiscoveryAnswer | null;
  narrative: NarrativeAnswer | null;
  refusal: Refusal | null;
}

/**
 * A question the corpus can actually answer, with the reason it is offered.
 * Built server-side from validated facts and extracted keyphrases, so a chip is
 * never a question that leads straight to a refusal.
 */
export interface QuerySuggestion {
  question: string;
  intent: QueryIntent;
  why: string;
}

// ----------------------------------------------------------- reports (ARCHITECTURE 11)

export type ReportState = "draft" | "in_review" | "approved" | "published";

/**
 * One resolved figure and the evidence it pins. `fact_id` plus
 * `document_id@document_version` is the pin: it is what lets the same report
 * re-render a year later with the figures as approved.
 */
export interface PinnedFigure {
  label: string;
  entity_id: string;
  metric: string;
  period_label: string;
  fact_id: string;
  value: number;
  unit: string;
  raw_value: number;
  raw_unit: string;
  document_id: string;
  document_version: number;
  locator: string;
}

/** A field that could not be pinned, carrying the structured reason why. */
export interface MissingFigure {
  label: string;
  entity_id: string;
  metric: string;
  period_label: string;
  reason: RefusalReason;
  message: string;
}

/**
 * The stored record of one report render. `missing_required` being non-empty
 * means the report will not render — a blank where a required number should be
 * is the one output this system must never produce.
 */
export interface ReportManifest {
  report_id: string;
  template_id: string;
  template_version: number;
  title: string;
  generated_at: string;
  state: ReportState;
  figures: PinnedFigure[];
  missing_required: MissingFigure[];
  missing_optional: MissingFigure[];
}

/** How one pinned figure compares to what the corpus says now. */
export interface FigureDelta {
  label: string;
  entity_id: string;
  metric: string;
  period_label: string;
  approved_value: number;
  approved_document_version: number;
  current_value: number | null;
  current_document_version: number | null;
  changed: boolean;
  note: string;
}

/**
 * An installed report template and what it needs. `required` metrics that do
 * not resolve block the render; `optional` ones are simply omitted.
 */
export interface TemplateSummary {
  id: string;
  version: number;
  title: string;
  required: string[];
  optional: string[];
  sections: string[];
  has_narrative: boolean;
}

// ------------------------------------------------------------ topics (ARCHITECTURE 12)

/**
 * One term in the word cloud. `document_count` is the size on screen and
 * `occurrences` the hover: a term is big because many documents use it, not
 * because one repetitive annexure repeats it.
 */
export interface CloudTerm {
  term: string;
  document_count: number;
  occurrences: number;
  score: number;
}

/** A document a cloud term reaches — what makes the cloud an index. */
export interface TermDocument {
  document_id: string;
  document_version: number;
  title: string | null;
  filename: string | null;
  fiscal_year: string | null;
  doc_class: string | null;
  occurrences: number;
  score: number;
}

/** One point of a term's year-on-year prevalence. */
export interface TermPrevalence {
  fiscal_year: string;
  document_count: number;
}

// --------------------------------------------------- document progress

/** One refusal reason, counted and explained. */
export interface SkipGroup {
  reason: string;
  /** A human sentence. Written on the backend, next to the code that raises the
   *  reason, so there is one source of truth for what a refusal means. */
  label: string;
  count: number;
  /** A few verbatim cells, for the actionable reasons only — five examples of an
   *  empty cell is noise. */
  examples: string[];
}

/** What the extract stage produced, and what it declined to. */
export interface ExtractionSummary {
  facts: number | null;
  candidates: number | null;
  tables: number | null;
  /** Refusals a reviewer can do something about. */
  needs_attention: SkipGroup[];
  /** Total rows, headers, blanks. Counted so the arithmetic adds up; separated so
   *  they do not read as problems. */
  ignored: SkipGroup[];
}

export interface DocumentProgress {
  document_id: string;
  state: DocumentState;
  doc_class: DocumentClass;
  failed_stage: string | null;
  failed_reason: string | null;
  progress: Record<string, unknown>;
  /** Null until extraction has run — which is a different answer from "ran and
   *  refused nothing". */
  extraction: ExtractionSummary | null;
  stages: { name: string; description: string; completed: boolean }[];
}
