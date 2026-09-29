/**
 * Stat tiles and meters.
 *
 * A single headline number is a stat tile, not a one-bar bar chart. The values
 * use the font's proportional figures on purpose — `tabular-nums` gives every
 * digit the width of a zero, which makes a figure like `121` look loose at
 * display sizes. Tabular spacing belongs in the table columns, not here.
 *
 * The meter's unfilled track is a lighter step of the *same* blue ramp as its
 * fill, so the state reads across the whole bar rather than only where the fill
 * stops.
 */

import type { LucideIcon } from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";

export function StatTile({
  label,
  value,
  caption,
  icon: Icon,
  tone = "default",
  href,
}: {
  label: string;
  value: string | number;
  caption?: string;
  icon?: LucideIcon;
  /** `attention` is for a count a reviewer is expected to act on. */
  tone?: "default" | "attention";
  href?: string;
}) {
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-ink-2">{label}</span>
        {Icon ? (
          <Icon
            className={cn(
              "size-4 shrink-0",
              tone === "attention" ? "text-serious" : "text-ink-3",
            )}
            aria-hidden
          />
        ) : null}
      </div>
      <p
        className={cn(
          "mt-2 text-3xl leading-none font-semibold",
          tone === "attention" && Number(value) > 0 ? "text-critical" : "text-ink",
        )}
      >
        {value}
      </p>
      {caption ? (
        <p className="mt-1.5 text-xs leading-snug text-ink-3">{caption}</p>
      ) : null}
    </>
  );

  const shell = cn(
    "block rounded-lg border border-hairline bg-surface px-4 py-3.5",
    href && "transition-colors hover:border-rule hover:bg-plane",
  );

  return href ? (
    <Link href={href} className={shell}>
      {body}
    </Link>
  ) : (
    <div className={shell}>{body}</div>
  );
}

/**
 * One confidence stage against the review threshold.
 *
 * The threshold is drawn as a tick on the track, so a reviewer can see not just
 * that a fact is in the queue but where the line is that put it there.
 */
export function Meter({
  label,
  score,
  threshold,
  className,
}: {
  label: string;
  /** `null` when this stage did not apply — rendered as "n/a", never as zero. */
  score: number | null;
  threshold?: number;
  className?: string;
}) {
  if (score === null) {
    return (
      <div className={cn("flex items-baseline gap-2 text-xs", className)}>
        <span className="w-14 shrink-0 text-ink-2">{label}</span>
        <span className="text-ink-3 italic">not applicable</span>
      </div>
    );
  }

  const below = threshold !== undefined && score < threshold;

  return (
    <div className={cn("flex items-center gap-2 text-xs", className)}>
      <span className="w-14 shrink-0 text-ink-2">{label}</span>
      <div className="relative h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-blue-100">
        <div
          className={cn(
            "h-full rounded-full",
            below ? "bg-warning" : "bg-series-1",
          )}
          style={{ width: `${Math.max(2, score * 100)}%` }}
        />
        {threshold !== undefined ? (
          <span
            className="absolute inset-y-0 w-0.5 bg-ink/35"
            style={{ left: `${threshold * 100}%` }}
            aria-hidden
          />
        ) : null}
      </div>
      <span
        className={cn(
          "tnum w-9 shrink-0 text-right",
          below ? "font-medium text-ink" : "text-ink-2",
        )}
      >
        {score.toFixed(2)}
      </span>
    </div>
  );
}
