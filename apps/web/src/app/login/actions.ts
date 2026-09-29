"use server";

/**
 * Sign-in and sign-out, as Server Functions.
 *
 * Two reasons these are actions rather than a Next route handler: `/api/*` is
 * proxied to FastAPI, so a route handler cannot live there without shadowing the
 * backend; and an action is the only place a cookie can be set during a form
 * submission without a second round trip.
 *
 * The password reaches the Next server and is forwarded to FastAPI over the
 * loopback (or internal) network, then discarded. It is never written to a log
 * line, a cookie or a client-visible response.
 */

import { redirect } from "next/navigation";
import { cookies } from "next/headers";

import { API_ORIGIN, SESSION_COOKIE } from "@/lib/session";

export interface SignInState {
  error: string | null;
  /** Present when the account is locked, so the form can say for how long. */
  lockedUntil?: string | null;
}

interface LoginSuccess {
  access_token: string;
  expires_at: string;
  user: { must_change_password: boolean };
}

/**
 * Exchange a username and password for a session cookie.
 *
 * Shaped for `useActionState`: returns a state object on failure and redirects
 * on success. The error message is whatever the backend said, which for a failed
 * login is deliberately the same sentence in every case — the backend refuses to
 * distinguish "no such user" from "wrong password", so neither does this.
 */
export async function signIn(
  _previous: SignInState,
  formData: FormData,
): Promise<SignInState> {
  const username = String(formData.get("username") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!username || !password) {
    return { error: "Enter both a username and a password." };
  }

  let response: Response;
  try {
    response = await fetch(`${API_ORIGIN}/api/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
      cache: "no-store",
    });
  } catch {
    // A connection failure is a different problem from a rejected credential,
    // and telling someone "incorrect password" when the backend is down wastes
    // an afternoon.
    return {
      error:
        "Cannot reach the MRIP API. Check that the backend is running, then try again.",
    };
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: { message?: string; locked_until?: string };
    } | null;
    return {
      error: body?.detail?.message ?? "Sign-in failed.",
      lockedUntil: body?.detail?.locked_until ?? null,
    };
  }

  const session = (await response.json()) as LoginSuccess;
  const expiresAt = new Date(session.expires_at);

  const store = await cookies();
  store.set(SESSION_COOKIE, session.access_token, {
    httpOnly: true,
    sameSite: "strict",
    // On in production, off on plain-HTTP localhost — a Secure cookie over
    // http:// is silently dropped, which looks exactly like a broken login.
    secure: process.env.NODE_ENV === "production",
    path: "/",
    expires: expiresAt,
  });

  // A temporary password buys exactly one thing, so send them straight to it
  // rather than to a dashboard that will refuse every request with a 403.
  redirect(session.user.must_change_password ? "/account/password" : "/");
}

/**
 * End the session: tell the backend (for the audit trail) and drop the cookie.
 *
 * The backend cannot revoke a stateless token, and says so; dropping the cookie
 * is what actually ends this browser's session. "Sign out everywhere" is a
 * password change, which bumps the account's session epoch.
 */
export async function signOut(): Promise<void> {
  const store = await cookies();
  const token = store.get(SESSION_COOKIE)?.value;

  if (token) {
    await fetch(`${API_ORIGIN}/api/auth/logout`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    }).catch(() => {
      // The audit row is best-effort; the cookie must go regardless. A sign-out
      // that failed because the backend was restarting must not leave someone
      // signed in.
    });
  }

  store.delete(SESSION_COOKIE);
  redirect("/login");
}
