"use client";

/**
 * Ask — the evidence-first query surface.
 *
 * Four paths, one box: exact_figure / comparison / discovery are synchronous JSON
 * over facts; narrative / draft stream prose around pinned facts. Every number
 * the model writes must already be in the citations — the verifier drops any
 * sentence with an invented numeral and the UI marks it flagged, not hidden.
 * Nothing here invents an answer the API did not return.
 */

import {
  AlertTriangle,
  FileSearch,
  MessageSquare,
  Search,
  Send,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ErrorState } from "@/components/ui/states";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { ApiError, query, queryStream, type StreamEvent } from "@/lib/api";
import { entityLabel, formatValue, humanize, locator } from "@/lib/format";
import type { Fact, Passage, QueryResponse } from "@/lib/types";

const EXAMPLES = [
  "SECL coal production FY2024-25",
  "compare coal production across subsidiaries FY2024-25",
  "why did SECL offtake fall in Q2",
  "draft a reply to PQ on SECL production FY2024-25",
  "documents mentioning Gevra",
] as const;

type AskState =
  | { phase: "idle" }
  | { phase: "loading"; question: string; streaming: boolean }
  | { phase: "streaming"; question: string; prose: string; facts: Fact[]; passages: Passage[] }
  | { phase: "done"; response: QueryResponse }
  | { phase: "error"; message: string };

function intentLabel(intent: string): string {
  const labels: Record<string, string> = {
    exact_figure: "Exact figure",
    comparison: "Comparison",
    discovery: "Discovery",
    narrative: "Narrative",
    draft: "Draft",
  };
  return labels[intent] ?? intent;
}

function modelBadge(modelUsed: boolean): string {
  return modelUsed ? "model" : "no model";
}

function ProseBlock({ prose, flagged }: { prose: string; flagged: boolean }) {
  if (!prose) return null;
  return (
    <div className="space-y-2">
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{prose}</p>
      {flagged ? (
        <p className="flex items-center gap-1.5 text-xs text-warning">
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
          A sentence with an unsupported figure was removed. Every number shown was
          already in the citations below.
        </p>
      ) : null}
    </div>
  );
}

