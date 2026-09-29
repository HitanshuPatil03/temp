"use client";

/**
 * A conflict between documents, and the form that settles it.
 *
 * The two disagreeing figures are shown on one shared scale as a dumbbell, so the
 * *size* of the disagreement is visible before any number is read — 0.3% apart and
 * 40% apart look nothing alike, and they mean nothing alike either.
 *
 * There is no "merge" or "average" control, and there never will be. Resolution
 * means a named person picked a side and said why; the losing fact is marked
 * superseded, not deleted, so the decision stays auditable.
 */

import { AlertTriangle, ArrowRight, Check } from "lucide-react";
import { useState } from "react";

import { Badge, StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Field, Textarea } from "@/components/ui/input";
import { Meter } from "@/components/ui/stat";
import { ApiError } from "@/lib/api";
import {
  entityLabel,
  formatDate,
  formatPercent,
  formatValue,
  humanize,
  locator,
  relativeSpread,
} from "@/lib/format";
import type { ConflictGroup, Fact } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ConflictCard({
  conflict,
  materialSpread,
  reviewThreshold,
  onResolved,
}: {
  conflict: ConflictGroup;
  /** The spread above which the backend called this material. Shown, not assumed. */
  materialSpread?: number;
  reviewThreshold?: number;
  /** Omit to render read-only — the dashboard radar does. */
  onResolved?: (conflictId: string, winningFactId: string) => Promise<void>;
}) {
  const spread = relativeSpread(conflict.facts);
  const resolved = conflict.resolved_fact_id !== null;
  const material = materialSpread !== undefined && spread >= materialSpread;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-ink">
                {entityLabel(conflict.entity_id)} · {humanize(conflict.metric)}
              </h3>
              <Badge tone="neutral">{conflict.period_label}</Badge>
              {resolved ? (
                <Badge tone="good" icon={Check}>
                  Resolved
                </Badge>
              ) : (
                <Badge
                  tone={material ? "critical" : "warning"}
                  icon={AlertTriangle}
                >
                  {material ? "Material" : "Immaterial"}
                </Badge>
              )}
            </div>
            <p className="mt-1 text-xs text-ink-3">
              {conflict.facts.length} sources disagree by{" "}
              <span className="tnum font-medium text-ink-2">
                {formatPercent(spread, 2)}
              </span>
              {materialSpread !== undefined ? (
                <> · materiality threshold {formatPercent(materialSpread, 2)}</>
              ) : null}{" "}
              · detected {formatDate(conflict.detected_at)}
            </p>
          </div>
        </div>
      </CardHeader>

      <CardBody className="space-y-4">
        <Dumbbell facts={conflict.facts} winner={conflict.resolved_fact_id} />

        <ul className="space-y-2">
          {conflict.facts.map((fact) => (
            <FactSide
              key={fact.fact_id}
              fact={fact}
              won={fact.fact_id === conflict.resolved_fact_id}
              reviewThreshold={reviewThreshold}
            />
          ))}
        </ul>

        {resolved ? (
          <p className="rounded-md border border-hairline bg-plane px-3 py-2 text-xs text-ink-2">
            Resolved in favour of{" "}
            <span className="font-mono text-ink">{conflict.resolved_fact_id}</span>.
            {conflict.resolution_note ? (
              <> Reviewer&rsquo;s note: &ldquo;{conflict.resolution_note}&rdquo;</>
            ) : (
              <> No note was recorded.</>
            )}
          </p>
        ) : onResolved ? (
          <ResolveForm conflict={conflict} onResolved={onResolved} />
        ) : null}
      </CardBody>
    </Card>
  );
}

/**
 * Both values on one shared scale. Deliberately not a bar chart — a bar from zero
 * would make two figures 0.4% apart look identical, which is the one thing the
 * reviewer must be able to see.
 */
