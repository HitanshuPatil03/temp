"use client";

/**
 * Reports — PS deliverable 1.
 *
 * The screen's argument is that a report is not a mail merge. Every figure
 * shows the fact and the `document@version` behind it; a figure that could not
 * be pinned is listed as *missing with a reason* rather than left blank; and
 * "what has changed since?" is a button, not a week of archaeology.
 *
 * Generating is deliberately an explicit action rather than something that
 * happens on page load, for the same reason conflict detection is: it writes.
 */

import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import {
  AlertTriangle,
  Download,
  FileSpreadsheet,
  FileText,
  Presentation,
  RefreshCw,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/shell/page-header";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/states";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { ApiError, api, fetcher, keys } from "@/lib/api";
import { formatNumber, humanize } from "@/lib/format";
import type { FigureDelta, ReportManifest, ReportState } from "@/lib/types";

/** The lifecycle, as the UI offers it. Mirrors `LEGAL_TRANSITIONS` server-side;
 *  the server is the authority and refuses anything this gets wrong. */
const NEXT_STATE: Record<ReportState, ReportState | null> = {
  draft: "in_review",
  in_review: "approved",
  approved: "published",
  published: null,
};

const STATE_TONE: Record<ReportState, "neutral" | "info" | "good" | "warning"> = {
  draft: "neutral",
  in_review: "warning",
  approved: "info",
  published: "good",
};

const FORMATS = [
  { fmt: "docx", label: "Word", icon: FileText },
  { fmt: "xlsx", label: "Excel", icon: FileSpreadsheet },
  { fmt: "pptx", label: "Slides", icon: Presentation },
  { fmt: "md", label: "Markdown", icon: Download },
] as const;

export default function ReportsPage() {
  const { mutate } = useSWRConfig();
  const [entity, setEntity] = useState("SECL");
  const [period, setPeriod] = useState("FY2024-25");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const list = useSWR<ReportManifest[]>(keys.reports(), fetcher);

  async function generate() {
    setGenerating(true);
    setError(null);
    try {
      const created = await api.generateReport(entity, period);
      setSelected(created.report_id);
      await mutate(keys.reports());
    } catch (cause) {
      setError(
        cause instanceof ApiError ? cause.detail : "Could not generate the report.",
      );
    } finally {
      setGenerating(false);
    }
  }

  async function advance(report: ReportManifest) {
    const target = NEXT_STATE[report.state];
    if (!target) return;
    setError(null);
    try {
      await api.transitionReport(report.report_id, target);
      await mutate(keys.reports());
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : "Could not update the state.");
    }
  }

  const reports = list.data ?? [];
  const current = reports.find((item) => item.report_id === selected) ?? reports[0];

  return (
    <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
      <PageHeader
        title="Reports"
        description="Every figure pinned to the fact and the source version behind it."
      />

      <Card>
        <CardHeader>
          <CardTitle>Generate</CardTitle>
          <CardDescription>
            Figures come from the fact store, never from the model. A required
            figure with no validated fact is reported as missing rather than
            filled in.
          </CardDescription>
        </CardHeader>
        <CardBody>
          <div className="flex flex-wrap items-end gap-3">
            <label className="min-w-40 flex-1">
              <span className="mb-1 block text-xs font-medium text-ink-2">Entity</span>
              <Input
                aria-label="Entity"
                value={entity}
                onChange={(event) => setEntity(event.target.value)}
                placeholder="SECL"
              />
            </label>
            <label className="min-w-40 flex-1">
              <span className="mb-1 block text-xs font-medium text-ink-2">Period</span>
              <Input
                aria-label="Period"
                value={period}
                onChange={(event) => setPeriod(event.target.value)}
                placeholder="FY2024-25"
              />
            </label>
            <Button onClick={generate} pending={generating} disabled={generating}>
              Generate report
            </Button>
          </div>
          {error ? (
            <p className="mt-3 text-sm text-amber-700" role="status">
              {error}
            </p>
          ) : null}
        </CardBody>
      </Card>

      {list.error ? (
        <ErrorState
          title="Could not load reports"
          detail={
            list.error instanceof ApiError ? list.error.detail : "Request failed."
          }
        />
      ) : list.isLoading ? (
        <Skeleton className="h-40" />
      ) : reports.length === 0 ? (
        <EmptyState
          icon={FileText}
          title="No reports yet"
        >
          Generate one above. It will be stored with its evidence pinned.
        </EmptyState>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <Card>
            <CardHeader>
              <CardTitle>Generated</CardTitle>
              <CardDescription>{reports.length} in your scope</CardDescription>
            </CardHeader>
            <CardBody className="space-y-1 p-2">
              {reports.map((report) => (
                <button
                  key={report.report_id}
                  type="button"
                  onClick={() => setSelected(report.report_id)}
                  aria-current={report.report_id === current?.report_id}
                  className={
                    "w-full rounded-md px-3 py-2 text-left transition-colors " +
                    (report.report_id === current?.report_id
                      ? "bg-series-1/10"
                      : "hover:bg-plane")
                  }
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium text-ink">
                      {report.title}
                    </span>
                    <Badge tone={STATE_TONE[report.state]}>
                      {humanize(report.state)}
                    </Badge>
                  </span>
                  <span className="mt-0.5 block text-xs text-ink-3">
                    {report.figures.length} figures
                    {report.missing_required.length > 0
                      ? ` · ${report.missing_required.length} missing`
                      : ""}
                  </span>
                </button>
              ))}
            </CardBody>
          </Card>

          {current ? <ReportDetail report={current} onAdvance={advance} /> : null}
        </div>
      )}
    </main>
  );
}

