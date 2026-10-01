import { FileQuestion } from "lucide-react";
import Link from "next/link";

/**
 * Shown for a URL inside the app that matches no route. A reviewer who follows a
 * stale bookmark or a mistyped link should land somewhere that explains itself
 * and offers the way back, not a bare 404.
 */
export default function NotFound() {
  return (
    <main className="flex min-h-full flex-1 items-center justify-center px-8 py-16">
      <div className="max-w-md space-y-4 text-center">
        <FileQuestion className="mx-auto size-6 text-ink-3" aria-hidden />
        <h1 className="text-lg font-semibold text-ink">Page not found</h1>
        <p className="text-sm leading-relaxed text-ink-2">
          There is nothing at this address. It may have been a stale link, or a
          document or report that no longer exists.
        </p>
        <Link
          href="/"
          className="inline-block rounded-md border border-hairline px-3 py-1.5 text-sm text-ink-2 transition-colors hover:bg-plane hover:text-ink"
        >
          Back to dashboard
        </Link>
      </div>
    </main>
  );
}
