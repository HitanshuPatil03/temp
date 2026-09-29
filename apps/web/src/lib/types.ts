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
  /** Per-stage counters, e.g. `{ digitize: { done: 142, total: 400 } }`. */
  stage_progress: Record<string, unknown>;
  blob_key: string | null;
  uploaded_by: string | null;
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
