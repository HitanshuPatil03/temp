"use client";

/**
 * Facts — the extracted figures, and the receipt behind each one.
 *
 * The filter row sits above everything it scopes. Entity, metric and fiscal year
 * are pushed to the API; **status is filtered here**, because `/facts` has no status
 * parameter — it is honest to narrow the returned page client-side rather than to
 * invent a query the backend does not answer.
 *
 * Selecting a row opens the evidence panel. Both the canonical value and the value
 * as literally printed are shown: canonical makes comparison possible, raw keeps the
 * receipt honest.
 */

import { FileSearch, Table2 } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import useSWR from "swr";

import { PageHeader } from "@/components/shell/page-header";
import { AmbiguityBadge, Badge, StatusBadge } from "@/components/ui/badge";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Meter } from "@/components/ui/stat";
import { EmptyState, ErrorState, Refetching, Skeleton } from "@/components/ui/states";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { ApiError, fetcher, keys } from "@/lib/api";
import {
  entityLabel,
  formatDate,
  formatValue,
  humanize,
  limitingConfidence,
  locator,
  stageLabel,
} from "@/lib/format";
import type { DashboardSummary, Fact, FactStatus } from "@/lib/types";

const STATUSES: FactStatus[] = [
  "extracted",
  "validated",
  "needs_review",
  "conflicted",
  "superseded",
  "rejected",
];

export default function FactsPage() {
  return (
    <Suspense fallback={<Skeleton className="m-8 h-64" />}>
      <FactsView />
    </Suspense>
  );
}

