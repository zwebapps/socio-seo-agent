/**
 * Typed client for the owner's dashboard summary — `GET /api/v1/dashboard`.
 *
 * **Every metric is nullable here, including the ones the endpoint documents as plain
 * integers.** That is the one decision in this file worth arguing about, so: the wire
 * shape arrives as `unknown`, and a key that is absent, `null`, `NaN` or a string is
 * indistinguishable from a key that was never measured. Typing `runsTotal` as `number`
 * would mean a payload missing that key renders `undefined` (blank) or, once someone
 * "fixes" the blank with `?? 0`, renders **zero runs** for a business that has run
 * fifty. Zero is a measurement. "Not measured" is not. This product's rule is that the
 * two are never confused, so the normaliser collapses everything unusable to `null` and
 * the tiles are obliged to say so in words.
 *
 * Two units that are easy to get wrong, both fixed by reading the backend rather than
 * guessing:
 *
 * - **`shareOfVoice` is a PERCENT, 0–100, already rounded to one decimal** by
 *   `_share()` in `backend/app/engines/geo/contract.py`. That helper returns `None`
 *   rather than `0.0` when there is nothing to divide by, for exactly the reason above.
 *   It is a SAMPLE of model answers, never a census (`docs/ARCHITECTURE.md` §15.2), and
 *   the tile that renders it is required to say so.
 * - **`spendUsd` is a STRING and stays one.** Money is `Decimal` server-side; parsing it
 *   into a JS number here would reintroduce binary floating point into the one value a
 *   customer would check against an invoice. It is rendered, never computed with — the
 *   same rule `app/lib/admin-api.ts` and the cost screen already follow.
 *
 * There is no `businessId` parameter and there must never be one: the business is
 * resolved from the session, and FastAPI ignores an unknown query parameter silently, so
 * one that appeared to "work" would be a cross-tenant read no test would notice.
 */

import { request } from "./api";

/** Clicks on tracked links, split by the channel the link was published to. */
export type ChannelClicks = { channel: string; clicks: number };

/**
 * The dashboard summary, after normalisation.
 *
 * `null` on any metric means **not measured** and must never be rendered as `0` or as a
 * bare dash. `gaps` is the endpoint's own prose about what it could not measure and is
 * rendered verbatim — it is the API's explanation, not ours to paraphrase.
 */
export type DashboardSummary = {
  clicksTotal: number | null;
  clicksByChannel: ChannelClicks[];
  clicksFromBots: number | null;
  runsTotal: number | null;
  runsAwaitingApproval: number | null;
  runsPartial: number | null;
  leadsTotal: number | null;
  /** A decimal string. Never parsed — see the module note. */
  spendUsd: string | null;
  seoProblems: number | null;
  seoPagesAudited: number | null;
  /** True when the crawl stopped early, so the audit covers only part of the site. */
  seoTruncated: boolean;
  /** Percent, 0–100. A sample, not a census. */
  shareOfVoice: number | null;
  gaps: string[];
};

/** A finite number, or `null` for everything else — see the module note. */
function metric(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * The per-channel breakdown, with unusable rows dropped rather than rendered.
 *
 * A row whose `clicks` is not a number would otherwise reach the screen as
 * "LinkedIn undefined", which reads as a bug in the dashboard rather than as a gap in
 * the data. Unknown channel NAMES are kept — `docs/ARCHITECTURE.md` §11 requires enum
 * consumers to tolerate values they do not recognise, and a short link can carry a
 * channel this frontend has never heard of.
 */
function channels(value: unknown): ChannelClicks[] {
  if (!Array.isArray(value)) return [];
  const rows: ChannelClicks[] = [];
  for (const raw of value) {
    if (raw === null || typeof raw !== "object") continue;
    const row = raw as { channel?: unknown; clicks?: unknown };
    const channel = text(row.channel);
    const clicks = metric(row.clicks);
    if (channel === null || clicks === null) continue;
    rows.push({ channel, clicks });
  }
  return rows;
}

function gaps(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(text).filter((entry): entry is string => entry !== null);
}

/**
 * Wire body → `DashboardSummary`.
 *
 * Exported for its own test. Everything here is a narrowing of `unknown`; nothing is
 * invented, and nothing unusable is turned into a number.
 */
export function normalizeSummary(body: unknown): DashboardSummary {
  const raw = (body === null || typeof body !== "object" ? {} : body) as Record<
    string,
    unknown
  >;
  return {
    clicksTotal: metric(raw.clicksTotal),
    clicksByChannel: channels(raw.clicksByChannel),
    clicksFromBots: metric(raw.clicksFromBots),
    runsTotal: metric(raw.runsTotal),
    runsAwaitingApproval: metric(raw.runsAwaitingApproval),
    runsPartial: metric(raw.runsPartial),
    leadsTotal: metric(raw.leadsTotal),
    spendUsd: text(raw.spendUsd),
    seoProblems: metric(raw.seoProblems),
    seoPagesAudited: metric(raw.seoPagesAudited),
    // Only a literal `true` is truncation. Anything else — absent, null, a string —
    // must not put "partial audit" on screen, because that is a claim about the crawl.
    seoTruncated: raw.seoTruncated === true,
    shareOfVoice: metric(raw.shareOfVoice),
    gaps: gaps(raw.gaps),
  };
}

export async function fetchDashboard(): Promise<DashboardSummary> {
  return normalizeSummary(await request<unknown>("/api/v1/dashboard"));
}

/**
 * The channel that earned the most clicks, or `null` when there is no breakdown.
 *
 * Ties resolve to the first row, so the same payload always renders the same channel —
 * the same first-seen-order discipline `engines/geo/score.py` applies to its own
 * breakdowns, and for the same reason: a headline that flips between two equal values on
 * every reload reads as the dashboard being unsure.
 */
export function topChannel(rows: readonly ChannelClicks[]): ChannelClicks | null {
  let best: ChannelClicks | null = null;
  for (const row of rows) {
    if (best === null || row.clicks > best.clicks) best = row;
  }
  return best;
}

/** Display names for the channels this frontend knows. Unknown ids render as they arrive. */
const CHANNEL_LABELS: Readonly<Record<string, string>> = {
  linkedin: "LinkedIn",
  facebook: "Facebook",
  instagram: "Instagram",
};

export function channelLabel(channel: string): string {
  return CHANNEL_LABELS[channel] ?? channel;
}
