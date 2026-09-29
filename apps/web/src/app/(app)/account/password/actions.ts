"use server";

/**
 * Changing your own password.
 *
 * Separate from `login/actions.ts` because this one runs *authenticated*: it
 * reads the session cookie and calls the API as the signed-in user. The backend
 * still demands the current password — holding a session is not proof of holding
 * the credential it was issued for.
 *
 * On success the backend bumps the account's session epoch, which invalidates
 * every token including this one. So the cookie is dropped and the user is sent
 * back to sign in: that is not a rough edge, it is the feature ("changing your
 * password signs out your other sessions") applied consistently to this one.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { API_ORIGIN, SESSION_COOKIE } from "@/lib/session";

export interface PasswordState {
  error: string | null;
  /** Policy reasons, listed rather than summarised — see the backend's 422. */
  reasons?: string[];
}

export async function changePassword(
  _previous: PasswordState,
  formData: FormData,
): Promise<PasswordState> {
  const current = String(formData.get("current_password") ?? "");
  const next = String(formData.get("new_password") ?? "");
  const confirm = String(formData.get("confirm_password") ?? "");

  if (next !== confirm) {
    return { error: "The two new passwords do not match." };
  }

  const store = await cookies();
  const token = store.get(SESSION_COOKIE)?.value;
  if (!token) redirect("/login");

  const response = await fetch(`${API_ORIGIN}/api/auth/password`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ current_password: current, new_password: next }),
    cache: "no-store",
  });

  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: { message?: string; reasons?: string[] };
    } | null;
    return {
      error: body?.detail?.message ?? "The password could not be changed.",
      reasons: body?.detail?.reasons ?? [],
    };
  }

  store.delete(SESSION_COOKIE);
  redirect("/login?changed=1");
}
