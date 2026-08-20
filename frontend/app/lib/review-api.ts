/**
 * Typed client for one run's review surface.
 *
 * The types mirror `backend/app/services/review_service.py` exactly. Every content field
 * is nullable and every tab has a `*Note` beside it, because "there is nothing here yet,
 * and here is which node produces it" is a real answer this screen must render rather
 * than paper over.
 */

import { request } from "./api";

export type Draft = {
  title: string;
  metaDescription: string;
  html: string;
};

export type ReviewFinding = {
  code: string;
  /** "error" and "warn" are problems; "info" is a rule that PASSED. */
  severity: string;
  message: string;
  /**
   * The actionable half, and quantitative by contract: it names the measured value and
   * the target. This is also the exact string the agent feeds back to GENERATE on a
   * retry, so what the owner reads is what the model was told.
   */
  fixHint: string;
  measured: number | null;
  expected: string;
};

export type SeoReport = {
  score: number;
  passed: boolean;
  findings: ReviewFinding[];
  /** Set when VALIDATE could not score at all, e.g. no draft HTML existed. */
  note: string | null;
};

export type SocialPost = {
  channel: string;
  body: string;
  /** Measured server-side, so the count on the screen is the count the server took. */
  characters: number;
  /** What REPACK asked the model for and code then brought inside the channel's range. */
  hashtags: string[];
  /**
   * How many hashtags code had to remove, and how many are still missing.
   *
   * Rendered, not hidden. This is evidence about the MODEL: three tidy hashtags shown
   * without saying five were cut out in code reports the renderer's competence as the
   * model's. `shortfall` is never filled by inventing a tag.
   */
  hashtagsRemoved: number;
  hashtagsShortfall: number;
  /**
   * The channel's editorial target, its platform ceiling, and its hashtag cap — from the
   * one spec table the runtime renders to and the eval grades against. `null` for a
   * channel that table does not cover, because "0 / 0" would be a false limit.
   */
  characterTarget: number | null;
  characterLimit: number | null;
  hashtagLimit: number | null;
  /** Over the target, inside the limit: publishable, and longer than it should be. */
  overTarget: boolean;
};

export type AiBlocks = {
  targetKeyword: string | null;
  /** Self-contained, quotable answers — what an AI answer engine can cite. */
  blocks: string[];
  headings: string[];
  cta: string | null;
};

export type ReviewOpportunity = {
  title: string;
  rationale: string | null;
  score: number | null;
};

export type RunReview = {
  hasOutput: boolean;
  draft: Draft | null;
  draftNote: string | null;
  seo: SeoReport | null;
  seoNote: string | null;
  social: SocialPost[];
  socialNote: string | null;
  aiBlocks: AiBlocks | null;
  aiBlocksNote: string | null;
  opportunity: ReviewOpportunity | null;
  /** What could NOT be gathered. Rendered so the screen never implies research happened. */
  factGaps: string[];
  errors: { node: string; code: string; message: string }[];
};

export function fetchReview(runId: string): Promise<RunReview> {
  return request<RunReview>(`/api/v1/runs/${runId}/review`);
}
