"use client";

/**
 * Page header, plus a live backend indicator.
 *
 * The indicator exists because the two processes start independently. Without
 * it, a cold FastAPI process looks identical to an empty warehouse — every table
 * is empty either way — and the first thing a demo needs is to distinguish
 * "no data yet" from "nothing is listening".
 */

import { Circle } from "lucide-react";
import useSWR from "swr";

import { fetcher, keys } from "@/lib/api";
import type { Health } from "@/lib/types";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4 border-b border-hairline bg-surface px-8 py-5">
      <div className="min-w-0">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold tracking-tight text-ink">{title}</h1>
          <BackendStatus />
        </div>
        {description ? (
          <p className="mt-1 max-w-3xl text-sm leading-relaxed text-ink-2">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}

function BackendStatus() {
  const { data, error, isLoading } = useSWR<Health>(keys.health(), fetcher, {
    refreshInterval: 15_000,
    shouldRetryOnError: true,
  });

  const state = isLoading
    ? { tone: "text-ink-3", label: "connecting…" }
    : error
      ? { tone: "text-critical", label: "API offline" }
      : { tone: "text-good", label: data?.app ?? "API online" };

  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-ink-2"
      title={
        error
          ? "Start the backend: python -m uvicorn mrip.main:app --port 8000"
          : undefined
      }
    >
      <Circle className={cn("size-2 fill-current", state.tone)} aria-hidden />
      {state.label}
    </span>
  );
}
