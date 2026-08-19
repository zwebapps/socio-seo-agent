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
