/**
 * The HTTP client.
 *
 * Every call goes through `/api`, which Next rewrites to the FastAPI process, so
 * nothing here needs to know the backend's origin.
 *
 * `ApiError` carries the backend's own `detail` string. That matters more here
 * than in most apps: the normalizer answers "I will not guess what 'wibbles' is"
 * with a 422 and a reason, and that reason is the useful part of the response.
 * Swallowing it into "Request failed" would throw away the product's argument.
 */

import type {
  AuditEntry,
  CloudTerm,
  Comparability,
  ConflictGroup,
  DashboardSummary,
  EvidenceSpan,
  Fact,
  FigureDelta,
  Health,
  IngestStage,
  MTConvention,
  MripDocument,
  NormalizedPeriod,
  NormalizedQuantity,
  ReportManifest,
  ReportState,
  ResolvedEntity,
  Role,
  SeriesPoint,
  TemplateSummary,
  TermDocument,
  TermPrevalence,
  UserAccount,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }

  /** A refusal the backend made on purpose, as opposed to something breaking. */
  get isRefusal(): boolean {
    return this.status === 404 || this.status === 422;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function withQuery(path: string, query?: Query): string {
  if (!query) return `/api${path}`;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== "") {
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `/api${path}?${qs}` : `/api${path}`;
}

/** FastAPI's `detail` is a string for our `HTTPException`s and an array for
 *  request-validation failures. Flatten both to one readable line. */
function readDetail(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          typeof item === "object" && item !== null && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join("; ");
    }
  }
  return `Request failed (${status})`;
}

async function request<T>(
  path: string,
  init?: RequestInit & { query?: Query },
): Promise<T> {
  const { query, ...rest } = init ?? {};
  const response = await fetch(withQuery(path, query), {
    ...rest,
    headers: { "content-type": "application/json", ...rest.headers },
  });

  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      /* a non-JSON error body is still an error */
    }
    throw new ApiError(response.status, readDetail(body, response.status));
  }
  return (await response.json()) as T;
}

/** SWR's fetcher. Keys are the `/api`-relative path plus its query string. */
export const fetcher = <T>(key: string): Promise<T> =>
  request<T>(key.startsWith("/") ? key : `/${key}`);

// ------------------------------------------------------------------- reads
// Exported as key builders rather than functions so SWR can dedupe on the key
// and so a mutation can revalidate exactly the queries it invalidated.

export const keys = {
  health: () => "/health",
  summary: () => "/dashboard/summary",
  documents: () => "/documents",
  document: (id: string) => `/documents/${id}`,
  pageEvidence: (id: string, page: number) =>
    `/documents/${id}/pages/${page}/evidence`,
  facts: (query?: Query) => withQuery("/facts", query).replace(/^\/api/, ""),
  fact: (id: string) => `/facts/${id}`,
  series: (metric: string, unit?: string) =>
    withQuery(`/series/${metric}`, { unit }).replace(/^\/api/, ""),
  conflicts: () => "/conflicts",
  quantity: (value: number, unit: string, mt?: MTConvention) =>
    withQuery("/normalize/quantity", {
      value,
      unit,
      mt_convention: mt,
    }).replace(/^\/api/, ""),
  period: (raw: string) =>
    withQuery("/normalize/period", { raw }).replace(/^\/api/, ""),
  comparable: (left: string, right: string) =>
    withQuery("/normalize/period/comparable", { left, right }).replace(
      /^\/api/,
      "",
    ),
  entity: (q: string) =>
    withQuery("/normalize/entity", { q }).replace(/^\/api/, ""),
  reports: (query?: Query) => withQuery("/reports", query).replace(/^\/api/, ""),
  reportTemplates: () => "/reports/templates",
  report: (id: string) => `/reports/${id}`,
  reportDiff: (id: string) => `/reports/${id}/diff`,
  cloud: (query?: Query) =>
    withQuery("/topics/cloud", query).replace(/^\/api/, ""),
  termDocuments: (term: string, query?: Query) =>
    withQuery(`/topics/${encodeURIComponent(term)}/documents`, query).replace(
      /^\/api/,
      "",
    ),
  termPrevalence: (term: string) =>
    `/topics/${encodeURIComponent(term)}/prevalence`,
  querySuggestions: () => "/query/suggestions",
  users: () => "/auth/users",
  audit: (query?: Query) => withQuery("/auth/audit", query).replace(/^\/api/, ""),
};

