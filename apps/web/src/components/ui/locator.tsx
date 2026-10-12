/**
 * Identifiers that get transcribed.
 *
 * An evidence locator is the product's signature surface: `doc:doc_93d6 p.1
 * p1t1 r1c2` is what an officer reads out on a call, copies into an email, or
 * types into a search box to find the cell a figure came from. A citation that
 * cannot be transcribed reliably is not a citation.
 *
 * So these are not styled as decoration. Plex Mono with `slashed-zero` and
 * tabular figures makes `0` unmistakable from `O` and `1` from `l`, and the
 * one-step size reduction says *reference material* rather than competing with
 * the figure it belongs to.
 *
 * `select-all` is the whole reason this is a component rather than a class:
 * one click selects the entire identifier instead of a word of it. Someone
 * copying a content hash by double-clicking gets four characters, and nothing
 * about that failure looks like a failure.
 */

import { cn } from "@/lib/utils";

export function Locator({
  children,
  className,
  title,
}: {
  children: React.ReactNode;
  className?: string;
  /** Spelled out on hover when the visible text is truncated. */
  title?: string;
}) {
  return (
    <span className={cn("locator select-all", className)} title={title}>
      {children}
    </span>
  );
}

/**
 * A content hash, shortened for reading but complete on selection.
 *
 * SHA-256 is 64 characters and is *the* key in this system — the blob store
 * addresses bytes by it, so it is how a document is identified across a
 * restore, a re-upload or an integrity sweep. Shown in full it swamps every
 * row it sits in; shown truncated with the rest discarded it stops being
 * usable.
 *
 * So the full value is the only text node, clipped by CSS. A copy gets all 64
 * characters, a screen reader reads all 64, and the column stays twelve
 * characters wide. Rendering a short form beside an `sr-only` long form —
 * which is the obvious way to do this — makes a selection yield both, pasting
 * the hash twice with an ellipsis in the middle.
 */
export function ContentHash({
  value,
  className,
}: {
  value: string;
  className?: string;
}) {
  return (
    <Locator
      className={cn(
        "inline-block max-w-[12ch] truncate align-bottom text-ink-3",
        className,
      )}
      title={value}
    >
      {value}
    </Locator>
  );
}
