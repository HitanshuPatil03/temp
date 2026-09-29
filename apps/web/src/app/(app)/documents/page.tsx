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

import { FileText, Files, Layers } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";

import { PageHeader } from "@/components/shell/page-header";
import { Badge, SyntheticBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { EmptyState, ErrorState, Refetching, Skeleton } from "@/components/ui/states";
import { ApiError, fetcher, keys } from "@/lib/api";
import { formatBytes, formatDate, formatNumber, humanize } from "@/lib/format";
import type { EvidenceSpan, MripDocument } from "@/lib/types";

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

export default function DocumentsPage() {
  const documents = useSWR<MripDocument[]>(keys.documents(), fetcher);
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

        {selected ? <EvidenceWalker doc={selected} /> : null}
      </main>
    </>
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