function Dumbbell({
  facts,
  winner,
}: {
  facts: Fact[];
  winner: string | null;
}) {
  const values = facts.map((f) => f.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  // Inset so the end dots are not clipped by the track.
  const position = (value: number) => 6 + ((value - min) / span) * 88;

  return (
    <div className="px-1 pt-1 pb-5">
      <div className="relative h-2">
        <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-rule" />
        <div
          className="absolute top-1/2 h-0.5 -translate-y-1/2 bg-series-1"
          style={{
            left: `${position(min)}%`,
            width: `${position(max) - position(min)}%`,
          }}
        />
        {facts.map((fact, index) => (
          <span
            key={fact.fact_id}
            className={cn(
              "absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-surface",
              winner === fact.fact_id
                ? "bg-good"
                : index === 0
                  ? "bg-series-1"
                  : "bg-series-2",
            )}
            style={{ left: `${position(fact.value)}%` }}
            title={formatValue(fact.value, fact.unit)}
          />
        ))}
        <span
          className="tnum absolute top-4 -translate-x-1/2 text-xs whitespace-nowrap text-ink-3"
          style={{ left: `${position(min)}%` }}
        >
          {formatValue(min, facts[0].unit)}
        </span>
        {max !== min ? (
          <span
            className="tnum absolute top-4 -translate-x-1/2 text-xs whitespace-nowrap text-ink-3"
            style={{ left: `${position(max)}%` }}
          >
            {formatValue(max, facts[0].unit)}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function FactSide({
  fact,
  won,
  reviewThreshold,
}: {
  fact: Fact;
  won: boolean;
  reviewThreshold?: number;
}) {
  return (
    <li
      className={cn(
        "rounded-md border px-3 py-2.5",
        won ? "border-good/40 bg-good/5" : "border-hairline bg-plane",
      )}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="tnum text-sm font-semibold text-ink">
          {formatValue(fact.value, fact.unit)}
        </span>
        <StatusBadge status={fact.status} />
      </div>

      <p className="mt-1 text-xs text-ink-2">
        printed as{" "}
        <span className="tnum font-medium">
          {fact.raw_value} {fact.raw_unit}
        </span>{" "}
        · {locator(fact.evidence)}
      </p>

      {fact.evidence.snippet ? (
        <p className="mt-1.5 border-l-2 border-rule pl-2 font-mono text-xs leading-relaxed text-ink-3">
          {fact.evidence.snippet}
        </p>
      ) : null}

      <div className="mt-2 grid gap-1 sm:max-w-sm">
        <Meter
          label="OCR"
          score={fact.confidence.ocr}
          threshold={reviewThreshold}
        />
        <Meter
          label="Parse"
          score={fact.confidence.parse}
          threshold={reviewThreshold}
        />
        <Meter
          label="Answer"
          score={fact.confidence.answer}
          threshold={reviewThreshold}
        />
      </div>
    </li>
  );
}

function ResolveForm({
  conflict,
  onResolved,
}: {
  conflict: ConflictGroup;
  onResolved: (conflictId: string, winningFactId: string) => Promise<void>;
}) {
  const [winner, setWinner] = useState<string>("");
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!winner) return;
    setPending(true);
    setError(null);
    try {
      await onResolved(conflict.conflict_id, winner);
    } catch (cause) {
      setError(
        cause instanceof ApiError ? cause.detail : "Could not record the decision.",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="space-y-3 rounded-md border border-hairline bg-plane px-3 py-3"
    >
      <fieldset>
        <legend className="text-xs font-medium text-ink-2">
          Which figure is correct?
        </legend>
        <div className="mt-2 space-y-1.5">
          {conflict.facts.map((fact) => (
            <label
              key={fact.fact_id}
              className="flex cursor-pointer items-baseline gap-2 text-xs"
            >
              <input
                type="radio"
                name={`winner-${conflict.conflict_id}`}
                value={fact.fact_id}
                checked={winner === fact.fact_id}
                onChange={() => setWinner(fact.fact_id)}
                className="accent-series-1"
              />
              <span className="tnum font-medium text-ink">
                {formatValue(fact.value, fact.unit)}
              </span>
              <span className="text-ink-3">{locator(fact.evidence)}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <Field
        label="Reason"
        hint="Recorded against your identity in the audit log. A later reader will rely on it."
      >
        <Textarea
          rows={2}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="e.g. FY24 revised annual report supersedes the provisional press release."
        />
      </Field>

      {error ? <p className="text-xs text-critical">{error}</p> : null}

      <Button type="submit" size="sm" disabled={!winner} pending={pending}>
        Record decision <ArrowRight className="size-3.5" aria-hidden />
      </Button>
    </form>
  );
}
