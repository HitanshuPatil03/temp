"use client";

/**
 * Route-level error boundary for the authenticated app.
 *
 * Without this, a throw anywhere in a page — an unexpected API shape, a null the
 * code did not guard — shows the user the framework's raw error screen, which in
 * a government reporting tool reads as "the system is broken" and may leak a
 * stack frame. This catches it, says plainly what happened, and offers the two
 * actions that actually help: try again (re-render), or go back to the dashboard.
 *
 * It deliberately shows no technical detail. The message is logged to the
 * console for an operator with the dev tools open; the user sees a sentence.
 */

import { useEffect } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Logged, not shown. The digest ties a user's report to a server log line.
    console.error("Unhandled error in the app shell:", error);
  }, [error]);

  return (
    <main className="flex min-h-full flex-1 items-center justify-center px-8 py-16">
      <div className="max-w-md space-y-4 text-center">
        <h1 className="text-lg font-semibold text-ink">Something went wrong</h1>
        <p className="text-sm leading-relaxed text-ink-2">
          This screen hit an error it did not expect, so it stopped rather than
          show you something that might be wrong. Nothing you were viewing was
          changed. Try again, and if it keeps happening, note what you were doing
          and tell your administrator{error.digest ? ` (ref ${error.digest})` : ""}.
        </p>
        <div className="flex items-center justify-center gap-2">
          <Button onClick={reset}>Try again</Button>
          <Link
            href="/"
            className="rounded-md border border-hairline px-3 py-1.5 text-sm text-ink-2 transition-colors hover:bg-plane hover:text-ink"
          >
            Back to dashboard
          </Link>
        </div>
      </div>
    </main>
  );
}
