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

/**
 * A row, optionally selectable.
 *
 * **`onSelect` rather than `onClick`, and that is the whole point.** These rows
 * were clickable and nothing else: a `<tr>` carrying a click handler with no
 * `tabIndex` and no key handler cannot be reached from a keyboard, so the
 * detail panel on the documents, facts and topics pages was mouse-only. That
 * is WCAG 2.1.1 at Level A, and on these three pages it is not a polish item —
 * selecting a row *is* the navigation.
 *
 * Taking a dedicated `onSelect` instead of leaving `onClick` to the caller is
 * what makes that unforgettable: a selectable row cannot be wired up now
 * without also being focusable and key-activatable, because the component does
 * both or neither.
 *
 * The row stays a row. `role="button"` would make a screen reader stop
 * announcing it as a row with cells, trading one barrier for another, and
 * `role="grid"` would promise arrow-key navigation that is not implemented.
 * Selection is conveyed with `aria-current`, which is valid on any element and
 * means exactly this — the current item within a set.
 */
export function TR({
  interactive,
  selected,
  onSelect,
  className,
  ...props
}: React.ComponentProps<"tr"> & {
  interactive?: boolean;
  selected?: boolean;
  /** Makes the row selectable by mouse *and* keyboard. */
  onSelect?: () => void;
}) {
  const selectable = Boolean(onSelect) || interactive;

  return (
    <tr
      className={cn(
        selectable &&
          "cursor-pointer transition-colors hover:bg-plane focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-550",
        selected && "bg-series-1/5 hover:bg-series-1/5",
        className,
      )}
      // Only focusable when it actually does something: a tab stop on every
      // row of a three-thousand-row corpus list, most of them inert, would be
      // its own accessibility problem.
      tabIndex={onSelect ? 0 : undefined}
      aria-current={selected ? true : undefined}
      onClick={onSelect}
      onKeyDown={
        onSelect
          ? (event) => {
              if (event.key !== "Enter" && event.key !== " ") return;
              // Space scrolls the page by default, and a row that scrolls
              // instead of selecting is worse than one that does nothing.
              event.preventDefault();
              onSelect();
            }
          : undefined
      }
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
