"use client";

/**
 * The navigation rail.
 *
 * Ordered by the pipeline rather than by importance: documents come in, facts
 * come out, conflicts are what is left to decide. A reviewer reading top to
 * bottom is reading the data's path through the system.
 */

import {
  FileText,
  GitCompareArrows,
  Hash,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  Ruler,
  ShieldCheck,
  Table2,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { signOut } from "@/app/login/actions";
import type { CurrentUser } from "@/lib/session";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard, hint: "Headline counts" },
  { href: "/ask", label: "Ask", icon: MessageSquare, hint: "Evidence-first answers" },
  { href: "/documents", label: "Documents", icon: FileText, hint: "Ingested sources" },
  { href: "/facts", label: "Facts", icon: Table2, hint: "Extracted figures" },
  {
    href: "/conflicts",
    label: "Conflict radar",
    icon: GitCompareArrows,
    hint: "Awaiting adjudication",
  },
  {
    href: "/normalize",
    label: "Normalizer",
    icon: Ruler,
    hint: "Units, periods, entities",
  },
  {
    href: "/reports",
    label: "Reports",
    icon: FileText,
    hint: "Pinned-evidence output",
  },
  { href: "/topics", label: "Topics", icon: Hash, hint: "Word cloud, indexed" },
] as const;

//: Shown only to administrators. Hiding it is courtesy — every /admin route and
//: the API behind it enforce the admin role server-side regardless.
const ADMIN_NAV = {
  href: "/admin",
  label: "Administration",
  icon: ShieldCheck,
  hint: "Users and the audit trail",
} as const;

function describeScope(user: CurrentUser): string {
  if (user.entities.includes("*")) return "All entities";
  if (user.entities.length === 0) return "No entities granted";
  return user.entities.map((id) => id.toUpperCase()).join(", ");
}

export function Sidebar({ user }: { user: CurrentUser }) {
  const pathname = usePathname();
  const nav = user.role === "admin" ? [...NAV, ADMIN_NAV] : NAV;

  return (
    <nav
      aria-label="Main"
      className="flex w-60 shrink-0 flex-col gap-1 border-r border-hairline bg-surface px-3 py-4"
    >
      <div className="px-2 pb-4">
        <p className="text-sm font-semibold tracking-tight text-ink">MRIP</p>
        <p className="mt-0.5 text-xs leading-snug text-ink-3">
          Mining Reporting
          <br />
          Intelligence Platform
        </p>
      </div>

      {nav.map(({ href, label, icon: Icon, hint }) => {
        const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "group flex items-start gap-2.5 rounded-md px-2 py-2 transition-colors",
              active
                ? "bg-series-1/10 text-blue-550"
                : "text-ink-2 hover:bg-plane hover:text-ink",
            )}
          >
            <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{label}</span>
              <span
                className={cn(
                  "block text-xs leading-snug",
                  active ? "text-blue-550/70" : "text-ink-3",
                )}
              >
                {hint}
              </span>
            </span>
          </Link>
        );
      })}

      {/* Who is signed in, and what they can see. The scope line answers the
          question an empty list otherwise raises: outage, or access? */}
      <div className="mt-auto border-t border-hairline pt-3">
        <div className="flex items-start gap-2 px-2">
          <ShieldCheck className="mt-0.5 size-4 shrink-0 text-ink-3" aria-hidden />
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-ink">
              {user.display_name ?? user.username}
            </p>
            <p className="text-xs capitalize text-ink-2">{user.role}</p>
            <p className="mt-0.5 text-xs leading-snug text-ink-3">
              {describeScope(user)}
            </p>
          </div>
        </div>

        <form action={signOut} className="mt-2 px-2">
          <button
            type="submit"
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-ink-2 transition-colors hover:bg-plane hover:text-ink"
          >
            <LogOut className="size-3.5" aria-hidden />
            Sign out
          </button>
        </form>

        <p className="px-2 pt-3 text-xs leading-relaxed text-ink-3">
          SIH26023 · CMPDI
          <br />
          Ministry of Coal
        </p>
      </div>
    </nav>
  );
}