function ReportDetail({
  report,
  onAdvance,
}: {
  report: ReportManifest;
  onAdvance: (report: ReportManifest) => void;
}) {
  const complete = report.missing_required.length === 0;
  const next = NEXT_STATE[report.state];

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>{report.title}</CardTitle>
          <CardDescription>
            {report.template_id} v{report.template_version} · generated{" "}
            {new Date(report.generated_at).toLocaleString("en-IN")}
          </CardDescription>
        </CardHeader>
        <CardBody className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={STATE_TONE[report.state]}>{humanize(report.state)}</Badge>
            {next ? (
              <Button variant="outline" onClick={() => onAdvance(report)}>
                Move to {humanize(next)}
              </Button>
            ) : (
              <span className="text-xs text-ink-3">
                Published reports are immutable — a correction is a new report.
              </span>
            )}
          </div>

          {complete ? (
            <div className="flex flex-wrap gap-2">
              {FORMATS.map(({ fmt, label, icon: Icon }) => (
                <a
                  key={fmt}
                  href={api.reportDownloadUrl(report.report_id, fmt)}
                  className="inline-flex items-center gap-1.5 rounded-md border border-hairline px-2.5 py-1.5 text-xs font-medium text-ink-2 transition-colors hover:bg-plane hover:text-ink"
                >
                  <Icon className="size-3.5" aria-hidden />
                  {label}
                </a>
              ))}
            </div>
          ) : (
            <p className="flex items-start gap-2 rounded-md bg-amber-50 p-3 text-sm text-amber-800">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                This report will not render: a required figure has no validated
                fact. Listed below with the reason — it is not left blank and it
                is not filled in by the model.
              </span>
            </p>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Pinned figures</CardTitle>
          <CardDescription>
            Each row carries the fact id and the document version it came from.
          </CardDescription>
        </CardHeader>
        <CardBody className="p-0">
          {report.figures.length === 0 ? (
            <p className="px-4 py-6 text-sm text-ink-3">No figures pinned.</p>
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH>Figure</TH>
                  <TH>Value</TH>
                  <TH>As printed</TH>
                  <TH>Source</TH>
                </TR>
              </THead>
              <TBody>
                {report.figures.map((figure) => (
                  <TR key={figure.fact_id}>
                    <TD>{figure.label}</TD>
                    <TD className="tabular-nums">
                      {formatNumber(figure.value)} {figure.unit}
                    </TD>
                    <TD className="tabular-nums text-ink-2">
                      {figure.raw_value} {figure.raw_unit}
                    </TD>
                    <TD>
                      <code className="text-xs text-ink-2">{figure.locator}</code>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardBody>
      </Card>

      {report.missing_required.length > 0 || report.missing_optional.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Not pinned</CardTitle>
            <CardDescription>
              Required fields block the render; optional ones are simply omitted.
            </CardDescription>
          </CardHeader>
          <CardBody className="p-0">
            <Table>
              <THead>
                <TR>
                  <TH>Figure</TH>
                  <TH>Requirement</TH>
                  <TH>Reason</TH>
                </TR>
              </THead>
              <TBody>
                {report.missing_required.map((item) => (
                  <TR key={`req-${item.label}`}>
                    <TD>{item.label}</TD>
                    <TD>
                      <Badge tone="warning">Required</Badge>
                    </TD>
                    <TD className="text-ink-2">{item.message}</TD>
                  </TR>
                ))}
                {report.missing_optional.map((item) => (
                  <TR key={`opt-${item.label}`}>
                    <TD>{item.label}</TD>
                    <TD>
                      <Badge tone="neutral">Optional</Badge>
                    </TD>
                    <TD className="text-ink-2">{humanize(item.reason)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardBody>
        </Card>
      ) : null}

      <DiffPanel reportId={report.report_id} />
    </div>
  );
}

/**
 * "Why has last year's number changed?" — each pinned figure re-resolved
 * against the corpus as it stands now. Loaded on demand rather than with the
 * report, because it is a question the reader asks, not one every view needs.
 */
function DiffPanel({ reportId }: { reportId: string }) {
  const [open, setOpen] = useState(false);
  const { data, error, isLoading } = useSWR<FigureDelta[]>(
    open ? keys.reportDiff(reportId) : null,
    fetcher,
  );

  const moved = (data ?? []).filter((delta) => delta.changed);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Changes since generation</CardTitle>
        <CardDescription>
          Re-resolves every pinned figure against the corpus as it is now.
        </CardDescription>
      </CardHeader>
      <CardBody>
        {!open ? (
          <Button variant="outline" onClick={() => setOpen(true)}>
            <RefreshCw className="mr-1.5 size-3.5" aria-hidden />
            Check for changes
          </Button>
        ) : error ? (
          <ErrorState
            title="Could not compute the diff"
            detail={error instanceof ApiError ? error.detail : "Request failed."}
          />
        ) : isLoading ? (
          <Skeleton className="h-20" />
        ) : moved.length === 0 ? (
          <p className="text-sm text-ink-2">
            Nothing has moved. Every figure still resolves to the value and the
            source version this report pinned.
          </p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Figure</TH>
                <TH>As approved</TH>
                <TH>Now</TH>
                <TH>What moved</TH>
              </TR>
            </THead>
            <TBody>
              {moved.map((delta) => (
                <TR key={delta.label}>
                  <TD>{delta.label}</TD>
                  <TD className="tabular-nums">
                    {formatNumber(delta.approved_value)}{" "}
                    <span className="text-ink-3">
                      v{delta.approved_document_version}
                    </span>
                  </TD>
                  <TD className="tabular-nums">
                    {delta.current_value === null
                      ? "—"
                      : formatNumber(delta.current_value)}{" "}
                    {delta.current_document_version ? (
                      <span className="text-ink-3">
                        v{delta.current_document_version}
                      </span>
                    ) : null}
                  </TD>
                  <TD className="text-ink-2">{delta.note}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardBody>
    </Card>
  );
}
