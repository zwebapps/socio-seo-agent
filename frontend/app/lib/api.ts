/** Typed client for the API. One place that knows the wire shape. */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

export type BusinessDna = {
  name: string;
  industry: string | null;
  city: string | null;
  country: string | null;
  locale: string;
  services: string[];
  audience: string[];
  usps: string[];
  tone: "professional" | "friendly" | "concise";
  bannedClaims: string[];
};

export type Usage = {
  tokensIn: number;
  tokensOut: number;
  usd: number;
  latencyMs: number;
  model: string;
};

export type PreviewResponse = {
  dna: BusinessDna;
  sourceUrl: string;
  usage: Usage;
  needsConfirmation: boolean;
  instructionLikeContent: boolean;
  factGaps: string[];
};

/** An error the API described on purpose, as opposed to a transport failure. */
export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * One authenticated JSON request, with the API's error shapes already unpacked.
 *
 * The API answers a failure it anticipated with `{detail: {code, message}}`, and a
 * malformed body with FastAPI's own validation array. Both are mapped to `ApiError` here
 * so no caller has to render "[object Object]" — which is what happens the first time
 * someone forgets one of the two shapes.
 *
 * `credentials: "include"` because every route this reaches is session-authenticated and
 * the cookie is host-only; `cache: "no-store"` because none of it is cacheable and a
 * stale draft or a stale memory panel is worse than a round trip.
 */
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    throw new ApiError("network", `Cannot reach the API at ${API_URL}. Is it running?`, 0);
  }

  if (response.status === 204) return undefined as T;

  const body: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;

    if (detail && typeof detail === "object" && "code" in detail) {
      const d = detail as { code: string; message: string };
      throw new ApiError(d.code, d.message, response.status);
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      throw new ApiError("invalid", first.msg ?? "That value is not valid.", response.status);
    }
    if (response.status === 401) {
      throw new ApiError("not_authenticated", "Sign in to see this.", 401);
    }
    throw new ApiError("unknown", `Request failed (${response.status}).`, response.status);
  }

  return body as T;
}

export type OnboardingState = {
  /**
   * False when the account has no business row at all — a platform admin created by
   * `scripts/grant_platform_admin.py`, or an owner whose business was removed. Kept
   * separate from `onboarded` because such an account cannot onboard: the confirm
   * route writes to a specific business and there is none.
   */
  hasBusiness: boolean;
  onboarded: boolean;
  name: string | null;
  website: string | null;
};

/**
 * Whether this business has confirmed a profile yet.
 *
 * The dashboard needs it to decide what to lead with. A run without a confirmed DNA
 * cannot do anything — INTAKE exits with "no business profile" by design — so a screen
 * that offers "Start a run" first to a business with no profile is offering the one
 * action that cannot work.
 */
/**
 * Name the business, after the account exists.
 *
 * Signup used to refuse without a business name, so the field stood between somebody
 * and an account they had not yet decided how to use. This is that step, taken from
 * the dashboard when they are ready.
 */
export function createBusiness(name: string): Promise<{ businessId: string; name: string }> {
  return request<{ businessId: string; name: string }>("/api/v1/auth/business", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function fetchOnboardingState(): Promise<OnboardingState> {
  return request<OnboardingState>("/api/v1/onboarding");
}

export async function previewOnboarding(url: string): Promise<PreviewResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/v1/onboarding/preview`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ url }),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "network",
      `Cannot reach the API at ${API_URL}. Is it running? (make api)`,
      0,
    );
  }

  if (!response.ok) {
    // The API returns {detail: {code, message}} for the failures it anticipates,
    // and FastAPI's own validation shape for a malformed body. Handle both rather
    // than showing "[object Object]".
    const body: unknown = await response.json().catch(() => null);
    const detail = (body as { detail?: unknown } | null)?.detail;

    if (detail && typeof detail === "object" && "code" in detail) {
      const d = detail as { code: string; message: string };
      throw new ApiError(d.code, d.message, response.status);
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      throw new ApiError("invalid_request", first.msg ?? "That URL is not valid.", response.status);
    }
    throw new ApiError("unknown", `Request failed (${response.status}).`, response.status);
  }

  return (await response.json()) as PreviewResponse;
}

export type ConfirmResponse = {
  saved: boolean;
  website: string;
  services: string[];
  bannedClaims: string[];
};

/**
 * Store the DNA the owner just confirmed.
 *
 * The step that used to be missing: `previewOnboarding` drafted a DNA and handed it
 * back, and nothing could accept it -- so the draft was shown and thrown away, and every
 * business kept `dna = {}`. What that cost is not obvious from the screen: the agent
 * reads `website` from there to crawl the site, and the regulated-claim guard reads
 * `bannedClaims`, so neither worked for a real business.
 *
 * `credentials: "include"` because this one is AUTHENTICATED, unlike preview -- it writes
 * to a specific business, and the business comes from the session rather than the body.
 * The `Origin` header the CSRF guard requires is supplied by the browser automatically,
 * which is why this must be called from a client component and not a server one.
 */
export async function confirmOnboarding(
  dna: BusinessDna,
  sourceUrl: string,
): Promise<ConfirmResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/v1/onboarding/confirm`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ dna, sourceUrl }),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "network",
      `Cannot reach the API at ${API_URL}. Is it running? (make api)`,
      0,
    );
  }

  if (response.status === 401) {
    throw new ApiError("unauthenticated", "Please sign in first, then save.", 401);
  }

  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === "object" && "code" in detail) {
      const d = detail as { code: string; message: string };
      throw new ApiError(d.code, d.message, response.status);
    }
    throw new ApiError("unknown", `Could not save (${response.status}).`, response.status);
  }

  return (await response.json()) as ConfirmResponse;
}
