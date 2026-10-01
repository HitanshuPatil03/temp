"use client";

/**
 * Administration — users and the audit trail.
 *
 * Everything here was previously possible only from the `mrip-admin` CLI, which
 * means an administrator had to have shell access to the host to do their job. A
 * corporation expects to manage accounts and read the audit log from the
 * application. The API already enforced the admin role on every one of these
 * routes; this is the surface over it.
 *
 * The page is admin-only three times over: the sidebar only offers it to an
 * admin, every endpoint it calls is admin-gated server-side, and a non-admin who
 * reaches the URL directly sees the access notice below rather than a broken
 * page. The middle one is the control; the other two are courtesy.
 */

import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { ShieldCheck, UserPlus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { PageHeader } from "@/components/shell/page-header";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/states";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { ApiError, api, fetcher, keys } from "@/lib/api";
import { formatDate, humanize } from "@/lib/format";
import type { AuditEntry, Role, UserAccount } from "@/lib/types";

const ROLES: Role[] = ["viewer", "officer", "reviewer", "approver", "admin"];

const ROLE_TONE: Record<Role, "neutral" | "info" | "good"> = {
  viewer: "neutral",
  officer: "neutral",
  reviewer: "info",
  approver: "info",
  admin: "good",
};

export default function AdminPage() {
  const users = useSWR<UserAccount[]>(keys.users(), fetcher);

  if (users.error instanceof ApiError && users.error.status === 403) {
    return (
      <main className="min-w-0 flex-1 px-8 py-6">
        <PageHeader title="Administration" description="Users and the audit trail." />
        <ErrorState
          title="Administrators only"
          detail="This area manages accounts and reads the audit log. Your role does not include it."
        />
      </main>
    );
  }

  return (
    <main className="min-w-0 flex-1 space-y-6 px-8 py-6">
      <PageHeader
        title="Administration"
        description="Manage accounts and read the append-only audit trail. Every action here is itself audited."
      />
      <UsersCard users={users} />
      <AuditCard />
    </main>
  );
}

function UsersCard({
  users,
}: {
  users: ReturnType<typeof useSWR<UserAccount[]>>;
}) {
  const { mutate } = useSWRConfig();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function patch(user: UserAccount, change: Parameters<typeof api.updateUser>[1]) {
    setBusy(user.user_id);
    setError(null);
    try {
      await api.updateUser(user.user_id, change);
      await mutate(keys.users());
      await mutate(keys.audit());
    } catch (cause) {
      setError(
        cause instanceof ApiError ? cause.detail : "Could not update the account.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Accounts</CardTitle>
        <CardDescription>
          Role and scope are read from the database on every request, so a change
          here takes effect on the account&apos;s next action, not at token expiry.
        </CardDescription>
      </CardHeader>
      <CardBody className="space-y-4">
        <CreateUser onCreated={() => void mutate(keys.users())} />

        {error ? (
          <p className="text-sm text-amber-700" role="status">
            {error}
          </p>
        ) : null}

        {users.error ? (
          <ErrorState
            title="Could not load accounts"
            detail={
              users.error instanceof ApiError ? users.error.detail : "Request failed."
            }
          />
        ) : users.isLoading ? (
          <Skeleton className="h-48" />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Account</TH>
                <TH>Role</TH>
                <TH>Scope</TH>
                <TH>Status</TH>
                <TH>Last sign-in</TH>
                <TH />
              </TR>
            </THead>
            <TBody>
              {(users.data ?? []).map((user) => (
                <TR key={user.user_id}>
                  <TD>
                    <span className="block font-medium text-ink">
                      {user.display_name ?? user.username}
                    </span>
                    <span className="block text-xs text-ink-3">{user.username}</span>
                  </TD>
                  <TD>
                    <Select
                      aria-label={`Role for ${user.username}`}
                      value={user.role}
                      disabled={busy === user.user_id}
                      onChange={(event) =>
                        patch(user, { role: event.target.value as Role })
                      }
                    >
                      {ROLES.map((role) => (
                        <option key={role} value={role}>
                          {humanize(role)}
                        </option>
                      ))}
                    </Select>
                  </TD>
                  <TD className="text-xs text-ink-2">
                    {user.entities.includes("*")
                      ? "All entities"
                      : user.entities.map((e) => e.toUpperCase()).join(", ") || "None"}
                  </TD>
                  <TD>
                    {user.locked_until ? (
                      <Badge tone="warning">Locked</Badge>
                    ) : user.is_active ? (
                      <Badge tone={ROLE_TONE[user.role]}>Active</Badge>
                    ) : (
                      <Badge tone="neutral">Disabled</Badge>
                    )}
                  </TD>
                  <TD className="text-xs text-ink-2">
                    {user.last_login_at ? formatDate(user.last_login_at) : "never"}
                  </TD>
                  <TD>
                    <div className="flex justify-end gap-1.5">
                      {user.locked_until ? (
                        <button
                          type="button"
                          onClick={() => patch(user, { unlock: true })}
                          disabled={busy === user.user_id}
                          className="rounded-md border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-plane hover:text-ink disabled:opacity-50"
                        >
                          Unlock
                        </button>
                      ) : null}
                      <button
                        type="button"
                        onClick={() => patch(user, { is_active: !user.is_active })}
                        disabled={busy === user.user_id}
                        className="rounded-md border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-plane hover:text-ink disabled:opacity-50"
                      >
                        {user.is_active ? "Disable" : "Enable"}
                      </button>
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardBody>
    </Card>
  );
}

function CreateUser({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [entities, setEntities] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setPending(true);
    setError(null);
    try {
      await api.createUser({
        username: username.trim().toLowerCase(),
        password,
        role,
        entities: entities
          .split(",")
          .map((e) => e.trim())
          .filter(Boolean),
        display_name: undefined,
      });
      setUsername("");
      setPassword("");
      setEntities("");
      setRole("viewer");
      setOpen(false);
      onCreated();
    } catch (cause) {
      setError(
        cause instanceof ApiError ? cause.detail : "Could not create the account.",
      );
    } finally {
      setPending(false);
    }
  }

  if (!open) {
    return (
      <Button variant="outline" onClick={() => setOpen(true)}>
        <UserPlus className="mr-1.5 size-3.5" aria-hidden />
        New account
      </Button>
    );
  }

  return (
    <div className="space-y-3 rounded-md border border-hairline bg-plane/50 p-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Username" hint="lowercased; the sign-in name">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="Temporary password" hint="the holder must change it on first use">
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Field label="Role">
          <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {humanize(r)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Entities" hint="comma-separated ids, or * for all">
          <Input
            value={entities}
            placeholder="secl, mcl"
            onChange={(e) => setEntities(e.target.value)}
          />
        </Field>
      </div>
      {error ? (
        <p className="text-xs text-amber-700" role="status">
          {error}
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button
          onClick={submit}
          pending={pending}
          disabled={!username.trim() || !password}
        >
          Create account
        </Button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded-md px-2 py-1 text-xs text-ink-2 hover:text-ink"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function AuditCard() {
  const audit = useSWR<AuditEntry[]>(keys.audit({ limit: 100 }), fetcher);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit trail</CardTitle>
        <CardDescription>
          Append-only, newest first — sign-ins and failures, account and role
          changes, conflict and fact decisions, report and topic actions. The
          table cannot be edited, by database trigger, not convention.
        </CardDescription>
      </CardHeader>
      <CardBody>
        {audit.error ? (
          <ErrorState
            title="Could not load the audit trail"
            detail={
              audit.error instanceof ApiError ? audit.error.detail : "Request failed."
            }
          />
        ) : audit.isLoading ? (
          <Skeleton className="h-48" />
        ) : (audit.data ?? []).length === 0 ? (
          <EmptyState icon={ShieldCheck} title="No audit entries yet" />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>When</TH>
                <TH>Actor</TH>
                <TH>Action</TH>
                <TH>Subject</TH>
                <TH>From</TH>
              </TR>
            </THead>
            <TBody>
              {(audit.data ?? []).map((entry, index) => (
                <TR key={`${entry.occurred_at}-${index}`}>
                  <TD className="whitespace-nowrap text-xs text-ink-2">
                    {formatDate(entry.occurred_at)}
                  </TD>
                  <TD className="text-ink">{entry.actor ?? "—"}</TD>
                  <TD>
                    <code className="text-xs text-ink-2">{entry.action}</code>
                  </TD>
                  <TD className="text-xs text-ink-3">
                    {entry.subject_type
                      ? `${entry.subject_type}${entry.subject_id ? ` ${entry.subject_id}` : ""}`
                      : "—"}
                  </TD>
                  <TD className="text-xs text-ink-3">{entry.source_ip ?? "—"}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardBody>
    </Card>
  );
}
