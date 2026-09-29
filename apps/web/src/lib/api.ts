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
  Comparability,
  ConflictGroup,
  DashboardSummary,
  EvidenceSpan,
  Fact,
  Health,
  MTConvention,
  MripDocument,
  NormalizedPeriod,
  NormalizedQuantity,
  ResolvedEntity,
  SeriesPoint,
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
};
