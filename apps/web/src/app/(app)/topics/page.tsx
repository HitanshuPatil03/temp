"use client";

/**
 * Word cloud and topics — PS deliverable 2.
 *
 * The whole argument of this screen is in one sentence from ARCHITECTURE §12:
 * *a word cloud is decoration unless every term is a query.* So clicking a term
 * does not filter a picture — it runs a scoped query and lists the documents
 * that produced it, which the documents view then takes down to the page.
 *
 * Three things follow the house chart rules (§15) rather than the usual
 * word-cloud conventions:
 *
 * - **Size encodes document frequency, never raw count.** One repetitive
 *   annexure must not dominate the corpus with its own boilerplate.
 * - **Every term has a table twin.** No value is reachable only by hovering, so
 *   the counts are listed as well as drawn.
 * - **Colour is not the encoding.** Terms carry one hue; weight and size carry
 *   the signal, which keeps the cloud readable without colour vision.
 *
 * The cloud is laid out with plain SVG rather than `d3-cloud`: the deployment
 * forbids runtime egress and every dependency is licence-reviewed, so a sized,
 * wrapped term list that needs no new package is the better trade.
 */

import { useMemo, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { FileText, Hash, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/shell/page-header";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/states";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { ApiError, api, fetcher, keys } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { CloudTerm, TermDocument } from "@/lib/types";

/** Type scale for the cloud. Five steps rather than a continuous ramp: a reader
 *  can tell five sizes apart, and a continuous one just looks noisy. */
const SIZES = [13, 16, 20, 26, 34];

/** One hue, five weights. Identity is the word itself — colour would be a
 *  second encoding of the same variable, which the chart rules forbid. */
const TONES = [
  "text-ink-3",
  "text-ink-2",
  "text-ink",
  "text-blue-550",
  "text-blue-550 font-semibold",
];

function bucket(value: number, min: number, max: number): number {
  if (max <= min) return 2;
  const ratio = (value - min) / (max - min);
  return Math.min(SIZES.length - 1, Math.floor(ratio * SIZES.length));
}

export default function TopicsPage() {
  const { mutate } = useSWRConfig();
  const [fiscalYear, setFiscalYear] = useState("");
  const [entityId, setEntityId] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const query = {
    fiscal_year: fiscalYear || undefined,
    entity_id: entityId || undefined,
  };
  const cloud = useSWR<CloudTerm[]>(keys.cloud(query), fetcher);

  async function extract() {
    setExtracting(true);
    setNotice(null);
    try {
      const result = await api.extractTopics();
      setNotice(
        `Extracted ${formatNumber(result.terms_written)} keyphrases from ` +
          `${formatNumber(result.documents)} documents.`,
      );
      await mutate((key) => typeof key === "string" && key.startsWith("/topics"));
    } catch (cause) {
      setNotice(
        cause instanceof ApiError ? cause.detail : "Could not run extraction.",
      );
    } finally {
      setExtracting(false);
    }
  }

  const terms = useMemo(() => cloud.data ?? [], [cloud.data]);
  const [min, max] = useMemo(() => {
    if (terms.length === 0) return [0, 0];
    const counts = terms.map((term) => term.document_count);
    return [Math.min(...counts), Math.max(...counts)];
  }, [terms]);

  return (
    <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
      <PageHeader
        title="Topics"
        description="What the corpus is about — and every term is a query into it."
      />

      <Card>
        <CardHeader>
          <CardTitle>Filters</CardTitle>
          <CardDescription>
            Filters are part of the query, not a post-filter on a picture:
            narrowing to a year re-weights the cloud, because document frequency
            within that year is a different number.
          </CardDescription>
        </CardHeader>
        <CardBody>
          <div className="flex flex-wrap items-end gap-3">
            <label className="min-w-40">
              <span className="mb-1 block text-xs font-medium text-ink-2">
                Fiscal year
              </span>
              <Input
                aria-label="Fiscal year"
                value={fiscalYear}
                onChange={(event) => setFiscalYear(event.target.value)}
                placeholder="FY2024-25"
              />
            </label>
            <label className="min-w-40">
              <span className="mb-1 block text-xs font-medium text-ink-2">Entity</span>
              <Input
                aria-label="Entity"
                value={entityId}
                onChange={(event) => setEntityId(event.target.value)}
                placeholder="secl"
              />
            </label>
            <Button variant="outline" onClick={extract} pending={extracting}>
              <RefreshCw className="mr-1.5 size-3.5" aria-hidden />
              Re-extract corpus
            </Button>
          </div>
          {notice ? (
            <p className="mt-3 text-sm text-ink-2" role="status">
              {notice}
            </p>
          ) : null}
        </CardBody>
      </Card>

      {cloud.error ? (
        <ErrorState
          title="Could not load the cloud"
          detail={
            cloud.error instanceof ApiError ? cloud.error.detail : "Request failed."
          }
        />
      ) : cloud.isLoading ? (
        <Skeleton className="h-64" />
      ) : terms.length === 0 ? (
        <EmptyState
          icon={Hash}
          title="No keyphrases yet"
        >
          Run extraction above to build the cloud from your corpus.
        </EmptyState>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_24rem]">
          <div className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle>Word cloud</CardTitle>
                <CardDescription>
                  Sized by how many documents use a term — not how often it
                  appears. Select one to see where it came from.
                </CardDescription>
              </CardHeader>
              <CardBody>
                <ul className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  {terms.map((term) => {
                    const step = bucket(term.document_count, min, max);
                    return (
                      <li key={term.term}>
                        <button
                          type="button"
                          onClick={() => setSelected(term.term)}
                          aria-pressed={selected === term.term}
                          title={`${term.term} — in ${term.document_count} documents, ${term.occurrences} occurrences`}
                          className={
                            "rounded px-1 transition-colors hover:bg-plane " +
                            TONES[step] +
                            (selected === term.term ? " bg-series-1/10" : "")
                          }
                          style={{ fontSize: `${SIZES[step]}px`, lineHeight: 1.5 }}
                        >
                          {term.term}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </CardBody>
            </Card>

            {/* The table twin. No value in this product is reachable only by
                hovering a mark — §15. */}
            <Card>
              <CardHeader>
                <CardTitle>Terms</CardTitle>
                <CardDescription>
                  The same data as the cloud, as numbers. Document count is the
                  size; occurrences is the raw count.
                </CardDescription>
              </CardHeader>
              <CardBody className="p-0">
                <Table>
                  <THead>
                    <TR>
                      <TH>Term</TH>
                      <TH>Documents</TH>
                      <TH>Occurrences</TH>
                    </TR>
                  </THead>
                  <TBody>
                    {terms.slice(0, 40).map((term) => (
                      <TR
                        key={term.term}
                        interactive
                        selected={selected === term.term}
                        onClick={() => setSelected(term.term)}
                      >
                        <TD>{term.term}</TD>
                        <TD className="tabular-nums">{term.document_count}</TD>
                        <TD className="tabular-nums text-ink-2">
                          {formatNumber(term.occurrences)}
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </CardBody>
            </Card>
          </div>

          <TermPanel term={selected} query={query} />
        </div>
      )}
    </main>
  );
}

/**
 * Where a term came from. This is the part that makes the cloud an index: the
 * §12 gate is that no term is a dead end, and this panel is where that is
 * either true or visibly false.
 */
function TermPanel({
  term,
  query,
}: {
  term: string | null;
  query: Record<string, string | undefined>;
}) {
  const { data, error, isLoading } = useSWR<TermDocument[]>(
    term ? keys.termDocuments(term, query) : null,
    fetcher,
  );

  if (!term) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Drill through</CardTitle>
          <CardDescription>
            Select a term to see the documents that produced it.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{term}</CardTitle>
        <CardDescription>
          {data ? `${data.length} documents in your scope` : "Looking…"}
        </CardDescription>
      </CardHeader>
      <CardBody className="space-y-2">
        {error ? (
          <ErrorState
            title="Could not load documents"
            detail={error instanceof ApiError ? error.detail : "Request failed."}
          />
        ) : isLoading ? (
          <Skeleton className="h-32" />
        ) : (data ?? []).length === 0 ? (
          <p className="text-sm text-ink-2">
            No documents in your scope use this term.
          </p>
        ) : (
          (data ?? []).map((document) => (
            <a
              key={`${document.document_id}-${document.document_version}`}
              href={`/documents?document=${document.document_id}`}
              className="block rounded-md border border-hairline px-3 py-2 transition-colors hover:bg-plane"
            >
              <span className="flex items-start gap-2">
                <FileText className="mt-0.5 size-4 shrink-0 text-ink-3" aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-ink">
                    {document.title ?? document.filename ?? document.document_id}
                  </span>
                  <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-ink-3">
                    {document.fiscal_year ? (
                      <Badge tone="neutral">{document.fiscal_year}</Badge>
                    ) : null}
                    <span>{document.occurrences} occurrences</span>
                  </span>
                </span>
              </span>
            </a>
          ))
        )}
      </CardBody>
    </Card>
  );
}