function Citations({
  facts,
  passages,
}: {
  facts: Fact[];
  passages: Passage[];
}) {
  if (facts.length === 0 && passages.length === 0) return null;
  return (
    <div className="space-y-4">
      {facts.length > 0 ? (
        <div>
          <p className="text-xs font-medium text-ink-2">
            Pinned facts · {facts.length}
          </p>
          <p className="text-xs text-ink-3">
            The prose was grounded only on these figures. Every number must appear
            verbatim in this list or the retrieved passages.
          </p>
          <div className="mt-2 divide-y divide-hairline rounded-md border border-hairline">
            {facts.map((fact) => (
              <div key={fact.fact_id} className="flex items-start justify-between gap-3 px-3 py-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-ink">
                    {entityLabel(fact.entity_id)} · {humanize(fact.metric)} · {fact.period_label}
                  </p>
                  <p className="font-mono text-xs text-ink-3">{locator(fact.evidence)}</p>
                </div>
                <p className="tnum shrink-0 text-sm font-medium text-ink">
                  {formatValue(fact.value, fact.unit)}
                </p>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {passages.length > 0 ? (
        <div>
          <p className="text-xs font-medium text-ink-2">
            Retrieved passages · {passages.length}
          </p>
          <div className="mt-2 space-y-2">
            {passages.map((passage) => (
              <div
                key={passage.evidence_id}
                className="rounded-md border border-hairline bg-plane px-3 py-2"
              >
                <p className="text-xs font-medium text-ink">
                  {[passage.title ?? passage.filename ?? passage.document_id, passage.page !== null ? `p.${passage.page}` : null]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
                <p className="mt-1 text-xs leading-relaxed text-ink-2">{passage.snippet}</p>
                <p className="mt-1 font-mono text-xs text-ink-3">
                  doc:{passage.document_id}
                  {passage.document_version > 1 ? ` v${passage.document_version}` : ""}
                  {passage.page !== null ? ` p.${passage.page}` : ""}
                </p>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function FigureAnswer({ fact }: { fact: Fact }) {
  return (
    <div className="space-y-3">
      <div>
        <p className="text-xs font-medium text-ink-2">Answer · exact figure</p>
        <p className="tnum mt-1 text-2xl font-semibold text-ink">{formatValue(fact.value, fact.unit)}</p>
        <p className="tnum text-xs text-ink-3">
          as printed {fact.raw_value} {fact.raw_unit} · {fact.period_label}
        </p>
      </div>
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-ink-3">Entity</dt>
        <dd className="text-ink">{entityLabel(fact.entity_id)}</dd>
        <dt className="text-ink-3">Metric</dt>
        <dd className="text-ink">{humanize(fact.metric)}</dd>
        <dt className="text-ink-3">Source</dt>
        <dd className="font-mono text-ink">{locator(fact.evidence)}</dd>
        <dt className="text-ink-3">Evidence</dt>
        <dd className="font-mono text-xs text-ink-3">{fact.evidence.snippet ?? "—"}</dd>
      </dl>
    </div>
  );
}

function ComparisonAnswer({
  points,
  metric,
}: {
  points: { entity_id: string; fiscal_year: string; unit: string; value: number; fact_count: number }[];
  metric: string;
}) {
  if (points.length === 0) return null;
  const unit = points[0]?.unit ?? "";
  return (
    <div className="space-y-3">
      <p className="text-xs font-medium text-ink-2">
        Comparison · {humanize(metric)} · {unit || "—"}
      </p>
      <Table>
        <THead>
          <TR>
            <TH>Entity</TH>
            <TH>Fiscal year</TH>
            <TH numeric>Value</TH>
            <TH numeric>Facts</TH>
          </TR>
        </THead>
        <TBody>
          {points.map((point) => (
            <TR key={`${point.entity_id}:${point.fiscal_year}`}>
              <TD className="font-medium">{entityLabel(point.entity_id)}</TD>
              <TD className="text-ink-2">{point.fiscal_year}</TD>
              <TD numeric>{formatValue(point.value, point.unit)}</TD>
              <TD numeric className="text-ink-2">
                {point.fact_count}
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

function ResponseView({ response }: { response: QueryResponse }) {
  const { intent, model_used, figure, comparison, discovery, narrative, refusal } = response;

  // Refusal — structured, not an exception. Show why, and the evidence behind it.
  if (refusal) {
    const reasonLabels: Record<string, string> = {
      ambiguous_unit: "Ambiguous unit",
      open_conflict: "Open conflict",
      out_of_corpus: "Out of corpus",
      no_validated_fact: "No validated fact",
    };
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Search className="size-4 text-ink-3" aria-hidden />
            {reasonLabels[refusal.reason] ?? refusal.reason}
          </CardTitle>
          <CardDescription>{refusal.message}</CardDescription>
        </CardHeader>
        <CardBody className="space-y-3">
          <p className="flex items-center gap-2 text-xs text-ink-3">
            <Badge tone="neutral">{intentLabel(intent)}</Badge>
            <span>{modelBadge(model_used)}</span>
            <span>question: &ldquo;{response.question}&rdquo;</span>
          </p>
          {refusal.conflict ? (
            <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2">
              <p className="text-xs font-medium text-ink">
                Conflicting values for {entityLabel(refusal.conflict.entity_id)} ·{" "}
                {humanize(refusal.conflict.metric)} · {refusal.conflict.period_label}
              </p>
              <ul className="mt-1 space-y-1">
                {refusal.conflict.facts.map((fact) => (
                  <li key={fact.fact_id} className="text-xs text-ink-2">
                    {formatValue(fact.value, fact.unit)} — {locator(fact.evidence)}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {refusal.facts.length > 0 ? (
            <Citations facts={refusal.facts} passages={[]} />
          ) : null}
        </CardBody>
      </Card>
    );
  }

  if (figure) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Exact figure</CardTitle>
          <CardDescription>
            One validated fact that answers the question exactly.{" "}
            <Badge tone="neutral">{intentLabel(intent)}</Badge>
            <span className="ml-2 text-xs text-ink-3">{modelBadge(model_used)}</span>
          </CardDescription>
        </CardHeader>
        <CardBody>
          <FigureAnswer fact={figure.fact} />
        </CardBody>
      </Card>
    );
  }

  if (comparison) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Comparison</CardTitle>
          <CardDescription>
            Summed from validated facts, grouped by entity and fiscal year.{" "}
            <Badge tone="neutral">{intentLabel(intent)}</Badge>
          </CardDescription>
        </CardHeader>
        <CardBody>
          <ComparisonAnswer points={comparison.points} metric={comparison.metric} />
        </CardBody>
      </Card>
    );
  }

  if (discovery) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileSearch className="size-4 text-ink-3" aria-hidden />
            Places to look
          </CardTitle>
          <CardDescription>
            No single figure answers this — here are the passages the retriever found.{" "}
            <Badge tone="neutral">{intentLabel(intent)}</Badge>
          </CardDescription>
        </CardHeader>
        <CardBody>
          {discovery.passages.length === 0 ? (
            <p className="text-sm text-ink-2">No passages matched.</p>
          ) : (
            <Citations facts={[]} passages={discovery.passages} />
          )}
        </CardBody>
      </Card>
    );
  }

  if (narrative) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="size-4 text-ink-3" aria-hidden />
            {intent === "draft" ? "Draft reply" : "Narrative"}
          </CardTitle>
          <CardDescription>
            Prose written around pinned figures. Verified numeral-by-numeral; invented
            numbers drop their sentence.{" "}
            <Badge tone={model_used ? "info" : "neutral"}>{modelBadge(model_used)}</Badge>
            <Badge tone="neutral" className="ml-2">
              {intentLabel(intent)}
            </Badge>
          </CardDescription>
        </CardHeader>
        <CardBody className="space-y-4">
          <ProseBlock prose={narrative.prose} flagged={narrative.flagged} />
          <Citations facts={narrative.facts} passages={narrative.passages} />
        </CardBody>
      </Card>
    );
  }

  return (
    <ErrorState title="Empty response" detail="The API returned no answer and no refusal." />
  );
}

export default function AskPage() {
  const [input, setInput] = useState("");
  const [preferStream, setPreferStream] = useState(true);
  const [state, setState] = useState<AskState>({ phase: "idle" });
  const anchorRef = useRef<HTMLDivElement>(null);

  const isBusy = state.phase === "loading" || state.phase === "streaming";

  const runQuery = useCallback(
    async (question: string) => {
      const trimmed = question.trim();
      if (!trimmed) return;

      // Only narrative/draft benefit from streaming — but try stream anyway and
      // let the API collapse to JSON when it chooses.
      if (preferStream) {
        setState({ phase: "loading", question: trimmed, streaming: true });
        try {
          const stream = queryStream(trimmed);
          let proseBuffer = "";
          let metaFacts: Fact[] = [];
          let metaPassages: Passage[] = [];
          let sawMeta = false;

          for await (const event of stream as AsyncGenerator<StreamEvent>) {
            if (event.event === "meta") {
              // JSON fallback lands here as meta+done — treat as full response
              const data = event.data as unknown as Record<string, unknown>;
              if (data && "narrative" in data) {
                const full = data as unknown as QueryResponse;
                setState({ phase: "done", response: full });
                return;
              }
              if (data && ("figure" in data || "comparison" in data || "discovery" in data || "refusal" in data)) {
                const full = data as unknown as QueryResponse;
                setState({ phase: "done", response: full });
                return;
              }
              // True prose meta — carry citations and flip to streaming prose
              const typed = event.data as { facts?: Fact[]; passages?: Passage[] };
              metaFacts = (typed.facts as Fact[]) ?? [];
              metaPassages = (typed.passages as Passage[]) ?? [];
              sawMeta = true;
              setState({ phase: "streaming", question: trimmed, prose: "", facts: metaFacts, passages: metaPassages });
            } else if (event.event === "token") {
              proseBuffer += (event.data as { t: string }).t;
              setState((prev) =>
                prev.phase === "streaming" ? { ...prev, prose: proseBuffer } : prev,
              );
            } else if (event.event === "done") {
              const done = event.data as { prose: string; flagged: boolean };
              const narrative: QueryResponse = {
                question: trimmed,
                intent: sawMeta ? "narrative" : "narrative",
                model_used: true,
                figure: null,
                comparison: null,
                discovery: null,
                narrative: {
                  prose: done.prose || proseBuffer,
                  passages: metaPassages,
                  facts: metaFacts,
                  flagged: done.flagged,
                },
                refusal: null,
              };
              setState({ phase: "done", response: narrative });
              return;
            } else if (event.event === "error") {
              const err = event.data as { reason: string; message: string };
              setState({ phase: "error", message: err.message || err.reason });
              return;
            }
          }

          // Stream ended without done — fall back to blocking query
          const fallback = await query(trimmed);
          setState({ phase: "done", response: fallback });
        } catch (error) {
          const message =
            error instanceof ApiError
              ? error.detail
              : error instanceof Error
                ? error.message
                : "The API is not reachable. Start it with: python -m uvicorn mrip.main:app --port 8000";
          setState({ phase: "error", message });
        }
        return;
      }

      setState({ phase: "loading", question: trimmed, streaming: false });
      try {
        const response = await query(trimmed);
        setState({ phase: "done", response });
      } catch (error) {
        const message =
          error instanceof ApiError
            ? error.detail
            : error instanceof Error
              ? error.message
              : "The API is not reachable.";
        setState({ phase: "error", message });
      }
    },
    [preferStream],
  );

  useEffect(() => {
    if (state.phase === "streaming" || state.phase === "done" || state.phase === "error") {
      anchorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [state.phase]);

  return (
    <>
      <PageHeader
        title="Ask"
        description="Ask a question over the corpus. Figures come from validated facts with their receipt; prose is written only around pinned numbers and is checked numeral-by-numeral. No answer is invented."
      />

      <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
        {/* Composer — one row above everything it scopes (interaction.md). */}
        <Card>
          <CardBody>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void runQuery(input);
              }}
              className="flex flex-col gap-3"
            >
              <label className="flex flex-col gap-1.5">
                <span className="text-xs font-medium text-ink-2">Question</span>
                <div className="flex gap-2">
                  <Input
                    value={input}
                    onChange={(event) => setInput(event.target.value)}
                    placeholder="e.g. SECL coal production FY2024-25"
                    aria-label="Question"
                    className="flex-1"
                    disabled={isBusy}
                  />
                  <Button type="submit" variant="primary" disabled={isBusy || !input.trim()} pending={isBusy}>
                    <Send className="size-3.5" aria-hidden />
                    Ask
                  </Button>
                </div>
              </label>

              <div className="flex flex-wrap items-center justify-between gap-3">
                <label className="flex items-center gap-2 text-xs text-ink-2">
                  <input
                    type="checkbox"
                    checked={preferStream}
                    onChange={(event) => setPreferStream(event.target.checked)}
                    disabled={isBusy}
                    className="size-3.5 rounded border-rule bg-surface accent-[var(--color-series-1)]"
                  />
                  Stream prose as it arrives
                  <span className="text-ink-3">(narrative / draft only)</span>
                </label>
                <span className="text-xs text-ink-3">
                  Max 2000 characters · the API classifies the intent for you
                </span>
              </div>
            </form>

            <div className="mt-4 flex flex-wrap gap-1.5">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => {
                    setInput(example);
                  }}
                  disabled={isBusy}
                  className="rounded-full border border-hairline bg-plane px-2.5 py-1 text-xs text-ink-2 transition-colors hover:border-rule hover:text-ink disabled:opacity-50"
                >
                  {example}
                </button>
              ))}
            </div>
          </CardBody>
        </Card>

        {/* Result */}
        <div ref={anchorRef} className="scroll-mt-6">
          {state.phase === "idle" ? (
            <Card>
              <CardBody>
                <div className="flex flex-col items-center gap-2 rounded-md border border-dashed border-hairline px-6 py-10 text-center">
                  <MessageSquare className="size-5 text-ink-3" aria-hidden />
                  <p className="text-sm font-medium text-ink">No question yet</p>
                  <p className="max-w-md text-xs leading-relaxed text-ink-2">
                    Try exact_figure (&ldquo;SECL coal production FY2024-25&rdquo;), comparison
                    (&ldquo;compare coal production&hellip;&rdquo;), discovery, or narrative/draft
                    (&ldquo;why did&hellip;&rdquo; / &ldquo;draft a reply&hellip;&rdquo;). The API
                    routes by shape — prose only where you asked for it, and always around pinned
                    evidence.
                  </p>
                </div>
              </CardBody>
            </Card>
          ) : state.phase === "loading" ? (
            <Card>
              <CardBody>
                <p className="flex items-center gap-2 text-sm text-ink-2">
                  <span className="size-2 animate-pulse rounded-full bg-series-1" aria-hidden />
                  Answering &ldquo;{state.question}&rdquo;
                  {state.streaming ? " — streaming…" : "…"}
                </p>
              </CardBody>
            </Card>
          ) : state.phase === "streaming" ? (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Sparkles className="size-4 text-ink-3" aria-hidden />
                  Writing…
                </CardTitle>
                <CardDescription>
                  Streaming around {state.facts.length} pinned fact{state.facts.length === 1 ? "" : "s"} and{" "}
                  {state.passages.length} passage{state.passages.length === 1 ? "" : "s"}.
                </CardDescription>
              </CardHeader>
              <CardBody className="space-y-4">
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                  {state.prose || "…"}
                  <span className="ml-0.5 inline-block size-2 animate-pulse rounded-sm bg-series-1 align-middle" aria-hidden />
                </p>
                <Citations facts={state.facts} passages={state.passages} />
              </CardBody>
            </Card>
          ) : state.phase === "done" ? (
            <ResponseView response={state.response} />
          ) : (
            <ErrorState title="Cannot answer that question" detail={state.message} />
          )}
        </div>
      </main>
    </>
  );
}
