import type { Metadata } from "next";

import "./globals.css";

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
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full bg-plane text-ink">{children}</body>
    </html>
  );
}
