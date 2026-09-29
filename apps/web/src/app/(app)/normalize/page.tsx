"use client";

/**
 * Normalizer playground.
 *
 * This screen exists to be argued with. Type `wibbles` as a unit and the API answers
 * 422 with a reason; ask whether `FY2024-25` is comparable to `CY2024` and it says no
 * and why. Those refusals are the product — a system that silently guesses `MT` means
 * million tonnes will eventually publish a figure that is off by a factor of a
 * million.
 *
 * So refusals render as warning-toned *information*, not as crashes.
 */

import { ArrowRight } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";

import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { ErrorState, Skeleton } from "@/components/ui/states";
import { ApiError, fetcher, keys } from "@/lib/api";
import { formatNumber, humanize } from "@/lib/format";
import type {
  Comparability,
  MTConvention,
  NormalizedPeriod,
  NormalizedQuantity,
  ResolvedEntity,
} from "@/lib/types";

export default function NormalizePage() {
  return (
    <>
      <PageHeader
        title="Normalizer"
        description="Units, fiscal periods and CIL entity aliases resolved to canonical form — and refused, with a reason, when the input is genuinely ambiguous."
      />

      <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
        <div className="grid gap-6 xl:grid-cols-2">
          <QuantityCard />
          <PeriodCard />
          <ComparabilityCard />
          <EntityCard />
        </div>
      </main>
    </>
  );
}

/** Shared result shell: a loading skeleton, a refusal, or the children. */
function Result({
  swrKey,
  error,
  isLoading,
  children,
}: {
  swrKey: string | null;
  error: unknown;
  isLoading: boolean;
  children: React.ReactNode;
}) {
  if (!swrKey) return null;
  if (error) {
    const refusal = error instanceof ApiError ? error : null;
    return (
      <div className="mt-4">
        <ErrorState
          title={
            refusal?.isRefusal
              ? "The normalizer declined to guess"
              : "Request failed"
          }
          detail={refusal?.detail ?? "The API is not reachable."}
          refusal={refusal?.isRefusal ?? false}
        />
      </div>
    );
  }
  if (isLoading) return <Skeleton className="mt-4 h-24" />;
  return <div className="mt-4">{children}</div>;
}

function Row({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-ink-3">{term}</dt>
      <dd className="text-ink">{children}</dd>
    </>
  );
}

function Facts({ children }: { children: React.ReactNode }) {
  return (
    <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1.5 text-xs">
      {children}
    </dl>
  );
}

function Note({ note }: { note: string | null }) {
  if (!note) return null;
  return (
    <p className="mt-3 rounded-md border border-hairline bg-plane px-2.5 py-2 text-xs leading-relaxed text-ink-2">
      {note}
    </p>
  );
}

// ------------------------------------------------------------------ quantity

