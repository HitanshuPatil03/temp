"use client";

/**
 * Dashboard.
 *
 * Ordered by what a reporting officer has to act on, not by what is impressive:
 * the two counts that represent unfinished work (facts awaiting review, open
 * conflicts) sit in the same row as the corpus counts and turn red when nonzero.
 *
 * Every threshold shown here is read from `/api/dashboard/summary` rather than
 * hardcoded, so the badge states the rule that produced it. A review queue whose
 * cutoff the UI has guessed at is worse than no queue.
 */

import {
  FileText,
  GitCompareArrows,
  Layers,
  MessageSquare,
  ShieldCheck,
  Table2,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import useSWR from "swr";

import { ProductionChart } from "@/components/charts/production-chart";
import { ConflictCard } from "@/components/conflicts/conflict-card";
import { PageHeader } from "@/components/shell/page-header";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Select } from "@/components/ui/input";
import { StatTile } from "@/components/ui/stat";
import { EmptyState, ErrorState, Refetching, Skeleton } from "@/components/ui/states";
import { ApiError, fetcher, keys } from "@/lib/api";
import { formatNumber, formatPercent } from "@/lib/format";
import type { ConflictGroup, DashboardSummary, SeriesPoint } from "@/lib/types";

/**
 * The canonical metric the production view charts. Metric *discovery* arrives with
 * fact extraction (roadmap Phase 3.1) — until then this is the one series the fact
 * store is guaranteed to key on, and an empty result is a truthful empty state
 * rather than a fabricated one.
 */
const PRODUCTION_METRIC = "coal_production";

