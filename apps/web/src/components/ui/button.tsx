"use client";

import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

const button = cva(
  cn(
    "inline-flex items-center justify-center gap-2 rounded-md font-medium",
    "transition-colors outline-none",
    "focus-visible:ring-2 focus-visible:ring-series-1/50 focus-visible:ring-offset-2",
    "focus-visible:ring-offset-surface",
    "disabled:pointer-events-none disabled:opacity-50",
  ),
  {
    variants: {
      variant: {
        primary: "bg-series-1 text-white hover:bg-blue-550",
        outline: "border border-rule bg-surface text-ink hover:bg-plane",
        ghost: "text-ink-2 hover:bg-plane hover:text-ink",
        /** For the one action a reviewer cannot undo without a second decision. */
        danger: "border border-critical/40 bg-critical/5 text-critical hover:bg-critical/10",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-9 px-4 text-sm",
      },
    },
    defaultVariants: { variant: "outline", size: "md" },
  },
);

export function Button({
  className,
  variant,
  size,
  pending,
  children,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof button> & { pending?: boolean }) {
  return (
    <button
      className={cn(button({ variant, size }), className)}
      disabled={props.disabled || pending}
      {...props}
    >
      {pending ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
      {children}
    </button>
  );
}