function QuantityCard() {
  const [value, setValue] = useState("3.2");
  const [unit, setUnit] = useState("lakh tonnes");
  const [convention, setConvention] = useState<MTConvention | "">("");
  const [swrKey, setKey] = useState<string | null>(null);

  const { data, error, isLoading } = useSWR<NormalizedQuantity>(swrKey, fetcher);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Quantity</CardTitle>
        <CardDescription>
          Indian numbering aware — lakh is 10<sup>5</sup>, crore is 10
          <sup>7</sup>. <code className="font-mono">MT</code> is treated as
          ambiguous and resolved from the stated convention rather than assumed.
        </CardDescription>
      </CardHeader>
      <CardBody>
        <form
          className="grid gap-3 sm:grid-cols-[6rem_1fr_9rem_auto] sm:items-end"
          onSubmit={(event) => {
            event.preventDefault();
            setKey(
              keys.quantity(
                Number(value),
                unit,
                convention === "" ? undefined : convention,
              ),
            );
          }}
        >
          <Field label="Value">
            <Input
              type="number"
              step="any"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              required
            />
          </Field>
          <Field label="Unit">
            <Input
              value={unit}
              onChange={(event) => setUnit(event.target.value)}
              placeholder="MT, lakh tonnes, MTPA, Mm³, ha…"
              required
            />
          </Field>
          <Field label="MT convention">
            <Select
              value={convention}
              onChange={(event) =>
                setConvention(event.target.value as MTConvention | "")
              }
            >
              <option value="">unspecified</option>
              <option value="million_tonnes">million tonnes</option>
              <option value="metric_tonne">metric tonne</option>
            </Select>
          </Field>
          <Button type="submit">
            Normalize <ArrowRight className="size-3.5" aria-hidden />
          </Button>
        </form>

        <Result swrKey={swrKey} error={error} isLoading={isLoading}>
          {data ? (
            <>
              <Facts>
                <Row term="Canonical">
                  <span className="tnum font-semibold">
                    {formatNumber(data.value, 4)} {data.unit}
                  </span>
                </Row>
                <Row term="As given">
                  <span className="tnum">
                    {data.raw_value} {data.raw_unit}
                  </span>
                </Row>
                <Row term="Dimension">{humanize(data.dimension)}</Row>
                <Row term="Ambiguous">
                  <Badge tone={data.ambiguous ? "warning" : "good"}>
                    {data.ambiguous ? "Yes — convention matters" : "No"}
                  </Badge>
                </Row>
              </Facts>
              <Note note={data.note} />
            </>
          ) : null}
        </Result>
      </CardBody>
    </Card>
  );
}

// -------------------------------------------------------------------- period

function PeriodCard() {
  const [raw, setRaw] = useState("FY2024-25");
  const [swrKey, setKey] = useState<string | null>(null);

  const { data, error, isLoading } = useSWR<NormalizedPeriod>(swrKey, fetcher);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Period</CardTitle>
        <CardDescription>
          The Indian fiscal year runs 1 April to 31 March. A calendar year is never
          silently treated as a fiscal one.
        </CardDescription>
      </CardHeader>
      <CardBody>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            setKey(keys.period(raw));
          }}
        >
          <Field label="Period as written" className="min-w-56 flex-1">
            <Input
              value={raw}
              onChange={(event) => setRaw(event.target.value)}
              placeholder="FY2024-25, 2024-25, Q3 FY24, CY2023, Apr–Sep 2024…"
              required
            />
          </Field>
          <Button type="submit">
            Resolve <ArrowRight className="size-3.5" aria-hidden />
          </Button>
        </form>

        <Result swrKey={swrKey} error={error} isLoading={isLoading}>
          {data ? (
            <>
              <Facts>
                <Row term="Label">
                  <span className="font-semibold">{data.label}</span>
                </Row>
                <Row term="Range">
                  <span className="tnum">
                    {data.start} → {data.end}
                  </span>{" "}
                  <span className="text-ink-3">({data.days} days)</span>
                </Row>
                <Row term="Kind">{humanize(data.kind)}</Row>
                <Row term="Fiscal">
                  <Badge tone={data.is_fiscal ? "info" : "neutral"}>
                    {data.is_fiscal
                      ? (data.fiscal_year ?? "fiscal")
                      : "calendar — not a fiscal year"}
                  </Badge>
                </Row>
                <Row term="Ambiguous">
                  <Badge tone={data.ambiguous ? "warning" : "good"}>
                    {data.ambiguous ? "Yes" : "No"}
                  </Badge>
                </Row>
              </Facts>
              <Note note={data.note} />
            </>
          ) : null}
        </Result>
      </CardBody>
    </Card>
  );
}

// ------------------------------------------------------------- comparability