export default function DashboardPage() {
  const summary = useSWR<DashboardSummary>(keys.summary(), fetcher);
  const series = useSWR<SeriesPoint[]>(
    keys.series(PRODUCTION_METRIC, "t"),
    fetcher,
  );
  const conflicts = useSWR<ConflictGroup[]>(keys.conflicts(), fetcher);

  const fiscalYears = useMemo(() => {
    const years = new Set((series.data ?? []).map((point) => point.fiscal_year));
    return [...years].sort().reverse();
  }, [series.data]);

  const [year, setYear] = useState<string>("");
  const activeYear = year || fiscalYears[0] || "";

  const points = useMemo(
    () => (series.data ?? []).filter((point) => point.fiscal_year === activeYear),
    [series.data, activeYear],
  );

  const counts = summary.data?.counts;
  const thresholds = summary.data?.thresholds;

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Corpus state and outstanding review work. Every figure below is traceable to a document, page and cell — follow any number to its source."
        actions={
          <Link
            href="/ask"
            className="inline-flex items-center gap-2 rounded-md bg-series-1 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-blue-550"
          >
            <MessageSquare className="size-3.5" aria-hidden />
            Ask the corpus
          </Link>
        }
      />

      <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
        {/* ------------------------------------------------------- counts */}
        <section aria-label="Corpus counts">
          {summary.error ? (
            <ErrorState
              title="Cannot read the corpus summary"
              detail={
                summary.error instanceof ApiError
                  ? summary.error.detail
                  : "The API is not reachable. Start it with: python -m uvicorn mrip.main:app --port 8000"
              }
            />
          ) : !counts ? (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
              {Array.from({ length: 5 }, (_, index) => (
                <Skeleton key={index} className="h-[104px]" />
              ))}
            </div>
          ) : (
            <Refetching active={summary.isValidating}>
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                <StatTile
                  label="Documents"
                  value={formatNumber(counts.documents, 0)}
                  caption={`${formatNumber(counts.pages, 0)} pages · ${formatNumber(counts.evidence_spans, 0)} evidence spans`}
                  icon={FileText}
                  href="/documents"
                />
                <StatTile
                  label="Facts extracted"
                  value={formatNumber(counts.facts, 0)}
                  caption={`across ${formatNumber(counts.entities, 0)} entities and ${formatNumber(counts.metrics, 0)} metrics`}
                  icon={Table2}
                  href="/facts"
                />
                <StatTile
                  label="Validated"
                  value={formatNumber(counts.facts_validated, 0)}
                  caption={
                    counts.facts > 0
                      ? `${formatPercent(counts.facts_validated / counts.facts)} of extracted facts`
                      : "nothing extracted yet"
                  }
                  icon={ShieldCheck}
                  href="/facts?status=validated"
                />
                <StatTile
                  label="Awaiting review"
                  value={formatNumber(counts.facts_needs_review, 0)}
                  caption={
                    thresholds
                      ? `limiting confidence below ${thresholds.review_confidence.toFixed(2)}`
                      : undefined
                  }
                  icon={Layers}
                  tone="attention"
                  href="/facts?status=needs_review"
                />
                <StatTile
                  label="Open conflicts"
                  value={formatNumber(counts.open_conflicts, 0)}
                  caption={
                    thresholds
                      ? `disagreement above ${formatPercent(thresholds.conflict_material_spread, 2)}`
                      : undefined
                  }
                  icon={GitCompareArrows}
                  tone="attention"
                  href="/conflicts"
                />
              </div>
            </Refetching>
          )}
        </section>

        {/* ------------------------------------------------------ production */}
        <section aria-label="Production by subsidiary">
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-end justify-between gap-4">
                <div>
                  <CardTitle>Coal production by subsidiary</CardTitle>
                  <CardDescription>
                    Summed from validated facts. Only fiscal-year figures appear —
                    calendar-year facts are excluded rather than silently mixed in.
                  </CardDescription>
                </div>
                {fiscalYears.length > 0 ? (
                  <Field label="Fiscal year" className="w-40 shrink-0">
                    <Select
                      value={activeYear}
                      onChange={(event) => setYear(event.target.value)}
                    >
                      {fiscalYears.map((fy) => (
                        <option key={fy} value={fy}>
                          {fy}
                        </option>
                      ))}
                    </Select>
                  </Field>
                ) : null}
              </div>
            </CardHeader>
            <CardBody>
              {series.error ? (
                <ErrorState
                  title="Cannot read the production series"
                  detail={
                    series.error instanceof ApiError
                      ? series.error.detail
                      : "The API is not reachable."
                  }
                  refusal={
                    series.error instanceof ApiError && series.error.isRefusal
                  }
                />
              ) : series.isLoading ? (
                <Skeleton className="h-64" />
              ) : (
                <Refetching active={series.isValidating}>
                  <ProductionChart points={points} />
                </Refetching>
              )}
            </CardBody>
          </Card>
        </section>

        {/* ------------------------------------------------- conflict radar */}
        <section aria-label="Conflict radar">
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-baseline justify-between gap-3">
                <div>
                  <CardTitle>Conflict radar</CardTitle>
                  <CardDescription>
                    Where two sources report different values for the same entity,
                    metric, unit and period. Shown side by side and never merged —
                    a person names the winner.
                  </CardDescription>
                </div>
                {(conflicts.data?.length ?? 0) > 0 ? (
                  <Link
                    href="/conflicts"
                    className="text-xs font-medium text-blue-550 underline decoration-blue-300 underline-offset-2 hover:decoration-blue-550"
                  >
                    Adjudicate all {conflicts.data?.length} →
                  </Link>
                ) : null}
              </div>
            </CardHeader>
            <CardBody>
              {conflicts.error ? (
                <ErrorState
                  title="Cannot read the conflict radar"
                  detail={
                    conflicts.error instanceof ApiError
                      ? conflicts.error.detail
                      : "The API is not reachable."
                  }
                />
              ) : conflicts.isLoading ? (
                <Skeleton className="h-40" />
              ) : (conflicts.data?.length ?? 0) === 0 ? (
                <EmptyState icon={ShieldCheck} title="No open conflicts">
                  Either no two documents disagree, or conflict detection has not run
                  over this corpus yet. Run it from the{" "}
                  <Link href="/conflicts" className="text-blue-550 underline">
                    conflict radar
                  </Link>
                  .
                </EmptyState>
              ) : (
                <Refetching active={conflicts.isValidating}>
                  <div className="space-y-3">
                    {conflicts.data?.slice(0, 3).map((conflict) => (
                      <ConflictCard
                        key={conflict.conflict_id}
                        conflict={conflict}
                        materialSpread={thresholds?.conflict_material_spread}
                        reviewThreshold={thresholds?.review_confidence}
                      />
                    ))}
                  </div>
                </Refetching>
              )}
            </CardBody>
          </Card>
        </section>
      </main>
    </>
  );
}
