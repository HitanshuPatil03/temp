import { redirect } from "next/navigation";

import { Sidebar } from "@/components/shell/sidebar";
import { getCurrentUser } from "@/lib/session";

/**
 * The authenticated shell.
 *
 * The user is resolved here, once per navigation, and handed to the rail — so
 * the header says who is signed in and which subsidiaries they cover. That last
 * part is not decoration: a reviewer scoped to SECL who sees an empty document
 * list needs to know it is a *scope*, not an outage.
 *
 * `src/proxy.ts` already redirects an unauthenticated browser to /login before
 * this renders. The check is repeated here anyway, because the proxy's matcher is
 * a pattern that a future route could fall outside of, and "the edge redirect
 * covers it" is exactly the assumption that turns a refactor into a disclosure.
 */
export default async function AppLayout({ children }: LayoutProps<"/">) {
  const user = await getCurrentUser();
  if (!user) redirect("/login");

  return (
    <div className="flex min-h-full">
      <Sidebar user={user} />
      <div className="flex min-w-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