function ComparabilityCard() {
  const [left, setLeft] = useState("FY2024-25");
  const [right, setRight] = useState("CY2024");
  const [swrKey, setKey] = useState<string | null>(null);

  const { data, error, isLoading } = useSWR<Comparability>(swrKey, fetcher);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Comparability</CardTitle>
        <CardDescription>
          Whether two periods may be placed on the same axis. This is the check that
          stops a fiscal-year figure being charted against a calendar-year one.
        </CardDescription>
      </CardHeader>
      <CardBody>
        <form
          className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
          onSubmit={(event) => {
            event.preventDefault();
            setKey(keys.comparable(left, right));
          }}
        >
          <Field label="Left period">
            <Input
              value={left}
              onChange={(event) => setLeft(event.target.value)}
              required
            />
          </Field>
          <Field label="Right period">
            <Input
              value={right}
              onChange={(event) => setRight(event.target.value)}
              required
            />
          </Field>
          <Button type="submit">
            Compare <ArrowRight className="size-3.5" aria-hidden />
          </Button>
        </form>

        <Result swrKey={swrKey} error={error} isLoading={isLoading}>
          {data ? (
            <Facts>
              <Row term="Verdict">
                <Badge tone={data.comparable ? "good" : "serious"}>
                  {data.comparable ? "Comparable" : "Not comparable"}
                </Badge>
              </Row>
              <Row term="Left">
                <span className="font-mono text-xs">{data.left}</span>
              </Row>
              <Row term="Right">
                <span className="font-mono text-xs">{data.right}</span>
              </Row>
              {data.reason ? <Row term="Reason">{data.reason}</Row> : null}
            </Facts>
          ) : null}
        </Result>
      </CardBody>
    </Card>
  );
}

// -------------------------------------------------------------------- entity

function EntityCard() {
  const [query, setQuery] = useState("S.E.C.L.");
  const [swrKey, setKey] = useState<string | null>(null);

  const { data, error, isLoading } = useSWR<ResolvedEntity>(swrKey, fetcher);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Entity</CardTitle>
        <CardDescription>
          Resolves CIL aliases to one canonical id so cross-document comparison is
          meaningful. SCCL and NLCIL are deliberately <em>not</em> CIL subsidiaries and
          the resolver says so.
        </CardDescription>
      </CardHeader>
      <CardBody>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            setKey(keys.entity(query));
          }}
        >
          <Field label="Name or alias" className="min-w-56 flex-1">
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="SECL, South Eastern Coalfields Ltd, CMPDI, NEC, SCCL…"
              required
            />
          </Field>
          <Button type="submit">
            Resolve <ArrowRight className="size-3.5" aria-hidden />
          </Button>
        </form>

        <Result swrKey={swrKey} error={error} isLoading={isLoading}>
          {data ? (
            <>
              <Facts>
                <Row term="Canonical id">
                  <span className="font-mono text-xs font-semibold">
                    {data.entity_id}
                  </span>
                </Row>
                <Row term="Name">
                  {data.name} <span className="text-ink-3">({data.code})</span>
                </Row>
                <Row term="Kind">{humanize(data.kind)}</Row>
                <Row term="CIL group">
                  <Badge tone={data.is_cil_group ? "info" : "neutral"}>
                    {data.is_cil_group ? "Yes" : "No — outside Coal India"}
                  </Badge>
                </Row>
                {data.parent ? <Row term="Parent">{data.parent}</Row> : null}
                {data.headquarters ? (
                  <Row term="Headquarters">
                    {data.headquarters}
                    {data.state ? `, ${data.state}` : ""}
                  </Row>
                ) : null}
                <Row term="Matched on">
                  <span className="font-mono text-xs">{data.matched_on}</span>{" "}
                  <span className="text-ink-3">
                    ({data.exact ? "exact" : `fuzzy, score ${data.score.toFixed(2)}`})
                  </span>
                </Row>
                <Row term="Needs review">
                  <Badge tone={data.needs_review ? "warning" : "good"}>
                    {data.needs_review ? "Yes — confirm before use" : "No"}
                  </Badge>
                </Row>
              </Facts>
              <Note note={data.note} />
            </>
          ) : null}
        </Result>
      </CardBody>
    </Card>
  );
}
