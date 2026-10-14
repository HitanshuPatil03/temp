import type { Metadata } from "next";

import { LoginForm } from "./login-form";

export const metadata: Metadata = {
  title: "Sign in · MRIP",
};

/**
 * The sign-in page.
 *
 * Deliberately says what this deployment *is* — problem statement, owner,
 * network posture — because the first question anyone asks of an unfamiliar
 * internal tool is whether they are on the right system. It does not say
 * anything about *who* has accounts on it.
 */
export default function LoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8">
          {/* An `h1`, not a styled paragraph. Every other page gets one from
              `PageHeader`; this one renders no header, so without this the
              sign-in page had no heading at all — and a screen-reader user
              navigating by heading, which is how most of them navigate, found
              nothing on the first page of the product. */}
          <h1 className="text-lg font-semibold tracking-tight text-ink">
            MRIP
          </h1>
          <p className="mt-1 text-sm leading-snug text-ink-2">
            Mining Reporting Intelligence Platform
          </p>
        </div>

        <LoginForm />

        <p className="mt-8 text-xs leading-relaxed text-ink-3">
          SIH26023 · CMPDI, Ministry of Coal. Accounts are issued by your
          administrator; there is no self-registration. This deployment runs
          entirely inside your network — no document or figure leaves it.
        </p>
      </div>
    </main>
  );
}