export const api = {
  health: () => request<Health>("/health"),
  summary: () => request<DashboardSummary>("/dashboard/summary"),
  documents: () => request<MripDocument[]>("/documents"),
  pageEvidence: (id: string, page: number) =>
    request<EvidenceSpan[]>(`/documents/${id}/pages/${page}/evidence`),
  facts: (query?: Query) => request<Fact[]>("/facts", { query }),
  series: (metric: string, unit?: string) =>
    request<SeriesPoint[]>(`/series/${metric}`, { query: { unit } }),
  conflicts: () => request<ConflictGroup[]>("/conflicts"),

  // ------------------------------------------------------------- writes
  detectConflicts: (materialSpread?: number) =>
    request<ConflictGroup[]>("/conflicts/detect", {
      method: "POST",
      query: { material_spread: materialSpread },
    }),

  /**
   * Re-run a document's ingestion from a named stage.
   *
   * Safe to call repeatedly: every stage replaces its own output rather than
   * appending to it, so a re-run produces the same rows, not a second copy. The
   * document is moved back to the state *before* the named stage, and the normal
   * chain carries it forward from there.
   */
  retryDocument: (documentId: string, stage: IngestStage) =>
    request<{
      document_id: string;
      stage: string;
      state: string;
      job_id: string;
    }>(`/documents/${documentId}/retry`, {
      method: "POST",
      body: JSON.stringify({ stage }),
    }),

  /**
   * Record a reviewer's adjudication. There is deliberately no counterpart that
   * merges the disagreeing values — the winner is always named by a person.
   */
  resolveConflict: (conflictId: string, winningFactId: string, note?: string) =>
    request<{ conflict_id: string; resolved_fact_id: string }>(
      `/conflicts/${conflictId}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({ winning_fact_id: winningFactId, note: note || null }),
      },
    ),

  /**
   * Adjudicate a fact in the review queue. The counterpart to resolveConflict,
   * for a low-confidence fact that is not in a conflict: validate accepts it,
   * reject marks it wrong, correct replaces the canonical value (keeping the raw
   * receipt). Reviewer role; recorded in the audit log.
   */
  reviewFact: (
    factId: string,
    decision: "validate" | "correct" | "reject",
    opts?: { note?: string; correctedValue?: number },
  ) =>
    request<Fact>(`/facts/${factId}/review`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        note: opts?.note || null,
        corrected_value: opts?.correctedValue ?? null,
      }),
    }),

  // --------------------------------------------------------- normalizers
  quantity: (value: number, unit: string, mtConvention?: MTConvention) =>
    request<NormalizedQuantity>("/normalize/quantity", {
      query: { value, unit, mt_convention: mtConvention },
    }),
  period: (raw: string) =>
    request<NormalizedPeriod>("/normalize/period", { query: { raw } }),
  comparable: (left: string, right: string) =>
    request<Comparability>("/normalize/period/comparable", {
      query: { left, right },
    }),
  entity: (q: string) =>
    request<ResolvedEntity>("/normalize/entity", { query: { q } }),

  // ------------------------------------------------------------- reports
  reports: (query?: Query) => request<ReportManifest[]>("/reports", { query }),
  reportTemplates: () => request<TemplateSummary[]>("/reports/templates"),
  report: (id: string) => request<ReportManifest>(`/reports/${id}`),
  reportDiff: (id: string) => request<FigureDelta[]>(`/reports/${id}/diff`),

  /** Generate and store a report. 201 even when incomplete: a manifest naming
   *  what it could not pin is a more useful answer than an error. */
  generateReport: (entity: string, period: string, templateId?: string) =>
    request<ReportManifest>("/reports", {
      method: "POST",
      body: JSON.stringify({
        entity,
        period,
        template_id: templateId ?? "production-summary",
      }),
    }),

  /** Move a report through draft → in_review → approved → published. */
  transitionReport: (id: string, state: ReportState, note?: string) =>
    request<ReportManifest>(`/reports/${id}/transition`, {
      method: "POST",
      body: JSON.stringify({ state, note: note || null }),
    }),

  /** The download is a file, not JSON — handled outside `request`. */
  reportDownloadUrl: (id: string, fmt: "md" | "docx" | "xlsx" | "pptx") =>
    `/api/reports/${id}/download?fmt=${fmt}`,

  // -------------------------------------------------------------- topics
  cloud: (query?: Query) => request<CloudTerm[]>("/topics/cloud", { query }),
  termDocuments: (term: string, query?: Query) =>
    request<TermDocument[]>(`/topics/${encodeURIComponent(term)}/documents`, {
      query,
    }),
  termPrevalence: (term: string) =>
    request<TermPrevalence[]>(`/topics/${encodeURIComponent(term)}/prevalence`),

  extractTopics: () =>
    request<{ status: string; job_id: string; terms_in_scope: number }>(
      "/topics/extract",
      { method: "POST" },
    ),

  // --------------------------------------------------------------- admin
  users: () => request<UserAccount[]>("/auth/users"),

  createUser: (input: {
    username: string;
    password: string;
    role: Role;
    entities: string[];
    display_name?: string;
  }) =>
    request<UserAccount>("/auth/users", {
      method: "POST",
      body: JSON.stringify({
        username: input.username,
        password: input.password,
        role: input.role,
        entities: input.entities,
        display_name: input.display_name || null,
      }),
    }),

  /** Change a role, scope, active status, unlock, or reset a password. */
  updateUser: (
    userId: string,
    patch: {
      role?: Role;
      is_active?: boolean;
      entities?: string[];
      unlock?: boolean;
      display_name?: string;
      reset_password?: string;
    },
  ) =>
    request<UserAccount>(`/auth/users/${userId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  auditLog: (query?: Query) => request<AuditEntry[]>("/auth/audit", { query }),
};

// ------------------------------------------------------------------- query (ARCHITECTURE 13)
//
// POST /api/query is the answer for every intent, non-streaming.
// POST /api/query/stream is narrative/draft with SSE: meta -> token* -> done.
// api.ask is the unified entry the Ask page uses: streaming prose, json fallback.

import type { Passage, QueryResponse } from "./types";

export type StreamEvent =
  | { event: "meta"; data: { intent: string; facts: Fact[]; passages: Passage[] } }
  | { event: "token"; data: { t: string } }
  | { event: "done"; data: { prose: string; flagged: boolean } }
  | { event: "error"; data: { reason: string; message: string } };

export async function query(question: string): Promise<QueryResponse> {
  return request<QueryResponse>("/query", {
    method: "POST",
    body: JSON.stringify({ question }),
  });
}

export async function* queryStream(question: string): AsyncGenerator<StreamEvent> {
  const res = await fetch("/api/query/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question }),
  });

  const ctype = res.headers.get("content-type") ?? "";

  // Non-prose and refusal branches return JSON from /query/stream — surface as
  // terminal events so the Ask page has one consumer, not two.
  if (ctype.includes("application/json")) {
    const body: unknown = await res.json();
    if (!res.ok) throw new ApiError(res.status, readDetail(body, res.status));
    yield { event: "meta", data: body as unknown as StreamEvent["data"] } as StreamEvent;
    yield { event: "done", data: { prose: "", flagged: false } } as StreamEvent;
    return;
  }

  if (!res.ok || !res.body) {
    let detail = `Request failed (${res.status})`;
    try {
      const b: unknown = await res.json();
      detail = readDetail(b, res.status);
    } catch {
      /* keep default */
    }
    throw new ApiError(res.status, detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    // SSE frames are "event: <name>\ndata: <json>\n\n"
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const eventLine = frame.split("\n").find((l) => l.startsWith("event:"));
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!eventLine || !dataLine) continue;
      const event = eventLine.slice(6).trim() as StreamEvent["event"];
      try {
        const data = JSON.parse(dataLine.slice(5).trim()) as StreamEvent["data"];
        yield { event, data } as StreamEvent;
      } catch {
        /* malformed frame — skip */
      }
    }
  }
}

export type AskResult =
  | { kind: "stream"; events: AsyncGenerator<StreamEvent> }
  | { kind: "json"; data: QueryResponse };

export async function ask(
  question: string,
  opts?: { stream?: boolean },
): Promise<AskResult> {
  const useStream = opts?.stream ?? true;
  if (!useStream) return { kind: "json", data: await query(question) };
  return { kind: "stream", events: queryStream(question) };
}
