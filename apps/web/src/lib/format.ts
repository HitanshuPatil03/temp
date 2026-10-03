/**
 * Display helpers.
 *
 * Two rules drive this file.
 *
 * **Indian numbering.** A CMPDI reviewer reads "1,93,00,000", not "19,300,000".
 * `en-IN` grouping is the default for every figure, and large tonnages get a
 * lakh/crore suffix, because "704.2 MT" is how the source document says it.
 *
 * **Never invent precision.** Canonical values are in base units (tonnes), which
 * means a production figure arrives as 193000000. Rendering that as "193 MT" is
 * a rounding the reviewer should be able to see through, so `formatTonnes` keeps
 * the scaled figure and the exact one available side by side.
 */

import type { Confidence, EvidenceRef, Fact } from "./types";

const IN = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

/** Indian-grouped integer/decimal, e.g. `1,93,00,000`. */
export function formatNumber(value: number, digits = 2): string {
  return new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: digits,
  }).format(value);
}

/** Scale a count of tonnes to the unit the sector actually prints. */
export function formatTonnes(tonnes: number): { display: string; exact: string } {
  const exact = `${IN.format(tonnes)} t`;
  const abs = Math.abs(tonnes);
  if (abs >= 1e6) return { display: `${formatNumber(tonnes / 1e6)} MT`, exact };
  if (abs >= 1e5) return { display: `${formatNumber(tonnes / 1e5)} lakh t`, exact };
  return { display: exact, exact };
}

/** Compact axis tick — no unit, the axis label carries it. */
export function tickTonnes(tonnes: number): string {
  const abs = Math.abs(tonnes);
  if (abs >= 1e7) return `${formatNumber(tonnes / 1e7, 1)} cr`;
  if (abs >= 1e6) return `${formatNumber(tonnes / 1e6, 0)}M`;
  if (abs >= 1e5) return `${formatNumber(tonnes / 1e5, 0)} L`;
  if (abs >= 1e3) return `${formatNumber(tonnes / 1e3, 0)}k`;
  return formatNumber(tonnes, 0);
}

/** A value in whichever unit the fact is stored in. */
export function formatValue(value: number, unit: string): string {
  if (unit === "t" || unit === "t/yr") {
    const { display } = formatTonnes(value);
    return unit === "t/yr" ? `${display}/yr` : display;
  }
  if (unit === "fraction") return `${formatNumber(value * 100, 2)}%`;
  if (unit === "inr") {
    const abs = Math.abs(value);
    if (abs >= 1e7) return `₹${formatNumber(value / 1e7)} crore`;
    if (abs >= 1e5) return `₹${formatNumber(value / 1e5)} lakh`;
    return `₹${IN.format(value)}`;
  }
  return `${IN.format(value)} ${unit}`;
}

export function formatPercent(fraction: number, digits = 0): string {
  return `${(fraction * 100).toFixed(digits)}%`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

/**
 * A span of seconds in words — "47 s", "4 min", "2 h", "3 d".
 *
 * Takes a duration, not two timestamps, because the only correct place to do that
 * subtraction is wherever the authoritative clock is. For operational readings
 * that is the server: a browser with a wrong clock would otherwise invent an
 * outage or hide one. It also keeps the component that renders this pure — no
 * clock read during render, so no hydration mismatch and nothing for the React
 * Compiler to object to.
 */
export function formatDuration(seconds: number): string {
  const magnitude = Math.max(0, Math.round(seconds));
  if (magnitude < 60) return `${magnitude} s`;
  if (magnitude < 3600) return `${Math.round(magnitude / 60)} min`;
  if (magnitude < 86_400) return `${Math.round(magnitude / 3600)} h`;
  return `${Math.round(magnitude / 86_400)} d`;
}

/**
 * The citation string, mirroring `EvidenceRef.locator` on the backend.
 *
 * Duplicated deliberately: Pydantic properties are not serialised, and shipping
 * a computed field just for display would make the wire format depend on a UI
 * concern. The format is asserted in the Python test suite, so drift here is a
 * one-line fix rather than a silent divergence.
 */
export function locator(ref: EvidenceRef): string {
  const parts = [`doc:${ref.document_id}`];
  if (ref.document_version > 1) parts.push(`v${ref.document_version}`);
  if (ref.page !== null) parts.push(`p.${ref.page}`);
  if (ref.table_id) parts.push(ref.table_id);
  if (ref.cell_ref) parts.push(ref.cell_ref);
  return parts.join(" ");
}

/** The weakest applicable stage — a floor for triage, not a trust score. */
export function limitingConfidence(
  confidence: Confidence,
): { stage: "ocr" | "parse" | "answer"; score: number } | null {
  const stages = (["ocr", "parse", "answer"] as const)
    .map((stage) => ({ stage, score: confidence[stage] }))
    .filter((entry): entry is { stage: typeof entry.stage; score: number } =>
      entry.score !== null,
    );
  if (stages.length === 0) return null;
  return stages.reduce((low, entry) => (entry.score < low.score ? entry : low));
}

const STAGE_LABEL: Record<"ocr" | "parse" | "answer", string> = {
  ocr: "character recognition",
  parse: "table structure",
  answer: "answer synthesis",
};

export function stageLabel(stage: "ocr" | "parse" | "answer"): string {
  return STAGE_LABEL[stage];
}

/** Relative spread across a conflict's claimed values, for triage ordering. */
export function relativeSpread(facts: Fact[]): number {
  const values = facts.map((fact) => fact.value);
  const largest = Math.max(...values.map(Math.abs));
  return largest === 0 ? 0 : (Math.max(...values) - Math.min(...values)) / largest;
}

/** `coal_production` → `Coal production`. Metric keys are snake_case by contract. */
export function humanize(key: string): string {
  const spaced = key.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Subsidiary codes are acronyms; anything else gets title-cased. */
export function entityLabel(entityId: string): string {
  return entityId.length <= 5 ? entityId.toUpperCase() : humanize(entityId);
}
