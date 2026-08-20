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

/**
 * One chunk, as retrieval judged it.
 *
 * There is no chunk TEXT here, and there is none in the checkpoint either. The id, the
 * document and the ordinal are the citation; the body is one indexed lookup away on the
 * server and would otherwise be re-serialised into a JSONB column on every node of every
 * run. `reason` is the grader's own justification and is what makes the grade reviewable
 * — "irrelevant" with no reason asks the owner to take the model's word for it.
 */
export type RetrievalGrade = {
  chunkId: string;
  documentId: string;
  ordinal: number;
  /** `relevant` · `partial` · `irrelevant`. A string, not a union: the server owns it. */
  grade: string;
  reason: string | null;
  /** `null` when the trace recorded none — which is NOT zero, i.e. not a perfect match. */
  distance: number | null;
};

/**
 * One turn of the retrieval loop.
 *
 * `query` is the REWRITE — what was actually embedded, in the words the documents would
 * use rather than the node's own question. It is the load-bearing field on this panel: a
 * system that embeds the question verbatim is doing vector search, and one that rewrites,
 * grades and then decides is doing retrieval the agent steered.
 */
export type RetrievalAttempt = {
  attempt: number;
  query: string;
  queryRationale: string | null;
  /** `sufficient` · `retry` · `exhausted`. A string, for the same reason `grade` is. */
  decision: string;
  decisionReason: string | null;
  relevant: number;
  partial: number;
  irrelevant: number;
  grades: RetrievalGrade[];
  /** How many were graded, against how many are listed. Unequal means a trim. */
  gradesTotal: number;
  notes: string[];
};

/**
 * One node's whole retrieval: question → rewritten queries → graded chunks → decision.
 *
 * `outcome` is the fallback decision and the reason this panel exists. `fallback_to_web`
 * means the business's own documents did not answer, so the run went on with live
 * research — a decision the agent MADE. `not_needed` is also a decision, not a miss.
 */
export type RetrievalTrace = {
  /** 1-based over the run's retrieval calls. A panel not starting at 1 has been trimmed. */
  seq: number;
  /** The graph node that asked. The trace itself does not know — five nodes call it. */
  node: string;
  question: string;
  needed: boolean;
  needReason: string | null;
  /** `sufficient` · `fallback_to_web` · `not_needed`. */
  outcome: string;
  outcomeReason: string | null;
  promptVersion: string | null;
  attempts: RetrievalAttempt[];
  attemptsTotal: number;
  /** Chunks graded `relevant`: the only citable evidence. */
  groundingChunkIds: string[];
  /** Chunks carried into the prompt, partials included. */
  chunkCount: number;
  modelCalls: number;
  /** A string, like every money value on this wire. */
  costUsd: string | null;
  notes: string[];
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
  /**
   * The agentic-RAG evidence, per node, in the order the run produced it.
   *
   * Empty for a business with no uploaded documents, and for any run whose checkpoint
   * predates the field. Both are normal, and `retrievalNote` is the server's own sentence
   * saying so — render it, never a generic "nothing here".
   */
  retrieval: RetrievalTrace[];
  retrievalNote: string | null;
  /** What could NOT be gathered. Rendered so the screen never implies research happened. */
  factGaps: string[];
  errors: { node: string; code: string; message: string }[];
  /** What EXPORT did, or `null` when it never ran — which is every unapproved run. */
  published: Published | null;
  publishedNote: string | null;
  measurement: Measurement | null;
  measurementNote: string | null;
};

/** One destination EXPORT tried, and what actually happened to it. */
export type PublishedTarget = {
  actionType: string;
  target: string;
  /** `succeeded` · `failed` · `refused`. A string, not a union: the server owns it. */
  status: string;
  externalRef: string | null;
  error: string | null;
  /**
   * The field this type exists for. A destination whose post never left the process
   * must be impossible to render as a success, and the only way to guarantee that on a
   * screen is for the screen to be handed the fact.
   */
  simulated: boolean;
  /** The actuator's own line, which already refuses to overstate. */
  summary: string;
};

export type Published = {
  /** EXPORT's own headline. Not recomputed here — it already folds "N of M". */
  note: string;
  attempted: number;
  succeeded: number;
  simulated: boolean;
  notified: boolean;
  notifyNote: string | null;
  targets: PublishedTarget[];
};

export type Measurement = {
  publishedRefs: number;
  channels: string[];
  simulated: boolean;
  /** What was NOT measured, and why. Shown, or the rest reads as zero. */
  gaps: string[];
  leadsMeasured: boolean;
  attributionNote: string | null;
};

export function fetchReview(runId: string): Promise<RunReview> {
  return request<RunReview>(`/api/v1/runs/${runId}/review`);
}
