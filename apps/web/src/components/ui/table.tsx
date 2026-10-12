/**
 * Table primitives.
 *
 * `tnum` on every numeric cell: figures that stack vertically must align, or the
 * eye cannot compare column-wise, which is the whole reason the data is in a
 * table. (Standalone hero figures do the opposite — see `StatTile`.)
 */

import { cn } from "@/lib/utils";

export function Table({
  stickyHead,
  className,
  ...props
}: React.ComponentProps<"table"> & { stickyHead?: boolean }) {
  return (
    <div
      className={cn(
        "w-full overflow-x-auto",
        // A sticky header needs a scroll container with a height to stick
        // inside. The corpus list runs to thousands of rows, and a column of
        // bare dates and hashes with the header scrolled away is unreadable —
        // you cannot tell `ingested` from `published` without it.
        stickyHead && "max-h-[70vh] overflow-y-auto",
      )}
    >
      <table
        className={cn("w-full border-collapse text-left text-sm", className)}
        {...props}
      />
    </div>
  );
}

export function THead({
  sticky,
  className,
  ...props
}: React.ComponentProps<"thead"> & { sticky?: boolean }) {
  return (
    <thead
      className={cn(
        "text-ink-2",
        // `sticky-head` is defined in globals.css, which puts the positioning
        // on the `th` cells rather than here. See the comment there: Tailwind
        // silently declined to generate the inset-shadow utility this needs,
        // and sticky on a `<thead>` is the less well-supported of the two
        // placements. The border moves with it so the two never stack.
        sticky ? "sticky-head" : "border-b border-hairline",
        className,
      )}
      {...props}
    />
  );
}

export function TH({
  numeric,
  className,
  ...props
}: React.ComponentProps<"th"> & { numeric?: boolean }) {
  return (
    <th
      scope="col"
      className={cn(
        "px-3 py-2 text-xs font-medium whitespace-nowrap",
        numeric && "text-right",
        className,
      )}
      {...props}
    />
  );
}

export function TBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody className={cn("divide-y divide-hairline", className)} {...props} />
  );
}

export function TR({
  interactive,
  selected,
  className,
  ...props
}: React.ComponentProps<"tr"> & { interactive?: boolean; selected?: boolean }) {
  return (
    <tr
      className={cn(
        interactive && "cursor-pointer transition-colors hover:bg-plane",
        selected && "bg-series-1/5 hover:bg-series-1/5",
        className,
      )}
      {...props}
    />
  );
}

export function TD({
  numeric,
  className,
  ...props
}: React.ComponentProps<"td"> & { numeric?: boolean }) {
  return (
    <td
      className={cn(
        "px-3 py-2.5 align-middle",
        numeric && "tnum text-right",
        className,
      )}
      {...props}
    />
  );
}
