/**
 * Typed client for one run's export pack — Tier 3 in docs/CHANNELS.md §2.
 *
 * The types mirror `ExportPack` in `backend/app/services/review_service.py` exactly, and
 * two of them are worth reading rather than skimming:
 *
 * - `pasteText` is the string to paste. It is assembled on the SERVER, so that the count
 *   rendered beside it is a count of the same string. Joining the body and the hashtags
 *   in the client would mean measuring one thing and pasting another.
 * - `notice`, `channelsNote`, `landingPageNote` and `trackedLinkNote` are the honest
 *   halves. Every one of them is a sentence the API wrote; none is a fallback this screen
 *   invents when a field is empty.
 *
 * There is deliberately no `publish`, `schedule` or `send` function in this file. Nothing
 * in this product posts to a platform, so a client function named for it would be the
 * first half of a lie the UI would then have to tell.
 */

import { request } from "./api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

/** How a link reaches a reader on this channel. `bioHub` is the Instagram/TikTok truth. */
export type LinkMechanism = "inline" | "bio_hub" | "unknown";

export type ExportChannel = {
  channel: string;
  body: string;
  /** Exactly what to paste: the body, plus any declared hashtag the body was missing. */
  pasteText: string;
  hashtags: string[];
  /** Declared hashtags not already written into the body, so appended to `pasteText`. */
  appendedHashtags: string[];
  /** The body's own length — the same number the review screen shows for this post. */
  bodyCharacters: number;
  /** What the platform will actually receive. The limits are judged against this. */
  pasteCharacters: number;
  /** `null` for a channel the spec table does not cover: "0 / 0" would be invented. */
  characterTarget: number | null;
  characterLimit: number | null;
  hashtagCount: number;
  hashtagMinimum: number | null;
  hashtagLimit: number | null;
  hashtagsRemoved: number;
  hashtagsShortfall: number;
  /** Over the editorial target, inside the platform limit: publishable, and too long. */
  overTarget: boolean;
  /** Over the platform's reject threshold — it will be refused as it stands. */
  overLimit: boolean;
  /** `null` when no spec covers the channel. Never assumed to be `true`. */
  linkInBody: boolean | null;
  linkMechanism: LinkMechanism;
  /** One honest sentence per thing this channel will cost the poster. */
  notes: string[];
};

export type ExportProofPoint = { text: string; source: string };
export type ExportCta = { channel: string; text: string };

export type ExportLandingPage = {
  headline: string;
  subhead: string | null;
  offer: string;
  primaryCta: string;
  consentText: string | null;
  proofPoints: ExportProofPoint[];
  channelCtas: ExportCta[];
};

export type ExportAiBlocks = {
  targetKeyword: string | null;
  blocks: string[];
  headings: string[];
  cta: string | null;
};

export type ExportPack = {
  hasPack: boolean;
  /** "Nothing in this pack has been sent to any platform…" — rendered, never paraphrased. */
  notice: string;
  channels: ExportChannel[];
  channelsNote: string | null;
  landingPage: ExportLandingPage | null;
  landingPageNote: string | null;
  aiBlocks: ExportAiBlocks | null;
  aiBlocksNote: string | null;
  hubUrl: string | null;
  hubNote: string | null;
  /** Why there is no tracked short link yet, and what to do instead. */
  trackedLinkNote: string;
  factGaps: string[];
  errors: { node: string; code: string; message: string }[];
};

export function fetchExportPack(runId: string): Promise<ExportPack> {
  return request<ExportPack>(`/api/v1/runs/${runId}/export`);
}

/**
 * The absolute URL of the Markdown rendering, for a plain `<a href>`.
 *
 * A real link rather than a scripted blob download, and that is the better answer twice
 * over: it works with JavaScript switched off, and the file's name and its
 * `attachment` disposition come from the server instead of being re-invented in the
 * browser. The session cookie rides along because this is the same site.
 */
export function exportPackMarkdownUrl(runId: string): string {
  return `${API_URL}/api/v1/runs/${runId}/export?format=markdown`;
}

/** How a channel is named on screen. One map, so two screens cannot name it differently. */
const CHANNEL_LABEL: Record<string, string> = {
  linkedin: "LinkedIn",
  facebook: "Facebook",
  instagram: "Instagram",
  x: "X",
  email: "Email",
  blog_article: "Blog article",
  link_hub: "Link hub",
};

/**
 * The channel's display name, or the stored name unchanged.
 *
 * Unknown channels fall through as themselves rather than being prettified: the stored
 * name is what the eval grades and what the short link is tagged with, and a guessed
 * title case would make the screen and the data disagree about what this channel is.
 */
export function channelLabel(channel: string): string {
  return CHANNEL_LABEL[channel] ?? channel;
}
