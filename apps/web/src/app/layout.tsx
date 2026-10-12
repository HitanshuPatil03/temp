import type { Metadata } from "next";
import localFont from "next/font/local";

import "./globals.css";

/**
 * IBM Plex Sans and Plex Mono, served from this repository.
 *
 * **Self-hosted, not a CDN, and that is a requirement rather than a
 * preference.** ARCHITECTURE §3 forbids runtime egress: this deployment runs
 * inside a government network and must not fetch anything at page load.
 * `next/font/local` inlines a `@font-face` against a hashed asset under
 * `/_next`, so there is no third-party request and no layout shift waiting for
 * one. The five files total 108 kB and ship in the offline install bundle
 * (roadmap 8.7) with everything else.
 *
 * **Why a typeface at all.** The previous stack was
 * `system-ui, -apple-system, "Segoe UI"`, which is three different products:
 * Segoe on the officer's Windows desktop, SF Pro on a reviewer's Mac, Roboto
 * elsewhere. Every density and alignment decision in this UI was then correct
 * on exactly one of them, and the figures — the entire subject of this
 * application — were whatever the operating system happened to ship.
 *
 * **Why Plex.** It was designed for technical interfaces and reads as
 * institutional rather than fashionable, which is the right register for a
 * Ministry of Coal tool; the current crop of startup faces would date it. Two
 * properties earn it the slot on merit:
 *
 * - Its tabular figures are a true monospaced set, so a column of tonnages
 *   aligns on the digit rather than nearly aligning. In Indian grouping
 *   (`19,30,00,000`) the comma positions differ between rows, which makes
 *   wobbling digits much more visible than in thousands grouping.
 * - `1`, `l` and `I` are unmistakable from one another, and so are `0` and `O`.
 *   Evidence locators are read aloud and typed back (`p.47 p1t1 r3c2`), and a
 *   citation that cannot be transcribed is not a citation.
 *
 * Plex Mono carries those locators, fact ids and content hashes. Weights are
 * 400/500/600 for sans and 400/500 for mono — no light or black, because
 * nothing in a dense data interface should be set in either.
 */
const sans = localFont({
  src: [
    { path: "./fonts/ibm-plex-sans-latin-400-normal.woff2", weight: "400", style: "normal" },
    { path: "./fonts/ibm-plex-sans-latin-500-normal.woff2", weight: "500", style: "normal" },
    { path: "./fonts/ibm-plex-sans-latin-600-normal.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-plex-sans",
  display: "swap",
  // Measured against Segoe UI and SF Pro so the swap does not reflow a table.
  fallback: ["system-ui", "Segoe UI", "sans-serif"],
  adjustFontFallback: false,
});

const mono = localFont({
  src: [
    { path: "./fonts/ibm-plex-mono-latin-400-normal.woff2", weight: "400", style: "normal" },
    { path: "./fonts/ibm-plex-mono-latin-500-normal.woff2", weight: "500", style: "normal" },
  ],
  variable: "--font-plex-mono",
  display: "swap",
  fallback: ["ui-monospace", "Cascadia Mono", "Menlo", "monospace"],
  adjustFontFallback: false,
});

export const metadata: Metadata = {
  title: "MRIP — Mining Reporting Intelligence Platform",
  description:
    "Evidence-first reporting intelligence for the Indian coal sector. Every figure " +
    "carries the document, page, table and cell it came from (SIH26023).",
};

/**
 * The root layout holds the document shell and nothing else.
 *
 * The navigation rail lives in `(app)/layout.tsx` instead, because the sign-in
 * page must not render it: a rail full of links that all bounce back to /login
 * is worse than no rail, and it would also force that page to resolve a user
 * that by definition does not exist yet.
 */
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${sans.variable} ${mono.variable} h-full antialiased`}
    >
      <body className="min-h-full bg-plane text-ink">{children}</body>
    </html>
  );
}
