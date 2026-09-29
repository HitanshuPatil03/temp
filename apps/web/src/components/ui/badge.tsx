/**
 * Status badges.
 *
 * Every status variant renders an icon *and* a word. That is not decoration: the
 * status palette's warning and serious steps sit below 3:1 against this surface,
 * so colour alone would be unreadable for some users and invisible in print. The
 * colour is the fast channel; the word is the reliable one.
 */

import type { LucideIcon } from "lucide-react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Eye,
  GitCompareArrows,
  XCircle,
} from "lucide-react";

import type { FactStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

type Tone = "neutral" | "good" | "warning" | "serious" | "critical" | "info";

const TONE: Record<Tone, string> = {
  neutral: "border-hairline bg-plane text-ink-2",
  good: "border-good/30 bg-good/10 text-success-ink",
  warning: "border-warning/40 bg-warning/15 text-ink",
  serious: "border-serious/40 bg-serious/15 text-ink",
  critical: "border-critical/30 bg-critical/10 text-critical",
  info: "border-series-1/30 bg-series-1/10 text-blue-550",
};

export function Badge({
  tone = "neutral",
  icon: Icon,
  className,
  children,
  ...props
}: React.ComponentProps<"span"> & { tone?: Tone; icon?: LucideIcon }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5",
        "text-xs font-medium whitespace-nowrap",
        TONE[tone],
        className,
      )}
      {...props}
    >
      {Icon ? <Icon className="size-3.5 shrink-0" aria-hidden /> : null}
      {children}
    </span>
  );
}

const FACT_STATUS: Record<FactStatus, { tone: Tone; icon: LucideIcon; label: string }> = {
  validated: { tone: "good", icon: CheckCircle2, label: "Validated" },
  extracted: { tone: "neutral", icon: CircleDashed, label: "Extracted" },
  needs_review: { tone: "warning", icon: Eye, label: "Needs review" },
  conflicted: { tone: "serious", icon: GitCompareArrows, label: "Conflicted" },
  superseded: { tone: "neutral", icon: CircleDashed, label: "Superseded" },
  rejected: { tone: "critical", icon: XCircle, label: "Rejected" },
};

export function StatusBadge({ status }: { status: FactStatus }) {
  const { tone, icon, label } = FACT_STATUS[status];
  return (
    <Badge tone={tone} icon={icon}>
      {label}
    </Badge>
  );
}

/** Shown wherever a figure's source unit had more than one plausible reading. */
export function AmbiguityBadge({ title }: { title?: string }) {
  return (
    <Badge tone="warning" icon={AlertTriangle} title={title}>
      Unit ambiguous
    </Badge>
  );
}

/** Demo data must never be mistaken for an authentic government source. */
export function SyntheticBadge() {
  return (
    <Badge tone="info" icon={CircleDashed} title="Generated for demonstration">
      Synthetic
    </Badge>
  );
}
