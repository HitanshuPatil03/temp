"use client";

import { cn } from "@/lib/utils";

export function Field({
  label,
  hint,
  className,
  children,
}: {
  label: string;
  hint?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-xs font-medium text-ink-2">{label}</span>
      {children}
      {hint ? <span className="text-xs text-ink-3">{hint}</span> : null}
    </label>
  );
}

const control = cn(
  "h-9 w-full rounded-md border border-rule bg-surface px-3 text-sm text-ink",
  "placeholder:text-ink-3 outline-none transition-colors",
  "focus-visible:border-series-1 focus-visible:ring-2 focus-visible:ring-series-1/30",
);

export function Input({ className, ...props }: React.ComponentProps<"input">) {
  return <input className={cn(control, className)} {...props} />;
}

export function Select({ className, ...props }: React.ComponentProps<"select">) {
  return <select className={cn(control, "pr-8", className)} {...props} />;
}

export function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      className={cn(control, "h-auto min-h-[4.5rem] resize-y py-2 leading-relaxed", className)}
      {...props}
    />
  );
}
