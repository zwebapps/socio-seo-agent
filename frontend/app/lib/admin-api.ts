/** Typed client for the admin model-configuration API. */

import { ApiError } from "./api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

export type Route = {
  taskClass: string;
  tier: string;
  chain: { provider: string; model: string }[];
  source: "configured" | "default";
  note: string | null;
  /** False when any model in the chain is absent from the price table. */
  costReportable: boolean;
};

export type Provider = {
  provider: string;
  enabled: boolean;
  available: boolean;
  requiresKey: boolean;
  baseUrl: string | null;
  note: string | null;
};

export type CatalogueModel = {
  id: string;
  provider: string;
  label: string;
  priced: boolean;
  contextTokens: number | null;
  note: string | null;
};

export type Catalogue = {
  provider: string;
  models: CatalogueModel[];
  live: boolean;
  message: string | null;
};


/* --------------------------------------------------------------------------- */
/* Sampling                                                                    */
/* --------------------------------------------------------------------------- */

/**
 * The slider limits, and the reason for each, sent by the server.
 *
 * Deliberately NOT duplicated as constants here. A frontend with its own copy of a
 * min/max is a second source of truth for a limit the server enforces, and on the day
 * they disagree the UI happily produces a value the API refuses. The `reason` strings
 * come along too, so the screen never has to invent an explanation for a limit it did
 * not choose.
 */
export type SamplingBounds = {
  temperatureMin: number;
  temperatureMax: number;
  temperatureStep: number;
  temperatureReason: string;
  maxTokensMin: number;
  maxTokensMax: number;
  maxTokensStep: number;
  maxTokensReason: string;
};

export type Sampling = {
  taskClass: string;
  tier: string;
  /** null means "send nothing, take the provider default". */
  temperature: number | null;
  maxOutputTokens: number | null;
  source: "configured" | "default";
  note: string | null;
  /** Models in this task's chain that reject `temperature` outright. */
  modelsRejectingTemperature: string[];
  /** True when EVERY model in the chain rejects it, so the control is inert. */
  temperatureInert: boolean;
  /** USD the pre-call budget guard reserves at this ceiling. A STRING: money is Decimal. */
  reservedUsdPerCall: string | null;
  callsWithinRunCap: number | null;
};

export type SamplingList = {
  bounds: SamplingBounds;
  runCapUsd: string;
  sampling: Sampling[];
};

/* --------------------------------------------------------------------------- */
/* Tool toggles                                                                */
/* --------------------------------------------------------------------------- */

export type NodeTools = {
  node: string;
  /** The code allowlist. The ceiling, and read-only from this screen. */
  granted: string[];
  revoked: string[];
  effective: string[];
  /** Stored revocations naming a tool the node does not hold. */
  ignored: string[];
  actuators: string[];
  enforced: boolean;
};

export type ToolPolicy = {
  nodes: NodeTools[];
  actuatorTools: string[];
  enforced: boolean;
  policy: string;
};

/* --------------------------------------------------------------------------- */
/* Prompt versions                                                             */
/* --------------------------------------------------------------------------- */

export type PromptSurface = {
  key: string;
  label: string;
  module: string;
  attribute: string;
  version: string | null;
  variants: number;
  switchable: boolean;
  howToChange: string;
  error: string | null;
};

export type PromptVersions = {
  surfaces: PromptSurface[];
  /** False today. The screen renders an inventory rather than a dropdown because of it. */
  selectable: boolean;
  evalHarnessNote: string;
  summary: string;
};

/* --------------------------------------------------------------------------- */
/* Cost                                                                        */
/* --------------------------------------------------------------------------- */

/** Every `usd` here is a STRING. Money is `Decimal` server-side and never a JS number. */
export type SpendRow = {
  key: string;
  calls: number;
  tokensIn: number;
  tokensOut: number;
  usd: string;
  priced: boolean | null;
};

export type DailySpend = { day: string; calls: number; usd: string };

export type RunSpend = { runId: string; usd: string; capUsd: string; atCap: boolean };

export type CostReport = {
  windowDays: number;
  since: string;
  calls: number;
  tokensIn: number;
  tokensOut: number;
  totalUsd: string;
  byModel: SpendRow[];
  byNode: SpendRow[];
  byPromptVersion: SpendRow[];
  byDay: DailySpend[];
  defaultRunCapUsd: string;
  runsInWindow: number;
  runsAtCap: number;
  topRuns: RunSpend[];
  /** False when runs exist but the ledger has no rows — "unrecorded", not "$0.00". */
  ledgerWired: boolean;
  message: string;
};

export type Cost = { businessId: string; report: CostReport };

