"use client";

/**
 * The last-resort boundary: an error in the root layout itself, above the app
 * shell's own error.tsx. It has to render its own <html>/<body> because at this
 * level the layout did not. Kept deliberately plain — no app chrome, since the
 * thing that renders the chrome is what failed.
 */

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Fatal error in the root layout:", error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          fontFamily: "system-ui, sans-serif",
          display: "flex",
          minHeight: "100vh",
          alignItems: "center",
          justifyContent: "center",
          margin: 0,
          padding: "2rem",
          color: "#1a1a1a",
        }}
      >
        <div style={{ maxWidth: "28rem", textAlign: "center" }}>
          <h1 style={{ fontSize: "1.125rem", fontWeight: 600 }}>
            The application could not start
          </h1>
          <p style={{ fontSize: "0.875rem", lineHeight: 1.6, color: "#555" }}>
            Something failed before the page could render. Reload to try again; if
            it persists, your administrator should check the server logs
            {error.digest ? ` (ref ${error.digest})` : ""}.
          </p>
          <button
            onClick={reset}
            style={{
              marginTop: "1rem",
              padding: "0.5rem 1rem",
              fontSize: "0.875rem",
              cursor: "pointer",
              borderRadius: "0.375rem",
              border: "1px solid #ccc",
              background: "#fff",
            }}
          >
            Reload
          </button>
        </div>
      </body>
    </html>
  );
}
