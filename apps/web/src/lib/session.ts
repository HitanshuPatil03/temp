/**
 * Server-side session handling.
 *
 * **The access token is never given to the browser.** It lives in an httpOnly,
 * SameSite=Strict cookie, and `src/proxy.ts` turns that cookie into an
 * `Authorization: Bearer` header as it forwards `/api/*` to FastAPI. So a stored
 * cross-site script — the realistic threat against an internal reporting tool
 * with many authors — cannot read the token and replay it elsewhere; it can only
 * make requests the browser was already able to make.
 *
 * The cost is that every authenticated call has to go through the proxy, which
 * it already did (there is no second origin), and that this module is
 * server-only. That is enforced by `import "server-only"`: importing it from a
 * client component is a build error rather than a token leak.
 */

import "server-only";

import { cookies } from "next/headers";

/** The session cookie. Prefixed so it is obvious in a browser inspector. */
export const SESSION_COOKIE = "mrip_session";

/**
 * Where the API lives, from the server's point of view. The browser never sees
 * this: it talks to Next, which forwards. One env var, no rebuild.
 */
export const API_ORIGIN = process.env.MRIP_API_URL ?? "http://127.0.0.1:8000";

export type Role = "viewer" | "officer" | "reviewer" | "approver" | "admin";

/** What `/api/auth/me` returns. Mirrors `PrincipalResponse` in the backend. */
export interface CurrentUser {
  user_id: string;
  username: string;
  display_name: string | null;
  role: Role;
  auth_source: "local" | "oidc" | "ldap";
  /** `["*"]` for an HQ-wide grant. */
  entities: string[];
  must_change_password: boolean;
}

/** Role ranking, kept in step with `mrip.schemas.Role`. */
const RANK: Record<Role, number> = {
  viewer: 0,
  officer: 1,
  reviewer: 2,
  approver: 3,
  admin: 4,
};

/**
 * Whether a user satisfies a minimum role.
 *
 * Used to decide what the UI *offers*, never to decide what is *allowed* — every
 * route enforces its own role server-side. Hiding a button the caller cannot use
 * is courtesy; it is not the control.
 */
export function hasRole(user: CurrentUser | null, minimum: Role): boolean {
  return user !== null && RANK[user.role] >= RANK[minimum];
}

export function describeScope(user: CurrentUser): string {
  if (user.entities.includes("*")) return "All entities";
  if (user.entities.length === 0) return "No entities granted";
  return user.entities.map((id) => id.toUpperCase()).join(", ");
}

export async function readSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}

/**
 * The signed-in user, or `null`.
 *
 * Asks the backend rather than decoding the token here, deliberately: the
 * backend reads the role and the entity grants from the database on every
 * request, so a demotion or a revoked grant is reflected immediately. A frontend
 * that decoded claims locally would keep rendering the old role until the token
 * expired.
 */
export async function getCurrentUser(): Promise<CurrentUser | null> {
  const token = await readSessionToken();
  if (!token) return null;

  const response = await fetch(`${API_ORIGIN}/api/auth/me`, {
    headers: { authorization: `Bearer ${token}` },
    // Identity is never cached: the whole point of reading it per request is
    // that it can change between two of them.
    cache: "no-store",
  });

  if (!response.ok) return null;
  return (await response.json()) as CurrentUser;
}
