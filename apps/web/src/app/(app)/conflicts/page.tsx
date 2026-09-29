"use client";

/**
 * Conflict radar — the adjudication surface.
 *
 * Detection is an explicit action rather than something that happens invisibly on
 * ingest, because its materiality threshold is a judgement call: at 0.5% a rounding
 * difference between a press release and an annual report is a conflict, at 5% it
 * isn't. The reviewer sets the threshold and sees what it produced.
 *
 * Nothing here merges two figures. The only write is "this fact wins, and here is
 * why", recorded against a person.
 */

import { GitCompareArrows, RefreshCw, ShieldCheck } from "lucide-react";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";

import { ConflictCard } from "@/components/conflicts/conflict-card";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { EmptyState, ErrorState, Refetching, Skeleton } from "@/components/ui/states";
import { ApiError, api, fetcher, keys } from "@/lib/api";
import { formatPercent } from "@/lib/format";
import type { ConflictGroup, DashboardSummary } from "@/lib/types";

export default function ConflictsPage() {
  const { mutate } = useSWRConfig();
  const summary = useSWR<DashboardSummary>(keys.summary(), fetcher);
  const conflicts = useSWR<ConflictGroup[]>(keys.conflicts(), fetcher);

  const shipped = summary.data?.thresholds.conflict_material_spread;
  const [spread, setSpread] = useState<string>("");
  const [detecting, setDetecting] = useState(false);
  const [detectError, setDetectError] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<number | null>(null);

  const effectiveSpread = spread === "" ? shipped : Number(spread) / 100;

  async function runDetection() {
    setDetecting(true);
    setDetectError(null);
    try {
      const found = await api.detectConflicts(
        spread === "" ? undefined : Number(spread) / 100,
      );
      setLastRun(found.length);
      await Promise.all([mutate(keys.conflicts()), mutate(keys.summary())]);
    } catch (cause) {
      setDetectError(
        cause instanceof ApiError ? cause.detail : "Detection could not run.",
      );
    } finally {
      setDetecting(false);
    }
  }

  async function resolve(conflictId: string, winningFactId: string) {
    await api.resolveConflict(conflictId, winningFactId);
    await Promise.all([
      mutate(keys.conflicts()),
      mutate(keys.summary()),
      // The losing fact becomes `superseded`, so any fact list is now stale.
      mutate(
        (key) => typeof key === "string" && key.startsWith("/facts"),
        undefined,
        { revalidate: true },
      ),
    ]);
  }

  const open = conflicts.data ?? [];

  return (
    <>
      <PageHeader
        title="Conflict radar"
        description="Two documents reporting different values for the same entity, metric, unit and period. MRIP surfaces both and refuses to pick — resolution is a recorded human decision."
      />

      <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
        <Card>
          <CardHeader>
            <CardTitle>Run detection</CardTitle>
            <CardDescription>
              Compares every fact sharing an{" "}
              <code className="font-mono text-xs">
                (entity, metric, unit, period)
              </code>{" "}
              key. A group is only raised when the relative spread exceeds the
              materiality threshold, so identical figures and rounding noise do not
              fill the queue.
            </CardDescription>
          </CardHeader>
          <CardBody>
            <div className="flex flex-wrap items-end gap-4">
              <Field
                label="Materiality threshold (%)"
                hint={
                  shipped !== undefined
                    ? `Backend default is ${formatPercent(shipped, 2)}. Leave blank to use it.`
                    : undefined
                }
                className="w-56"
              >
                <Input
                  type="number"
                  min={0}
                  max={100}
                  step={0.05}
                  value={spread}
                  placeholder={
                    shipped !== undefined ? (shipped * 100).toFixed(2) : "0.50"
                  }
                  onChange={(event) => setSpread(event.target.value)}
                />
              </Field>
              <Button onClick={runDetection} pending={detecting}>
                <RefreshCw className="size-3.5" aria-hidden /> Detect conflicts
              </Button>
              {lastRun !== null ? (
                <Badge tone="info">
                  {lastRun === 0
                    ? "No material disagreement found"
                    : `${lastRun} group${lastRun === 1 ? "" : "s"} raised`}
                </Badge>
              ) : null}
            </div>
            {detectError ? (
              <p className="mt-3 text-xs text-critical">{detectError}</p>
            ) : null}
          </CardBody>
        </Card>

        <section aria-label="Open conflicts">
          {conflicts.error ? (
            <ErrorState
              title="Cannot read the conflict radar"
              detail={
                conflicts.error instanceof ApiError
                  ? conflicts.error.detail
                  : "The API is not reachable. Start it with: python -m uvicorn mrip.main:app --port 8000"
              }
            />
          ) : conflicts.isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-56" />
              <Skeleton className="h-56" />
            </div>
          ) : open.length === 0 ? (
            <EmptyState icon={ShieldCheck} title="No open conflicts">
              Nothing is awaiting adjudication. Either no two sources disagree
              materially, or detection has not been run over this corpus — use the
              control above.
            </EmptyState>
          ) : (
            <Refetching active={conflicts.isValidating}>
              <div className="space-y-3">
                <p className="flex items-center gap-2 text-xs text-ink-2">
                  <GitCompareArrows className="size-3.5 text-serious" aria-hidden />
                  {open.length} group{open.length === 1 ? "" : "s"} awaiting a
                  decision
                  {effectiveSpread !== undefined ? (
                    <> at a {formatPercent(effectiveSpread, 2)} threshold</>
                  ) : null}
                </p>
                {open.map((conflict) => (
                  <ConflictCard
                    key={conflict.conflict_id}
                    conflict={conflict}
                    materialSpread={effectiveSpread}
                    reviewThreshold={summary.data?.thresholds.review_confidence}
                    onResolved={resolve}
                  />
                ))}
              </div>
            </Refetching>
          )}
        </section>
      </main>
    </>
  );
}
