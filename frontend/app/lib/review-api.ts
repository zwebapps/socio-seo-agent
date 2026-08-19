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
  /**
   * Measured server-side. There is deliberately no per-channel limit on the wire: two
   * limit tables already disagree in the backend, so the review screen does not publish
   * a third. REPACK has already trimmed to its ceiling by the time a post is stored.
   */
  characters: number;
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
