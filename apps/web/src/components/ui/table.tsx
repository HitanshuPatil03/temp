/**
 * Table primitives.
 *
 * `tnum` on every numeric cell: figures that stack vertically must align, or the
 * eye cannot compare column-wise, which is the whole reason the data is in a
 * table. (Standalone hero figures do the opposite — see `StatTile`.)
 */

import { cn } from "@/lib/utils";

export function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div className="w-full overflow-x-auto">
      <table
        className={cn("w-full border-collapse text-left text-sm", className)}
        {...props}
      />
    </div>
  );
}

export function THead({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      className={cn("border-b border-hairline text-ink-2", className)}
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
  return <tbody className={cn("divide-y divide-hairline", className)} {...props} />;
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
