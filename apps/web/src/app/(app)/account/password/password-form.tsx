"use client";

/**
 * Change-password form.
 *
 * The policy is stated *before* the attempt rather than only in the rejection,
 * because a password field that refuses twice and explains once is how people
 * end up writing credentials on paper. The backend's reasons are still shown in
 * full when it does refuse — it returns every failing rule at once, not the
 * first.
 */

import { useActionState } from "react";
import { AlertCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";

import { changePassword, type PasswordState } from "./actions";

const INITIAL: PasswordState = { error: null };

export function PasswordForm() {
  const [state, submit, pending] = useActionState(changePassword, INITIAL);

  return (
    <form action={submit} className="max-w-sm space-y-4">
      <Field label="Current password">
        <Input
          name="current_password"
          type="password"
          autoComplete="current-password"
          required
        />
      </Field>

      <Field
        label="New password"
        hint="At least 12 characters, five of them distinct, and not your username."
      >
        <Input
          name="new_password"
          type="password"
          autoComplete="new-password"
          required
        />
      </Field>

      <Field label="Repeat new password">
        <Input
          name="confirm_password"
          type="password"
          autoComplete="new-password"
          required
        />
      </Field>

      {state.error ? (
        <div
          role="alert"
          className="space-y-1 rounded-md border border-critical/30 bg-critical/5 px-3 py-2.5"
        >
          <p className="flex gap-2 text-xs leading-relaxed text-critical">
            <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
            {state.error}
          </p>
          {state.reasons?.length ? (
            <ul className="ml-6 list-disc space-y-0.5 text-xs text-critical">
              {state.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <Button type="submit" variant="primary" pending={pending}>
        Change password
      </Button>
      <p className="text-xs leading-relaxed text-ink-3">
        Changing your password signs out every session, including this one. You
        will be asked to sign in again.
      </p>
    </form>
  );
}
