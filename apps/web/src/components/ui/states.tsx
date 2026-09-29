/**
 * Loading, empty and error states.
 *
 * `Refetching` holds the previous render at reduced opacity rather than swapping
 * in a skeleton: re-fetching a filtered table should not make the page jump, and
 * a skeleton flash on every keystroke reads as breakage.
 */

import type { LucideIcon } from "lucide-react";
import { AlertTriangle, Info } from "lucide-react";

import { cn } from "@/lib/utils";

export function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-hairline/70", className)}
      {...props}
    />
  );
}

export function Refetching({
  active,
  className,
  children,
}: {
  active: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn("transition-opacity", active && "opacity-60", className)}
      aria-busy={active}
    >
      {children}
    </div>
  );
}

export function EmptyState({
  icon: Icon = Info,
  title,
  children,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-2 rounded-md border border-dashed border-hairline",
        "px-6 py-10 text-center",
        className,
      )}
    >
      <Icon className="size-5 text-ink-3" aria-hidden />
      <p className="text-sm font-medium text-ink">{title}</p>
      {children ? (
        <p className="max-w-md text-xs leading-relaxed text-ink-2">{children}</p>
      ) : null}
    </div>
  );
}

/**
 * A backend refusal (404/422) is rendered as information, not as a failure: the
 * normalizer declining to guess a unit is the product working correctly, and it
 * should not look like a crash.
 */
export function ErrorState({
  title,
  detail,
  refusal = false,
}: {
  title: string;
  detail?: string;
  refusal?: boolean;
}) {
  return (
    <div
      role="status"
      className={cn(
        "flex items-start gap-2.5 rounded-md border px-4 py-3 text-sm",
        refusal
          ? "border-warning/40 bg-warning/10 text-ink"
          : "border-critical/30 bg-critical/5 text-ink",
      )}
    >
      <AlertTriangle
        className={cn("mt-0.5 size-4 shrink-0", refusal ? "text-warning" : "text-critical")}
        aria-hidden
      />
      <div className="min-w-0">
        <p className="font-medium">{title}</p>
        {detail ? (
          <p className="mt-0.5 leading-relaxed break-words text-ink-2">{detail}</p>
        ) : null}
      </div>
    </div>
  );
}
