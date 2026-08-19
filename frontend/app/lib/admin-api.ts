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
};