function FactsView() {
  const params = useSearchParams();
  const summary = useSWR<DashboardSummary>(keys.summary(), fetcher);

  const [entityId, setEntityId] = useState("");
  const [metric, setMetric] = useState("");
  const [fiscalYear, setFiscalYear] = useState("");
  const [status, setStatus] = useState<string>(params.get("status") ?? "");
  const [selected, setSelected] = useState<Fact | null>(null);

  // `superseded` and `rejected` are hidden by the API unless asked for, so
  // selecting either of them has to widen the server query too.
  const includeInactive = status === "superseded" || status === "rejected";

  const facts = useSWR<Fact[]>(
    keys.facts({
      entity_id: entityId,
      metric,
      fiscal_year: fiscalYear,
      include_inactive: includeInactive || undefined,
      limit: 500,
    }),
    fetcher,
  );

  const rows = useMemo(
    () =>
      (facts.data ?? []).filter((fact) => !status || fact.status === status),
    [facts.data, status],
  );

  const reviewThreshold = summary.data?.thresholds.review_confidence;

  return (
    <>
      <PageHeader
        title="Facts"
        description="Every figure the deterministic pipeline extracted, with the document, page, table and cell it came from. No value on this screen was produced by a language model."
      />

      <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
        {/* One filter row, above everything it scopes. */}
        <Card>
          <CardBody>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Field label="Entity" hint="Canonical id, e.g. cil_secl">
                <Input
                  value={entityId}
                  placeholder="all entities"
                  onChange={(event) => setEntityId(event.target.value.trim())}
                />
              </Field>
              <Field label="Metric" hint="e.g. coal_production">
                <Input
                  value={metric}
                  placeholder="all metrics"
                  onChange={(event) => setMetric(event.target.value.trim())}
                />
              </Field>
              <Field label="Fiscal year" hint="e.g. FY2024-25">
                <Input
                  value={fiscalYear}
                  placeholder="all years"
                  onChange={(event) => setFiscalYear(event.target.value.trim())}
                />
              </Field>
              <Field
                label="Status"
                hint={
                  includeInactive
                    ? "Inactive facts are kept, never deleted"
                    : undefined
                }
              >
                <Select
                  value={status}
                  onChange={(event) => setStatus(event.target.value)}
                >
                  <option value="">any status</option>
                  {STATUSES.map((value) => (
                    <option key={value} value={value}>
                      {humanize(value)}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
          </CardBody>
        </Card>

        <div className="grid gap-6 xl:grid-cols-[1fr_22rem]">
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <CardTitle>
                  {rows.length} fact{rows.length === 1 ? "" : "s"}
                </CardTitle>
                {reviewThreshold !== undefined ? (
                  <CardDescription>
                    Flagged below a {reviewThreshold.toFixed(2)} limiting confidence
                  </CardDescription>
                ) : null}
              </div>
            </CardHeader>
            <CardBody>
              {facts.error ? (
                <ErrorState
                  title="Cannot read the fact store"
                  detail={
                    facts.error instanceof ApiError
                      ? facts.error.detail
                      : "The API is not reachable. Start it with: python -m uvicorn mrip.main:app --port 8000"
                  }
                  refusal={
                    facts.error instanceof ApiError && facts.error.isRefusal
                  }
                />
              ) : facts.isLoading ? (
                <Skeleton className="h-64" />
              ) : rows.length === 0 ? (
                <EmptyState icon={Table2} title="No facts match this filter">
                  {(facts.data?.length ?? 0) > 0
                    ? "Widen the filter — the store holds facts, just none with these values."
                    : "Fact extraction is roadmap Phase 3. The store is empty rather than seeded with plausible-looking numbers."}
                </EmptyState>
              ) : (
                <Refetching active={facts.isValidating}>
                  <Table>
                    <THead>
                      <TR>
                        <TH>Entity</TH>
                        <TH>Metric</TH>
                        <TH>Period</TH>
                        <TH numeric>Value</TH>
                        <TH>Status</TH>
                        <TH numeric>Limiting</TH>
                      </TR>
                    </THead>
                    <TBody>
                      {rows.map((fact) => {
                        const limiting = limitingConfidence(fact.confidence);
                        return (
                          <TR
                            key={fact.fact_id}
                            interactive
                            selected={selected?.fact_id === fact.fact_id}
                            onClick={() => setSelected(fact)}
                          >
                            <TD className="font-medium">
                              {entityLabel(fact.entity_id)}
                            </TD>
                            <TD className="text-ink-2">
                              {humanize(fact.metric)}
                            </TD>
                            <TD className="text-ink-2">{fact.period_label}</TD>
                            <TD numeric>
                              <span className="inline-flex items-center gap-1.5">
                                {formatValue(fact.value, fact.unit)}
                                {fact.unit_ambiguous ? (
                                  <AmbiguityBadge
                                    title={`Printed as "${fact.raw_unit}", which is ambiguous`}
                                  />
                                ) : null}
                              </span>
                            </TD>
                            <TD>
                              <StatusBadge status={fact.status} />
                            </TD>
                            <TD
                              numeric
                              className={
                                limiting &&
                                reviewThreshold !== undefined &&
                                limiting.score < reviewThreshold
                                  ? "font-medium text-ink"
                                  : "text-ink-2"
                              }
                              title={
                                limiting
                                  ? `Weakest stage: ${stageLabel(limiting.stage)}`
                                  : undefined
                              }
                            >
                              {limiting ? limiting.score.toFixed(2) : "n/a"}
                            </TD>
                          </TR>
                        );
                      })}
                    </TBody>
                  </Table>
                </Refetching>
              )}
            </CardBody>
          </Card>

          <EvidencePanel fact={selected} reviewThreshold={reviewThreshold} />
        </div>
      </main>
    </>
  );
}

function EvidencePanel({
  fact,
  reviewThreshold,
}: {
  fact: Fact | null;
  reviewThreshold?: number;
}) {
  if (!fact) {
    return (
      <Card>
        <CardBody>
          <EmptyState icon={FileSearch} title="No fact selected">
            Select a row to see its receipt — the document, page and cell it came
            from, what the page literally printed, and the confidence of each
            extraction stage.
          </EmptyState>
        </CardBody>
      </Card>
    );
  }

  const limiting = limitingConfidence(fact.confidence);

  return (
    <Card className="self-start">
      <CardHeader>
        <CardTitle>Evidence</CardTitle>
        <CardDescription className="font-mono text-xs">
          {fact.fact_id}
        </CardDescription>
      </CardHeader>
      <CardBody className="space-y-4">
        <div>
          <p className="text-xs font-medium text-ink-2">Canonical value</p>
          <p className="tnum mt-0.5 text-xl font-semibold text-ink">
            {formatValue(fact.value, fact.unit)}
          </p>
          <p className="tnum mt-0.5 text-xs text-ink-3">
            printed as {fact.raw_value} {fact.raw_unit}
          </p>
          {fact.unit_ambiguous ? (
            <p className="mt-1.5 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-xs text-ink">
              <span className="font-medium">Ambiguous unit.</span> &ldquo;
              {fact.raw_unit}&rdquo; has more than one reading in Indian coal
              reporting; the convention used is recorded rather than guessed.
            </p>
          ) : null}
        </div>

        <dl className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1.5 text-xs">
          <dt className="text-ink-3">Entity</dt>
          <dd className="text-ink">{entityLabel(fact.entity_id)}</dd>
          {fact.mine_or_block ? (
            <>
              <dt className="text-ink-3">Mine / block</dt>
              <dd className="text-ink">{fact.mine_or_block}</dd>
            </>
          ) : null}
          <dt className="text-ink-3">Metric</dt>
          <dd className="text-ink">{humanize(fact.metric)}</dd>
          <dt className="text-ink-3">Dimension</dt>
          <dd className="text-ink">{humanize(fact.dimension)}</dd>
          <dt className="text-ink-3">Period</dt>
          <dd className="text-ink">
            {fact.period_label}
            <span className="block text-ink-3">
              {fact.period_start} → {fact.period_end}
            </span>
          </dd>
          <dt className="text-ink-3">Source</dt>
          <dd className="text-ink">
            <Link
              href="/documents"
              className="font-mono text-blue-550 underline decoration-blue-300 underline-offset-2 hover:decoration-blue-550"
            >
              {locator(fact.evidence)}
            </Link>
          </dd>
          <dt className="text-ink-3">Method</dt>
          <dd className="text-ink">{humanize(fact.extraction_method)}</dd>
          <dt className="text-ink-3">Extracted</dt>
          <dd className="text-ink">{formatDate(fact.extracted_at)}</dd>
          <dt className="text-ink-3">Review</dt>
          <dd>
            <Badge tone="neutral">{humanize(fact.review_state)}</Badge>
          </dd>
        </dl>

        {fact.evidence.snippet ? (
          <div>
            <p className="text-xs font-medium text-ink-2">As printed</p>
            <p className="mt-1 border-l-2 border-rule pl-2 font-mono text-xs leading-relaxed text-ink-3">
              {fact.evidence.snippet}
            </p>
          </div>
        ) : null}

        <div>
          <div className="flex items-baseline justify-between gap-2">
            <p className="text-xs font-medium text-ink-2">Confidence by stage</p>
            {limiting ? (
              <p className="text-xs text-ink-3">
                limited by {stageLabel(limiting.stage)}
              </p>
            ) : null}
          </div>
          <div className="mt-2 grid gap-1.5">
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
          <p className="mt-2 text-xs leading-relaxed text-ink-3">
            Kept as three numbers on purpose. A 0.99 answer built on a 0.42 character
            read is a 0.42 fact, and one blended score would hide that.
          </p>
        </div>

        {fact.notes ? (
          <p className="rounded-md border border-hairline bg-plane px-2 py-1.5 text-xs text-ink-2">
            {fact.notes}
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}
