/**
 * Typed client for the owner's captured leads.
 *
 * Mirrors `GET /api/v1/leads` in `backend/app/api/leads.py`. Read that module before
 * changing anything here — the response shape is narrower than it first looks, and the
 * gaps are deliberate:
 *
 * - **`fields` and `utm` are open maps** (`dict[str, Any]` server-side), because `fields`
 *   is a JSONB column. So every value that comes out of them is `unknown` here and has to
 *   be narrowed before it reaches the DOM. `asText` is that narrowing, in one place.
 * - **Attribution arrives as IDS, not names.** `contentPieceId` and `shortLinkId` are
 *   uuids; there is no title and no short-link code on this endpoint, and there is no other
 *   endpoint to join them against (`/go/{business}` returns labels but no ids). A screen
 *   must therefore show what the API actually knows and not invent a headline for it.
 * - **There is no `businessId` parameter and there must never be one.** The business comes
 *   from the session. FastAPI ignores unknown query parameters silently, so one that
 *   "worked" would be a cross-tenant read that no test would notice.
 *
 * Runs in the browser, like every other client in this directory: the cookie needs an
 * `Origin` header and a server component sends none.
 */

import { request } from "./api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

export type Lead = {
  id: string;
  /** The landing page that captured this lead, if it came through one. */
  contentPieceId: string | null;
  /** The short link the visitor arrived by, if any. An id — see the module note. */
  shortLinkId: string | null;
  source: string;
  status: string;
  utm: Record<string, unknown>;
  fields: Record<string, unknown>;
  createdAt: string;
};

export type LeadListResponse = { leads: Lead[] };

/** The API's default is 100 and its ceiling 500; the ceiling is enforced there, not here. */
export const LEADS_PAGE = 100;

export function fetchLeads(limit: number = LEADS_PAGE): Promise<LeadListResponse> {
  return request<LeadListResponse>(`/api/v1/leads?limit=${encodeURIComponent(limit)}`);
}

/**
 * One value out of the JSONB blob, as a string, or null if there is nothing to show.
 *
 * Everything in `fields` is `unknown`, so this is the only way a value reaches the screen.
 * A non-string that is not null (a number, a boolean) is stringified rather than dropped,
 * because a lead is a real person's enquiry and silently hiding part of it is worse than
 * showing it plainly. Empty and whitespace-only collapse to null so the caller can render
 * an honest "not given" instead of an empty row.
 */
export function asText(blob: Record<string, unknown>, key: string): string | null {
  const raw = blob[key];
  if (raw === null || raw === undefined) return null;
  const text = typeof raw === "string" ? raw : String(raw);
  return text.trim() === "" ? null : text.trim();
}

/**
 * The public URL of the landing page that earned a lead.
 *
 * `GET /p/{piece_id}` is served by the API, not by Next, so it is built from
 * `NEXT_PUBLIC_API_URL` and not from a relative path. It resolves only for a page whose
 * status is approved or published — a draft answers 404 by design, so that a 403 could not
 * confirm unpublished work exists. Callers should not promise the reader more than that.
 */
export function landingPageUrl(contentPieceId: string): string {
  return `${API_URL}/p/${contentPieceId}`;
}

/** The five real UTM keys the API keeps; anything else it drops before storing. */
export const UTM_KEYS = [
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
] as const;
