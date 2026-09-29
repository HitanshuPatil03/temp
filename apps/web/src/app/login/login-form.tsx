"use client";

/**
 * The sign-in form.
 *
 * `useActionState` over the `signIn` Server Function, so the password is posted
 * to the Next server and forwarded from there. It is never held in client state
 * beyond the input element, and there is no client-side fetch to the API for it.
 *
 * The error message is whatever the backend returned, unchanged. That matters
 * here: the backend refuses to distinguish "no such user" from "wrong password"
 * (a response that did would hand over a list of valid usernames), so this form
 * must not helpfully add the distinction back.
 */

import { useActionState } from "react";
import { AlertCircle, LockKeyhole } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";

import { signIn, type SignInState } from "./actions";

const INITIAL: SignInState = { error: null };

export function LoginForm() {
  const [state, submit, pending] = useActionState(signIn, INITIAL);

  return (
    <form action={submit} className="space-y-4">
      <Field label="Username">
        <Input
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          autoFocus
          required
          placeholder="s.kumar"
        />
      </Field>

      <Field label="Password">
        <Input
          name="password"
          type="password"
          autoComplete="current-password"
          required
        />
      </Field>

      {state.error ? (
        <div
          role="alert"
          className="flex gap-2 rounded-md border border-critical/30 bg-critical/5 px-3 py-2.5"
        >
          {state.lockedUntil ? (
            <LockKeyhole className="mt-0.5 size-4 shrink-0 text-critical" aria-hidden />
          ) : (
            <AlertCircle className="mt-0.5 size-4 shrink-0 text-critical" aria-hidden />
          )}
          <p className="text-xs leading-relaxed text-critical">{state.error}</p>
        </div>
      ) : null}

      <Button type="submit" variant="primary" pending={pending} className="w-full">
        Sign in
      </Button>
    </form>
  );
}
