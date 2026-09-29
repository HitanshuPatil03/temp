import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * The proxy: one place decides where `/api/*` goes and what credential it
 * carries.
 *
 * This is the half of the token design that makes the other half safe. The
 * access token lives in an **httpOnly** cookie, so no script in the browser can
 * read it; here — on the server, before the request is forwarded — that cookie
 * is turned into the `Authorization: Bearer` header FastAPI expects. The browser
 * therefore never holds a credential it can leak, and the API never accepts a
 * cookie it would have to defend against CSRF.
 *
 * It also does the unauthenticated redirect for *pages*, which is a courtesy
 * rather than a control: the API enforces its own authentication on every route,
 * and `(app)/layout.tsx` re-checks before rendering. A redirect here just means
 * a signed-out user lands on the sign-in page instead of watching five panels
 * each report a 401.
 *
 * Note the asymmetry: an unauthenticated **page** request is redirected, an
 * unauthenticated **API** request is forwarded as-is so the backend can answer
 * 401 with its own reason (expired, revoked, disabled). Rewriting those into a
 * redirect would turn a client's fetch into an HTML page, which is how a "why is
 * my table showing the login form" bug report starts.
 */

const SESSION_COOKIE = "mrip_session";

/** Where FastAPI listens, from the server's side. The browser never sees it. */
const API_ORIGIN = process.env.MRIP_API_URL ?? "http://127.0.0.1:8000";

/** Pages reachable without a session. */
const PUBLIC_PATHS = new Set(["/login"]);

export function proxy(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  const token = request.cookies.get(SESSION_COOKIE)?.value;

  // ------------------------------------------------------------------ API
  if (pathname.startsWith("/api/")) {
    const target = new URL(`${pathname}${search}`, API_ORIGIN);
    const headers = new Headers(request.headers);

    if (token) {
      headers.set("authorization", `Bearer ${token}`);
    } else {
      // Strip anything the client supplied. The cookie is the only credential
      // this deployment recognises, and a browser-supplied Authorization header
      // reaching the backend would bypass the httpOnly protection entirely.
      headers.delete("authorization");
    }
    // The cookie itself is not the backend's business, and forwarding a
    // credential to a service that does not use it is how it ends up in a log.
    headers.delete("cookie");

    return NextResponse.rewrite(target, { request: { headers } });
  }

  // ---------------------------------------------------------------- pages
  //
  // Only a *document navigation* may be redirected. A Server Function call is a
  // POST to the page's own URL carrying a `Next-Action` header, and its caller
  // expects a Server Function response — redirect it and React reports "an
  // unexpected response was received from the server", which is what the sign-in
  // form did until this check existed: submitting it set the session cookie,
  // and the very next action POST to /login was bounced to / by the rule below.
  const isNavigation =
    request.method === "GET" && !request.headers.get("next-action");

  if (!isNavigation) return NextResponse.next();

  if (PUBLIC_PATHS.has(pathname)) {
    // Already signed in: skip the form rather than presenting a login page that
    // would replace a perfectly good session.
    return token
      ? NextResponse.redirect(new URL("/", request.url))
      : NextResponse.next();
  }

  if (!token) {
    const login = new URL("/login", request.url);
    // Remember where they were headed, so a bookmarked conflict page survives a
    // session that expired overnight.
    if (pathname !== "/") login.searchParams.set("next", pathname);
    return NextResponse.redirect(login);
  }

  return NextResponse.next();
}

export const config = {
  /**
   * Everything except Next's own static output and the favicon. `/api` is
   * *included* on purpose — that is where the token is attached — which is the
   * opposite of the usual starter matcher, and the reason this comment exists.
   */
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
