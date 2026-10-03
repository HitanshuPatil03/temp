"use client";

/**
 * Documents — the corpus, and the evidence each page yielded.
 *
 * A document row is not the interesting part; the page-level evidence is. Selecting
 * a document opens a page walker, because "which page did this figure come from"
 * is the question this screen exists to answer.
 *
 * Version chains are shown explicitly. A superseded document stays queryable, so
 * "what did the FY23 report say before it was revised?" has an answer.
 */

import { FileText, Files, Layers, RotateCcw } from "lucide-react";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";

import { PageHeader } from "@/components/shell/page-header";
import { Badge, SyntheticBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Select } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { EmptyState, ErrorState, Refetching, Skeleton } from "@/components/ui/states";
import { api, ApiError, fetcher, keys } from "@/lib/api";
import { formatBytes, formatDate, formatNumber, humanize } from "@/lib/format";
import { INGEST_STAGES, latestStageProgress } from "@/lib/types";
import type { EvidenceSpan, IngestStage, MripDocument } from "@/lib/types";

import { UploadPanel } from "./upload-panel";

/** Lifecycle state → how it reads on a badge. */
const STATE_TONE: Record<string, "neutral" | "good" | "warning" | "critical"> = {
  received: "neutral",
  classified: "neutral",
  digitized: "neutral",
  extracted: "neutral",
  normalized: "neutral",
  validated: "neutral",
  indexed: "neutral",
  ready: "good",
  failed: "critical",
  quarantined: "critical",
};

/** States a document can still move out of on its own. */
const IN_FLIGHT = new Set<string>([
  "received",
  "classified",
  "digitized",
  "extracted",
  "normalized",
  "validated",
  "indexed",
]);

