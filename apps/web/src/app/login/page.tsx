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
          <p className="text-lg font-semibold tracking-tight text-ink">MRIP</p>
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
