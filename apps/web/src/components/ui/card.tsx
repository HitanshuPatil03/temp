/**
 * Surface primitives.
 *
 * Hairline rings rather than shadows, and a card surface one shade off the page
 * plane. The chrome stays recessive so the figures are the loudest thing on
 * screen — a dashboard whose borders compete with its numbers is harder to read
 * at a glance, which is the only thing a dashboard is for.
 */

import { cn } from "@/lib/utils";

export function Card({
  className,
  ...props
}: React.ComponentProps<"section">) {
  return (
    <section
      className={cn(
        "rounded-lg border border-hairline bg-surface",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({
  className,
  ...props
}: React.ComponentProps<"header">) {
  return (
    <header
      className={cn("flex items-start justify-between gap-4 px-5 pt-4 pb-3", className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return (
    <h2
      className={cn("text-sm font-semibold tracking-tight text-ink", className)}
      {...props}
    />
  );
}

export function CardDescription({
  className,
  ...props
}: React.ComponentProps<"p">) {
  return (
    <p className={cn("mt-1 text-xs leading-relaxed text-ink-2", className)} {...props} />
  );
}

export function CardBody({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("px-5 pb-5", className)} {...props} />;
}

export function CardFooter({ className, ...props }: React.ComponentProps<"footer">) {
  return (
    <footer
      className={cn(
        "flex items-center gap-3 border-t border-hairline px-5 py-3 text-xs text-ink-2",
        className,
      )}
      {...props}
    />
  );
}