export default function DocumentsPage() {
  const documents = useSWR<MripDocument[]>(keys.documents(), fetcher, {
    // Poll only while something is actually moving. A 400-page scan takes
    // minutes, and an officer watching a static "digitized" badge cannot tell
    // working from stuck — which is when they re-upload. Once every document is
    // terminal there is nothing to see, so the polling stops rather than asking
    // a question whose answer cannot change.
    refreshInterval: (latest) =>
      latest?.some((doc) => IN_FLIGHT.has(doc.state)) ? 3000 : 0,
  });
  const [selected, setSelected] = useState<MripDocument | null>(null);

  return (
    <>
      <PageHeader
        title="Documents"
        description="Every ingested source, content-addressed by SHA-256. Re-uploading an identical file is a no-op; the same logical report with different bytes becomes a new version in the revision chain."
      />

      <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
        <UploadPanel />

        <Card>
          <CardHeader>
            <CardTitle>Corpus</CardTitle>
            <CardDescription>
              Select a document to walk its pages and read the evidence spans
              extracted from each.
            </CardDescription>
          </CardHeader>
          <CardBody>
            {documents.error ? (
              <ErrorState
                title="Cannot read the document registry"
                detail={
                  documents.error instanceof ApiError
                    ? documents.error.detail
                    : "The API is not reachable. Start it with: python -m uvicorn mrip.main:app --port 8000"
                }
              />
            ) : documents.isLoading ? (
              <Skeleton className="h-48" />
            ) : (documents.data?.length ?? 0) === 0 ? (
              <EmptyState icon={Files} title="No documents ingested">
                Upload one above. It is hashed, checked at the boundary, then read
                through the pipeline — classify, digitize, extract, normalize,
                validate, index — and every figure it yields will cite the page and
                cell it came from.
              </EmptyState>
            ) : (
              <Refetching active={documents.isValidating}>
                <Table>
                  <THead>
                    <TR>
                      <TH>Document</TH>
                      <TH>State</TH>
                      <TH>Class</TH>
                      <TH numeric>Ver.</TH>
                      <TH numeric>Pages</TH>
                      <TH numeric>Size</TH>
                      <TH>Fiscal year</TH>
                      <TH>Ingested</TH>
                    </TR>
                  </THead>
                  <TBody>
                    {documents.data?.map((doc) => (
                      <TR
                        key={doc.document_id}
                        interactive
                        selected={selected?.document_id === doc.document_id}
                        onClick={() => setSelected(doc)}
                      >
                        <TD>
                          <span className="flex items-center gap-2">
                            <FileText
                              className="size-3.5 shrink-0 text-ink-3"
                              aria-hidden
                            />
                            <span className="min-w-0">
                              <span className="block truncate font-medium text-ink">
                                {doc.title ?? doc.filename}
                              </span>
                              <span className="block truncate text-xs text-ink-3">
                                {doc.filename}
                              </span>
                            </span>
                            {doc.is_synthetic ? <SyntheticBadge /> : null}
                          </span>
                        </TD>
                        <TD>
                          <Badge tone={STATE_TONE[doc.state] ?? "neutral"}>
                            {humanize(doc.state)}
                          </Badge>
                          {doc.failed_stage ? (
                            <span
                              className="ml-1 text-xs text-critical"
                              title={doc.failed_reason ?? undefined}
                            >
                              at {doc.failed_stage}
                            </span>
                          ) : null}
                          <StageProgressBar doc={doc} />
                        </TD>
                        <TD>
                          <Badge tone="neutral">{humanize(doc.doc_class)}</Badge>
                        </TD>
                        <TD numeric>
                          {doc.version}
                          {doc.supersedes ? (
                            <span
                              className="ml-1 text-xs text-ink-3"
                              title={`Supersedes ${doc.supersedes}`}
                            >
                              ↑
                            </span>
                          ) : null}
                        </TD>
                        <TD numeric className="text-ink-2">
                          {doc.page_count ?? "—"}
                        </TD>
                        <TD numeric className="text-ink-2">
                          {formatBytes(doc.size_bytes)}
                        </TD>
                        <TD className="text-ink-2">{doc.fiscal_year ?? "—"}</TD>
                        <TD className="text-ink-2">
                          {formatDate(doc.ingested_at)}
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </Refetching>
            )}
          </CardBody>
        </Card>

        {/* Keyed on the document so selecting a different one gets a fresh
            component rather than inheriting the last one's local state. Without
            it the page walker stays on page 47 when you move to a two-page
            letter, and the failure panel keeps the previous document's chosen
            stage and queued job id. */}
        {selected ? (
          <FailurePanel key={`fail-${selected.document_id}`} doc={selected} />
        ) : null}
        {selected ? (
          <EvidenceWalker key={`pages-${selected.document_id}`} doc={selected} />
        ) : null}
      </main>
    </>
  );
}

/**
 * What to do about a document that did not finish.
 *
 * Rendered only for `failed` and `quarantined`, and the two are deliberately not
 * symmetric:
 *
 * **Failed is retryable.** Every stage replaces its own output rather than
 * appending to it, so re-running one produces the same rows and not a second
 * copy — which is what makes "try it again" a safe button rather than a
 * duplication risk. It defaults to the stage that failed, because the common
 * cause is transient (an OOM on a 400-page scan), but an earlier stage can be
 * chosen when the cause is not: a misclassified spreadsheet has to go back to
 * `classify`, not to the extractor that was reading the wrong shape.
 *
 * **Quarantined is not.** The lifecycle has no transition out of it, because
 * whatever made the bytes unsafe — an encrypted PDF, an archive that expands
 * past its stated size — is still true of those bytes. "Try again" on a
 * decompression bomb is not a recovery strategy. Offering a button that 409s
 * would be worse than offering none, so this says what to do instead.
 *
 * The reason is rendered as text. It was previously a `title` tooltip, which is
 * invisible on a touch device, invisible to a screen reader, and the one sentence
 * the officer's next action depends on.
 */
function FailurePanel({ doc }: { doc: MripDocument }) {
  const { mutate } = useSWRConfig();
  const [stage, setStage] = useState<IngestStage>(
    (doc.failed_stage as IngestStage) ?? "classify",
  );
  const [pending, setPending] = useState(false);
  const [queued, setQueued] = useState<{ stage: string; job_id: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (doc.state !== "failed" && doc.state !== "quarantined") return null;

  async function retry() {
    setPending(true);
    setError(null);
    setQueued(null);
    try {
      const outcome = await api.retryDocument(doc.document_id, stage);
      setQueued({ stage: outcome.stage, job_id: outcome.job_id });
      await mutate(keys.documents());
      await mutate(keys.summary());
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.detail
          : "The API did not respond. Check that the backend is running.",
      );
    } finally {
      setPending(false);
    }
  }

  if (doc.state === "quarantined") {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Stopped at the boundary</CardTitle>
          <CardDescription>
            {doc.title ?? doc.filename} was refused before ingestion and is not
            re-processed: whatever made these bytes unsafe is still true of them.
          </CardDescription>
        </CardHeader>
        <CardBody>
          {doc.failed_reason ? (
            <p className="text-sm leading-relaxed text-critical">{doc.failed_reason}</p>
          ) : null}
          <p className="mt-3 text-xs leading-relaxed text-ink-2">
            Ask the subsidiary for a copy without the protection or the archive, and
            upload that as a new document. Nothing is lost by leaving this row here —
            it is the record that the file arrived and why it was not used.
          </p>
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Ingestion failed{doc.failed_stage ? ` at ${doc.failed_stage}` : ""}
        </CardTitle>
        <CardDescription>
          {doc.title ?? doc.filename} — the stages before this one finished, so a
          re-run starts from here rather than from the beginning.
        </CardDescription>
      </CardHeader>
      <CardBody className="space-y-4">
        {doc.failed_reason ? (
          <p className="rounded-md border border-critical/30 bg-critical/5 px-3 py-2.5 font-mono text-xs leading-relaxed whitespace-pre-wrap text-critical">
            {doc.failed_reason}
          </p>
        ) : null}

        <div className="flex flex-wrap items-end gap-3">
          <Field
            label="Re-run from"
            hint="Earlier stages re-do work that already succeeded"
          >
            <Select
              value={stage}
              onChange={(event) => setStage(event.target.value as IngestStage)}
            >
              {INGEST_STAGES.map((item) => (
                <option key={item} value={item}>
                  {humanize(item)}
                  {item === doc.failed_stage ? " — where it failed" : ""}
                </option>
              ))}
            </Select>
          </Field>
          <Button variant="primary" pending={pending} onClick={retry}>
            <RotateCcw className="size-3.5" aria-hidden />
            Re-run
          </Button>
        </div>

        {error ? (
          <p role="alert" className="text-xs leading-relaxed text-critical">
            {error}
          </p>
        ) : null}

        {queued ? (
          <p className="text-xs leading-relaxed text-ink-2">
            Queued from <span className="font-medium text-ink">{queued.stage}</span> as{" "}
            <span className="font-mono">{queued.job_id}</span>. A worker picks it up
            within a second or two; the state in the table above changes as it
            progresses.
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

/**
 * "OCR 142 / 400", while it is happening.
 *
 * The counters have always been written — each stage publishes them in its own
 * transaction precisely so they are visible before the stage commits — but
 * nothing read them, so a 400-page scan showed a static badge for several
 * minutes with no way to tell working from stuck. That is the state in which an
 * officer re-uploads the same file.
 *
 * Read from the row the table already has rather than from
 * `/documents/{id}/progress`, which would be one request per visible row to
 * learn something the list response already carries.
 *
 * Hidden once the document is terminal: a finished document's last counter is
 * history, and `142 / 400` beside a green "ready" badge reads like a failure.
 */
function StageProgressBar({ doc }: { doc: MripDocument }) {
  if (!IN_FLIGHT.has(doc.state)) return null;

  const progress = latestStageProgress(doc.stage_progress);
  if (progress === null) return null;

  const percent = Math.min(100, Math.round((progress.done / progress.total) * 100));

  return (
    <span className="mt-1 flex items-center gap-1.5">
      <span
        className="h-1 w-10 shrink-0 overflow-hidden rounded-full bg-plane"
        role="progressbar"
        aria-valuenow={progress.done}
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-label={`${progress.stage}: ${progress.done} of ${progress.total}`}
      >
        <span
          className="block h-full rounded-full bg-series-1 transition-[width] duration-500"
          style={{ width: `${percent}%` }}
        />
      </span>
      <span className="tnum text-xs text-ink-3">
        {humanize(progress.stage)} {formatNumber(progress.done)} /{" "}
        {formatNumber(progress.total)}
      </span>
    </span>
  );
}

function EvidenceWalker({ doc }: { doc: MripDocument }) {
  const [page, setPage] = useState(1);
  const pageCount = doc.page_count ?? 1;

  const evidence = useSWR<EvidenceSpan[]>(
    keys.pageEvidence(doc.document_id, page),
    fetcher,
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="min-w-0">
            <CardTitle>Evidence · page {page}</CardTitle>
            <CardDescription>
              {doc.title ?? doc.filename} · version {doc.version} ·{" "}
              <span className="font-mono text-xs">
                {doc.content_hash.slice(0, 12)}…
              </span>
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={page <= 1}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              ← Previous
            </Button>
            <span className="tnum text-xs text-ink-2">
              {page} / {pageCount}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={page >= pageCount}
              onClick={() => setPage((current) => Math.min(pageCount, current + 1))}
            >
              Next →
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardBody>
        {evidence.error ? (
          <ErrorState
            title="Cannot read this page's evidence"
            detail={
              evidence.error instanceof ApiError
                ? evidence.error.detail
                : "The API is not reachable."
            }
            refusal={
              evidence.error instanceof ApiError && evidence.error.isRefusal
            }
          />
        ) : evidence.isLoading ? (
          <Skeleton className="h-32" />
        ) : (evidence.data?.length ?? 0) === 0 ? (
          <EmptyState icon={Layers} title={`No evidence spans on page ${page}`}>
            Either the page carries no extractable text, or digitization has not run
            for this version.
          </EmptyState>
        ) : (
          <Refetching active={evidence.isValidating}>
            <Table>
              <THead>
                <TR>
                  <TH>Text</TH>
                  <TH>Kind</TH>
                  <TH>Cell</TH>
                  <TH>Method</TH>
                  <TH numeric>OCR conf.</TH>
                </TR>
              </THead>
              <TBody>
                {evidence.data?.map((span) => (
                  <TR key={span.evidence_id}>
                    <TD>
                      <span className="block max-w-xl truncate font-mono text-xs text-ink">
                        {span.text ?? "—"}
                      </span>
                    </TD>
                    <TD className="text-ink-2">
                      {span.kind ? humanize(span.kind) : "—"}
                    </TD>
                    <TD className="font-mono text-xs text-ink-2">
                      {span.table_id
                        ? `${span.table_id}${span.cell_ref ? `!${span.cell_ref}` : ""}`
                        : "—"}
                    </TD>
                    <TD className="text-ink-2">
                      {span.extraction_method
                        ? humanize(span.extraction_method)
                        : "—"}
                    </TD>
                    <TD numeric className="text-ink-2">
                      {span.ocr_confidence === null
                        ? "n/a"
                        : formatNumber(span.ocr_confidence, 2)}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </Refetching>
        )}
      </CardBody>
    </Card>
  );
}