/**
 * One admin request.
 *
 * `credentials: "include"` sends the session cookie cross-origin (Next on :3100, the
 * API on :8100), which is what makes every call here a state-changing request the API
 * checks for CSRF. Nothing has to be added for that: the browser sets `Origin` itself,
 * and page script cannot — it is a forbidden header name — which is exactly why the API
 * validates it instead of asking us to echo a token back. See `backend/app/core/csrf.py`.
 *
 * Two consequences worth knowing before changing this file. The API's allowlist is
 * `CORS_ORIGINS`, so a new frontend origin needs a server-side change and not just a
 * different `NEXT_PUBLIC_API_URL`. And these calls must stay in the browser: moved into
 * a server component or a route handler, `fetch` sends no `Origin`, and a cookie-bearing
 * write with no `Origin` is refused with 403 by design.
 */
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    throw new ApiError("network", `Cannot reach the API at ${API_URL}.`, 0);
  }

  if (response.status === 204) return undefined as T;

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === "object" && "code" in detail) {
      const d = detail as { code: string; message: string };
      throw new ApiError(d.code, d.message, response.status);
    }
    if (response.status === 401) {
      throw new ApiError("not_authenticated", "Sign in to change model settings.", 401);
    }
    if (response.status === 403) {
      // Distinct from 401 on purpose: this person IS signed in, so sending them to the
      // login page is a loop they cannot escape.
      throw new ApiError(
        "forbidden",
        "Your account does not have access to these settings.",
        403,
      );
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string; loc?: unknown[] };
      throw new ApiError(
        "invalid",
        `${(first.loc ?? []).join(".")}: ${first.msg ?? "invalid value"}`,
        response.status,
      );
    }
    throw new ApiError("unknown", `Request failed (${response.status}).`, response.status);
  }
  return body as T;
}

export const adminApi = {
  routes: () => call<{ routes: Route[] }>("/api/v1/admin/models/routes"),
  providers: () => call<{ providers: Provider[] }>("/api/v1/admin/models/providers"),
  catalogue: (provider: string) =>
    call<Catalogue>(`/api/v1/admin/models/available?provider=${encodeURIComponent(provider)}`),
  saveRoute: (taskClass: string, body: { tier: string; chain: { provider: string; model: string }[] }) =>
    call<{ status: string }>(`/api/v1/admin/models/routes/${taskClass}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  revertRoute: (taskClass: string) =>
    call<void>(`/api/v1/admin/models/routes/${taskClass}`, { method: "DELETE" }),
  saveProvider: (provider: string, body: { enabled: boolean; baseUrl?: string | null }) =>
    call<{ status: string }>(`/api/v1/admin/models/providers/${provider}`, {
      method: "PUT",
      body: JSON.stringify({ enabled: body.enabled, base_url: body.baseUrl ?? null }),
    }),

  sampling: () => call<SamplingList>("/api/v1/admin/models/sampling"),
  saveSampling: (
    taskClass: string,
    body: { temperature: number | null; maxOutputTokens: number | null },
  ) =>
    call<{ status: string }>(`/api/v1/admin/models/sampling/${taskClass}`, {
      method: "PUT",
      // Sent under the camelCase aliases the API declares. `extra="forbid"` on the
      // server means a misspelling is a 422 rather than a 200 that changed nothing,
      // which is why these key names are worth checking against the endpoint.
      body: JSON.stringify({
        temperature: body.temperature,
        maxOutputTokens: body.maxOutputTokens,
      }),
    }),
  revertSampling: (taskClass: string) =>
    call<void>(`/api/v1/admin/models/sampling/${taskClass}`, { method: "DELETE" }),

  tools: () => call<ToolPolicy>("/api/v1/admin/models/tools"),
  /**
   * Set which of a node's tools are switched OFF.
   *
   * There is no `grantTools` counterpart and there must never be one: the per-node
   * allowlist in the backend is a prompt-injection barrier, and the effective set is
   * that allowlist MINUS this list. The server refuses a body carrying `granted`
   * outright, so adding one here would produce a 422, not a widened allowlist.
   */
  revokeTools: (node: string, revoked: string[]) =>
    call<{ status: string; node: string; effective: string[] }>(
      `/api/v1/admin/models/tools/${node}`,
      { method: "PUT", body: JSON.stringify({ revoked }) },
    ),

  promptVersions: () => call<PromptVersions>("/api/v1/admin/models/prompt-versions"),

  cost: (windowDays: number) =>
    call<Cost>(`/api/v1/admin/cost?windowDays=${encodeURIComponent(windowDays)}`),
};
