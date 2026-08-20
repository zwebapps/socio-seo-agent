"use client";

/**
 * The review surface: one tab per kind of output a run produces.
 *
 * Deliberately not "N tabs": the count has now been wrong twice — it said four in
 * `review_service.py` and five here while the real number reached seven — and a number in
 * a comment cannot be derived from the array below it. `TABS` is the source of truth; a
 * reader who needs the count can read it there and it cannot go stale.
 *
 * The blog draft, the deterministic SEO findings, the social posts per platform, the
 * "AI blocks" — the self-contained answers an AI answer engine can quote — and the export
 * pack. One request feeds the first four (`GET /api/v1/runs/{id}/review`), deliberately
 * separate from the timeline poll so the draft HTML is not re-sent every couple of
 * seconds; the export pack fetches its own payload (`GET /api/v1/runs/{id}/export`) the
 * first time its tab is opened, because it is the same content measured for a different
 * purpose and nobody who never opens it should pay for it.
 *
 * Three things this screen refuses to do, each because the alternative would be a lie
 * about the product rather than merely a rough edge:
 *
 * - **It never fills an empty tab.** Every tab renders the server's own note — "GENERATE
 *   has not completed for this run" — instead of a placeholder draft. The whole claim of
 *   this product is that output is grounded in evidence; a review screen that invents
 *   content to look finished would undo that claim on the one screen where the owner
 *   checks it.
 * - **It never hides what was missing.** `factGaps` and node errors are shown above the
 *   tabs, so "written without live research" is stated rather than implied. That is the
 *   claims-discipline rule in docs/CRITERIA_MAP.md section 7 applied to UI copy.
 * - **It never renders the draft as HTML.** See `components/safe-html.tsx` — the draft is
 *   model output influenced by crawled pages, and this origin holds the owner's session.
 *
 * `fixHint` gets the visual weight in the SEO tab. `message` says a rule failed;
 * `fixHint` names the measured value and the target, and it is the same string fed back
 * to GENERATE on a retry — so it is both the actionable half for a human and the evidence
 * that the retry loop is real.
 */

import { useCallback, useEffect, useState } from "react";
import { Pill, SoftCard, SoftWell } from "../../components/soft";
import { SoftTabs, type TabSpec } from "../../components/tabs";
import { SafeHtml } from "../../components/safe-html";
import { ApiError } from "../../lib/api";
// One map of "how is this channel named on screen", shared with the export pack: two
// private copies is two chances for the same channel to be called two things.
import { channelLabel } from "../../lib/export-api";
import {
  fetchReview,
  type AiBlocks,
  type Draft,
  type Measurement,
  type Published,
  type PublishedTarget,
  type RetrievalAttempt,
  type RetrievalTrace,
  type ReviewFinding,
  type RunReview,
  type SeoReport,
  type SocialPost,
} from "../../lib/review-api";
import { CopyButton } from "./copy-button";
import { ExportPanel } from "./export";

/** From the GENERATE tool schema: the model is asked for these ranges. */
const TITLE_RANGE = { min: 50, max: 60 } as const;
const META_RANGE = { min: 140, max: 160 } as const;

export function RunReviewTabs({ runId, runState }: { runId: string; runState: string }) {
  const [review, setReview] = useState<RunReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState("draft");

  const load = useCallback(async () => {
    try {
      setReview(await fetchReview(runId));
      setError(null);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Could not load this run's output.");
    }
  }, [runId]);

  // Re-read whenever the run's state changes: output appears as the graph advances, and a
  // review fetched while the run was still at HARVEST would otherwise stay empty forever.
  useEffect(() => {
    void load();
  }, [load, runState]);

  if (error) {
    return (
      <SoftCard className="mt-6 p-5" size="md">
        <h2 className="text-sm font-semibold" style={{ color: "var(--err)" }}>
          {error}
        </h2>
      </SoftCard>
    );
  }

  if (!review) {
    return (
      <SoftCard className="mt-6 p-5" size="md">
        <p className="text-sm" style={{ color: "var(--text-muted)" }} aria-live="polite">
          Loading the output…
        </p>
      </SoftCard>
    );
  }

  const problems = review.seo?.findings.filter((f) => f.severity !== "info").length ?? 0;

  const tabs: TabSpec[] = [
    {
      id: "draft",
      label: "Draft",
      panel: <DraftPanel draft={review.draft} note={review.draftNote} opportunity={review.opportunity} />,
    },
    {
      id: "seo",
      label: "SEO findings",
      badge: review.seo ? problems : undefined,
      panel: <SeoPanel report={review.seo} note={review.seoNote} />,
    },
    {
      id: "social",
      label: "Social",
      badge: review.social.length || undefined,
      panel: <SocialPanel posts={review.social} note={review.socialNote} />,
    },
    {
      id: "ai",
      label: "AI blocks",
      badge: review.aiBlocks?.blocks.length || undefined,
      panel: <AiBlocksPanel blocks={review.aiBlocks} note={review.aiBlocksNote} />,
    },
    {
      // "Retrieval", not "Sources": this tab shows what was ASKED of the business's own
      // documents and how every answer was judged, including the runs where the answer
      // was "nothing useful". A tab called "Sources" would promise a citation list.
      id: "retrieval",
      label: "Retrieval",
      badge: review.retrieval.length || undefined,
      panel: <RetrievalPanel traces={review.retrieval} note={review.retrievalNote} />,
    },
    {
      // "Export pack", not "Publish": this tab produces text to paste and nothing else
      // reaches a platform. See `export.tsx` — the naming rule is asserted in its tests.
      id: "export",
      label: "Export pack",
      panel: <ExportPanel runId={runId} runState={runState} />,
    },
    {
      // "Delivery", not "Published": on most runs nothing was, and on the rest some of
      // it was simulated. A tab labelled "Published" would be a claim before the panel
      // has said anything.
      id: "delivery",
      label: "Delivery",
      badge: review.published ? review.published.succeeded || undefined : undefined,
      panel: (
        <DeliveryPanel
          published={review.published}
          publishedNote={review.publishedNote}
          measurement={review.measurement}
          measurementNote={review.measurementNote}
        />
      ),
    },
  ];

  return (
    <section className="mt-8" aria-labelledby="review-heading">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="review-heading" className="text-[19px] font-semibold tracking-tight">
          Review the output
        </h2>
        {!review.hasOutput && <Pill tone="muted">nothing produced yet</Pill>}
      </div>

      <Honesty gaps={review.factGaps} errors={review.errors} />

      <div className="mt-5">
        <SoftTabs tabs={tabs} active={active} onActivate={setActive} label="Run output" />
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------------- */
/* Honesty about what was NOT available                                       */
/* ------------------------------------------------------------------------- */

function Honesty({
  gaps,
  errors,
}: {
  gaps: string[];
  errors: { node: string; code: string; message: string }[];
}) {
  if (gaps.length === 0 && errors.length === 0) return null;

  return (
    <SoftCard className="mt-4 p-5" size="md" as="div">
      <h3 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--warn)" }}>
        What this was written without
      </h3>
      <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
        The run continued with less to go on. Nothing below implies research that did not
        happen.
      </p>
      {gaps.length > 0 && (
        <ul className="mt-3 space-y-1 text-sm">
          {gaps.map((gap) => (
            <li key={gap} className="flex gap-2">
              <span aria-hidden style={{ color: "var(--warn)" }}>
                —
              </span>
              <span>{gap}</span>
            </li>
          ))}
        </ul>
      )}
      {errors.length > 0 && (
        <ul className="mt-3 space-y-1.5 text-sm">
          {errors.map((e, i) => (
            <li key={`${e.node}-${e.code}-${i}`}>
              <span
                className="text-[11px] font-semibold uppercase tracking-wider"
                style={{ color: "var(--text-faint)" }}
              >
                {e.node}
              </span>{" "}
              <span style={{ color: "var(--text-muted)" }}>{e.message}</span>
            </li>
          ))}
        </ul>
      )}
    </SoftCard>
  );
}

/** The one empty state, used by all four tabs so they cannot drift apart. */
function Nothing({ note }: { note: string | null }) {
  return (
    <SoftWell className="p-5">
      <p className="text-sm" style={{ color: "var(--text-muted)" }}>
        {note ?? "There is nothing here yet."}
      </p>
    </SoftWell>
  );
}

/* ------------------------------------------------------------------------- */
/* Tab 1 — the draft                                                          */
/* ------------------------------------------------------------------------- */

function DraftPanel({
  draft,
  note,
  opportunity,
}: {
  draft: Draft | null;
  note: string | null;
  opportunity: { title: string; rationale: string | null; score: number | null } | null;
}) {
  const [view, setView] = useState<"reading" | "source">("reading");

  if (!draft) return <Nothing note={note} />;

  return (
    <div className="space-y-5">
      {opportunity && (
        <SoftCard className="p-5" size="md" as="div">
          <h3
            className="text-[11px] font-semibold uppercase tracking-[0.18em]"
            style={{ color: "var(--accent)" }}
          >
            Why this topic
          </h3>
          <p className="mt-1.5 text-sm font-medium">{opportunity.title}</p>
          {opportunity.rationale && (
            <p className="mt-1 text-sm" style={{ color: "var(--text-muted)" }}>
              {opportunity.rationale}
            </p>
          )}
          {opportunity.score !== null && (
            <p className="mt-2">
              <Pill tone="muted">lead-impact score {opportunity.score}/100</Pill>
            </p>
          )}
        </SoftCard>
      )}

      <SoftCard className="p-5" size="md" as="div">
        <Field label="Page title" value={draft.title} range={TITLE_RANGE} />
        <div className="mt-4">
          <Field label="Meta description" value={draft.metaDescription} range={META_RANGE} />
        </div>
      </SoftCard>

      <SoftCard className="p-5" size="md" as="div">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-sm font-semibold">The page</h3>
          <div role="group" aria-label="How to show the draft" className="flex gap-1.5">
            <ViewToggle current={view} value="reading" onSelect={setView}>
              Reading
            </ViewToggle>
            <ViewToggle current={view} value="source" onSelect={setView}>
              Source
            </ViewToggle>
          </div>
        </div>

        <div className="mt-4">
          {view === "reading" ? (
            <SafeHtml html={draft.html} />
          ) : (
            <SoftWell className="p-4">
              {/* Escaped text, never markup: this is the publishable source, shown as
                  characters. */}
              <pre
                className="overflow-x-auto text-xs leading-relaxed"
                style={{ color: "var(--text-muted)", whiteSpace: "pre-wrap" }}
              >
                {draft.html}
              </pre>
            </SoftWell>
          )}
        </div>
        <p className="mt-4 text-xs" style={{ color: "var(--text-faint)" }}>
          Links open in a new tab and are shown as text when their address is not a plain
          web or mail address.
        </p>
      </SoftCard>
    </div>
  );
}

function ViewToggle({
  current,
  value,
  onSelect,
  children,
}: {
  current: "reading" | "source";
  value: "reading" | "source";
  onSelect: (next: "reading" | "source") => void;
  children: React.ReactNode;
}) {
  const on = current === value;
  return (
    <button
      type="button"
      // aria-pressed, not a tab: this switches how ONE panel is displayed, it does not
      // switch panels. Calling it a tab would announce a structure that is not there.
      aria-pressed={on}
      onClick={() => onSelect(value)}
      className={`soft-press soft-edge px-3 py-1.5 text-xs font-semibold ${on ? "soft-raised" : ""}`}
      style={{
        borderRadius: "var(--r-pill)",
        color: on ? "var(--text)" : "var(--text-muted)",
        background: on ? "var(--surface-raised)" : "transparent",
      }}
    >
      {children}
    </button>
  );
}

function Field({
  label,
  value,
  range,
}: {
  label: string;
  value: string;
  range: { min: number; max: number };
}) {
  const length = value.length;
  const inRange = length >= range.min && length <= range.max;
  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
          {label}
        </h4>
        <span
          className="tabular text-[11px] font-semibold"
          style={{ color: inRange ? "var(--ok)" : "var(--warn)" }}
        >
          {length} characters
          <span style={{ color: "var(--text-faint)" }}>
            {" "}
            · asked for {range.min}–{range.max}
            {inRange ? " · in range" : " · out of range"}
          </span>
        </span>
      </div>
      <p className="mt-1.5 text-sm">{value || <em style={{ color: "var(--text-faint)" }}>empty</em>}</p>
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Tab 2 — deterministic SEO findings                                         */
/* ------------------------------------------------------------------------- */

function SeoPanel({ report, note }: { report: SeoReport | null; note: string | null }) {
  if (!report) return <Nothing note={note} />;

  const problems = report.findings.filter((f) => f.severity !== "info");
  const passes = report.findings.filter((f) => f.severity === "info");

  return (
    <div className="space-y-5">
      <SoftCard className="p-5" size="md" as="div">
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
              Score
            </p>
            <p
              className="tabular text-3xl font-semibold"
              style={{ color: report.passed ? "var(--ok)" : "var(--warn)" }}
            >
              {report.score}
              <span className="text-base" style={{ color: "var(--text-faint)" }}>
                /100
              </span>
            </p>
          </div>
          <Pill tone={report.passed ? "ok" : "warn"}>
            {report.passed ? "passed" : "needs work"}
          </Pill>
        </div>
        <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
          Measured by arithmetic over the markup, not by a model — the same scorer the
          agent uses to decide whether to rewrite.
        </p>
        {report.note && (
          <SoftWell className="mt-3 p-3">
            <p className="text-sm">{report.note}</p>
          </SoftWell>
        )}
      </SoftCard>

      {problems.length > 0 && (
        <section aria-labelledby="seo-problems">
          <h3 id="seo-problems" className="text-sm font-semibold">
            To fix ({problems.length})
          </h3>
          <ul className="mt-3 space-y-3">
            {problems.map((finding) => (
              <li key={finding.code}>
                <FindingRow finding={finding} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {passes.length > 0 && (
        <section aria-labelledby="seo-passes">
          <h3 id="seo-passes" className="text-sm font-semibold">
            Already fine ({passes.length})
          </h3>
          <ul className="mt-3 space-y-2">
            {passes.map((finding) => (
              <li key={finding.code} className="flex items-start gap-2.5 text-sm">
                <Pill tone="ok">pass</Pill>
                <span style={{ color: "var(--text-muted)" }}>{finding.message}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {report.findings.length === 0 && (
        <Nothing note="The scorer recorded no individual rule results for this run." />
      )}
    </div>
  );
}

function FindingRow({ finding }: { finding: ReviewFinding }) {
  const tone = finding.severity === "error" ? "err" : "warn";
  return (
    <SoftCard className="p-4" size="md" as="div">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={tone}>{finding.severity}</Pill>
        <span className="text-[11px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
          {finding.code.replace(/_/g, " ")}
        </span>
      </div>
      <p className="mt-2 text-sm font-medium">{finding.message}</p>

      {/* The actionable half gets the emphasis, because "your title is wrong" is not
          something anyone can act on and "it is 21 characters, write 50-60" is. */}
      {finding.fixHint && (
        <SoftWell className="mt-2.5 p-3">
          <h4
            className="text-[10px] font-semibold uppercase tracking-[0.16em]"
            style={{ color: "var(--accent)" }}
          >
            How to fix it
          </h4>
          <p className="mt-1 text-sm">{finding.fixHint}</p>
        </SoftWell>
      )}

      {(finding.measured !== null || finding.expected) && (
        <dl className="mt-2.5 flex flex-wrap gap-x-6 gap-y-1 text-xs">
          {finding.measured !== null && (
            <div className="flex gap-1.5">
              <dt style={{ color: "var(--text-faint)" }}>measured</dt>
              <dd className="tabular font-semibold">{finding.measured}</dd>
            </div>
          )}
          {finding.expected && (
            <div className="flex gap-1.5">
              <dt style={{ color: "var(--text-faint)" }}>target</dt>
              <dd className="font-semibold">{finding.expected}</dd>
            </div>
          )}
        </dl>
      )}
    </SoftCard>
  );
}

/* ------------------------------------------------------------------------- */
/* Tab — the retrieval trace: query -> chunks -> grades -> decision            */
/* ------------------------------------------------------------------------- */

/**
 * The fallback decision, IN WORDS.
 *
 * `fallback_to_web` is a decision the agent made, not an error it hit, and the words
 * have to say so — this is the one field on the panel whose raw value would be read as a
 * failure by anyone who has not read the code. `not_needed` is likewise a choice: judging
 * that a step needs no business facts is one of the four decisions that make this
 * retrieval agentic rather than a vector search with a nice name.
 *
 * An unrecognised outcome falls through to the raw value plus the server's own reason,
 * for the same reason `runStateTone` does: the server owns this vocabulary and can add to
 * it, and a screen that asserted otherwise would silently show the wrong sentence.
 */
const OUTCOME_WORDS: Record<string, string> = {
  sufficient: "Answered from the business's own documents.",
  fallback_to_web:
    "Fell back to live web research: the business's own documents did not answer this.",
  not_needed: "Decided no facts from the business's own documents were needed here.",
};

const OUTCOME_TONE: Record<string, "ok" | "warn" | "muted"> = {
  sufficient: "ok",
  // `warn`, not `err`. Nothing broke — the documents simply did not hold the answer, and
  // the run said so and carried on. Painting it red would teach the owner to treat a
  // correct degradation as an outage.
  fallback_to_web: "warn",
  not_needed: "muted",
};

/** `relevant` earns green; `partial` is weak context; `irrelevant` is not a fault. */
const GRADE_TONE: Record<string, "ok" | "warn" | "muted"> = {
  relevant: "ok",
  partial: "warn",
  irrelevant: "muted",
};

const DECISION_WORDS: Record<string, string> = {
  sufficient: "enough to ground the claim — stopped here",
  retry: "not enough — rewrote the query and searched again",
  exhausted: "still not enough, and the attempt ceiling was reached",
};

function RetrievalPanel({
  traces,
  note,
}: {
  traces: RetrievalTrace[];
  note: string | null;
}) {
  // The empty state renders the SERVER's sentence, which says that a business with no
  // uploaded documents had nothing to retrieve and names the node that recorded it. A
  // generic "nothing here yet" would read as a broken panel, and a panel that invented
  // "retrieval failed" would report the absence of a knowledge base as a defect in the
  // agent — sending the owner to hunt a bug instead of uploading a PDF.
  const first = traces[0];
  if (first === undefined) return <Nothing note={note} />;

  // `seq` is a 1-based ordinal over the run's retrieval calls and it survives the cap on
  // how many are stored, so a first entry numbered above 1 is the panel saying — without
  // needing a flag — that earlier calls were dropped. A cap that trimmed evidence
  // silently would be a quieter version of the bug this whole panel exists to fix.
  const dropped = first.seq - 1;

  return (
    <div className="space-y-5">
      <SoftCard className="p-5" size="md" as="div">
        <h3 className="text-sm font-semibold">What was asked of this business&rsquo;s documents</h3>
        <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
          Each node below decided whether it needed facts from the uploaded documents,
          wrote its own search query, graded every passage that came back, and then decided
          what to do about the result. The passages themselves are not shown here — a
          passage id and its grade are what make a claim checkable.
        </p>
        {dropped > 0 && (
          <p className="mt-2 text-xs" style={{ color: "var(--warn)" }}>
            This run made more retrieval calls than are kept in its saved state; the
            earliest {dropped} {dropped === 1 ? "is" : "are"} not shown.
          </p>
        )}
      </SoftCard>

      {traces.map((trace) => (
        <TraceCard key={`${trace.seq}-${trace.node}`} trace={trace} />
      ))}
    </div>
  );
}

function TraceCard({ trace }: { trace: RetrievalTrace }) {
  const words = OUTCOME_WORDS[trace.outcome];
  const tone = OUTCOME_TONE[trace.outcome] ?? "muted";

  return (
    <SoftCard className="p-5" size="md" as="div">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="text-[11px] font-semibold uppercase tracking-[0.18em]"
          style={{ color: "var(--accent)" }}
        >
          {trace.node}
        </span>
        <Pill tone={tone}>{trace.outcome.replace(/_/g, " ")}</Pill>
        {trace.groundingChunkIds.length > 0 && (
          <Pill tone="ok">
            {trace.groundingChunkIds.length} passage
            {trace.groundingChunkIds.length === 1 ? "" : "s"} cited
          </Pill>
        )}
      </div>

      {/* The decision in words. The raw `fallback_to_web` reads as an error to anybody
          who has not read the code, and it is not one. */}
      <p className="mt-2.5 text-sm font-medium">{words ?? trace.outcome.replace(/_/g, " ")}</p>
      {trace.outcomeReason && (
        <p className="mt-1 text-sm" style={{ color: "var(--text-muted)" }}>
          {trace.outcomeReason}
        </p>
      )}

      <SoftWell className="mt-3.5 p-3">
        <h4
          className="text-[10px] font-semibold uppercase tracking-[0.16em]"
          style={{ color: "var(--text-faint)" }}
        >
          What the node asked for
        </h4>
        <p className="mt-1 text-sm">{trace.question || <em>nothing recorded</em>}</p>
        {trace.needReason && (
          <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
            {trace.needed ? "Needed because" : "Not needed because"}: {trace.needReason}
          </p>
        )}
      </SoftWell>

      {trace.attempts.length > 0 && (
        <ol className="mt-4 space-y-4">
          {trace.attempts.map((attempt) => (
            <li key={attempt.attempt}>
              <AttemptRow attempt={attempt} total={trace.attemptsTotal} />
            </li>
          ))}
        </ol>
      )}

      <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-1 text-xs">
        <div className="flex gap-1.5">
          <dt style={{ color: "var(--text-faint)" }}>passages carried</dt>
          <dd className="tabular font-semibold">{trace.chunkCount}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt style={{ color: "var(--text-faint)" }}>model calls</dt>
          <dd className="tabular font-semibold">{trace.modelCalls}</dd>
        </div>
        {trace.costUsd && (
          <div className="flex gap-1.5">
            <dt style={{ color: "var(--text-faint)" }}>cost</dt>
            <dd className="tabular font-semibold">${trace.costUsd}</dd>
          </div>
        )}
        {trace.promptVersion && (
          <div className="flex gap-1.5">
            <dt style={{ color: "var(--text-faint)" }}>prompt</dt>
            <dd className="font-semibold">{trace.promptVersion}</dd>
          </div>
        )}
      </dl>

      {trace.notes.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs" style={{ color: "var(--text-muted)" }}>
          {trace.notes.map((entry) => (
            <li key={entry}>{entry}</li>
          ))}
        </ul>
      )}
    </SoftCard>
  );
}

function AttemptRow({ attempt, total }: { attempt: RetrievalAttempt; total: number }) {
  const decision = DECISION_WORDS[attempt.decision];

  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-2">
        <span
          className="text-[10px] font-semibold uppercase tracking-[0.16em]"
          style={{ color: "var(--text-faint)" }}
        >
          Attempt {attempt.attempt} of {total || attempt.attempt}
        </span>
        <span className="text-xs" style={{ color: "var(--text-muted)" }}>
          {attempt.relevant} relevant · {attempt.partial} partial · {attempt.irrelevant}{" "}
          irrelevant
        </span>
      </div>

      {/* The rewritten query. This is the field the whole "agentic" claim rests on: a
          system that embeds the node's own question is doing vector search. */}
      <p className="mt-1 text-sm">
        <span style={{ color: "var(--text-faint)" }}>searched for </span>
        <span className="font-medium">&ldquo;{attempt.query}&rdquo;</span>
      </p>
      {attempt.queryRationale && (
        <p className="mt-0.5 text-xs" style={{ color: "var(--text-muted)" }}>
          {attempt.queryRationale}
        </p>
      )}

      {attempt.grades.length > 0 && (
        <ul className="mt-2 space-y-2">
          {attempt.grades.map((grade) => (
            <li key={grade.chunkId} className="flex flex-wrap items-baseline gap-2 text-xs">
              <Pill tone={GRADE_TONE[grade.grade] ?? "muted"}>{grade.grade}</Pill>
              <code style={{ color: "var(--text-faint)" }}>
                {grade.documentId}#{grade.ordinal}
              </code>
              {grade.distance !== null && (
                <span className="tabular" style={{ color: "var(--text-faint)" }}>
                  distance {grade.distance.toFixed(3)}
                </span>
              )}
              {grade.reason && <span style={{ color: "var(--text-muted)" }}>{grade.reason}</span>}
            </li>
          ))}
        </ul>
      )}

      {attempt.gradesTotal > attempt.grades.length && (
        <p className="mt-1.5 text-xs" style={{ color: "var(--warn)" }}>
          {attempt.gradesTotal} passages were graded; {attempt.grades.length} are kept in this
          run&rsquo;s saved state.
        </p>
      )}

      <p className="mt-1.5 text-xs">
        <span style={{ color: "var(--text-faint)" }}>then: </span>
        <span style={{ color: "var(--text-muted)" }}>{decision ?? attempt.decision}</span>
      </p>
      {attempt.decisionReason && (
        <p className="mt-0.5 text-xs" style={{ color: "var(--text-muted)" }}>
          {attempt.decisionReason}
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Tab 3 — social posts, per platform                                         */
/* ------------------------------------------------------------------------- */

function SocialPanel({ posts, note }: { posts: SocialPost[]; note: string | null }) {
  if (posts.length === 0) return <Nothing note={note} />;

  return (
    <div className="space-y-4">
      <p className="text-sm" style={{ color: "var(--text-muted)" }}>
        One post per channel, in that channel&rsquo;s register. The claim is the same
        across all of them; only the length and the tone change.
      </p>
      {posts.map((post) => (
        <SoftCard key={post.channel} className="p-5" size="md" as="article">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">{channelLabel(post.channel)}</h3>
            <div className="flex items-center gap-3">
              <span className="tabular text-[11px]" style={{ color: "var(--text-muted)" }}>
                {/* Against the channel's own target when there is one, because "1,240
                    characters" answers nothing on its own. */}
                {post.characters.toLocaleString()}
                {post.characterTarget !== null && ` / ${post.characterTarget.toLocaleString()}`}{" "}
                characters
              </span>
              <CopyButton text={post.body} label={`Copy the ${post.channel} post`} />
            </div>
          </div>
          <SoftWell className="mt-3 p-4">
            <p className="text-sm leading-relaxed" style={{ whiteSpace: "pre-wrap" }}>
              {post.body}
            </p>
          </SoftWell>

          {post.hashtags.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-1.5">
              {post.hashtags.map((tag) => (
                <span
                  key={tag}
                  className="soft-flat px-2 py-0.5 text-[11px] font-medium"
                  style={{ borderRadius: "var(--r-pill)", color: "var(--text-muted)" }}
                >
                  {tag}
                </span>
              ))}
              {post.hashtagLimit !== null && (
                <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>
                  max {post.hashtagLimit}
                </span>
              )}
            </div>
          )}

          {/*
            What code had to correct, said out loud. A clean post shown without this
            credits the model for the renderer's work — and the hashtag engine exists
            because a measured run produced 21 tags against a prompt whose last line
            said "Keine Hashtags".
          */}
          <PostNotes post={post} />
        </SoftCard>
      ))}
      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        Length and hashtag counts were enforced in code after the post was written — the
        model is never asked to count, because it gets it wrong and the platform then
        rejects the post. The targets shown are the same ones the evaluation grades
        against.
      </p>
    </div>
  );
}

/**
 * The corrections and the shortfalls, or nothing at all.
 *
 * Three different facts, and they are not interchangeable. `removed` is work code did
 * that the model should not have needed. `shortfall` is a gap deliberately left open —
 * the engine refuses to fabricate a hashtag, so a channel wanting three and given one
 * says so. `overTarget` is publishable copy that is longer than it should be, which is
 * NOT the same as a post the platform would reject.
 */
function PostNotes({ post }: { post: SocialPost }) {
  const notes: string[] = [];
  if (post.hashtagsRemoved > 0) {
    notes.push(
      `${post.hashtagsRemoved} hashtag${post.hashtagsRemoved === 1 ? "" : "s"} removed to ` +
        `stay inside this channel's cap`,
    );
  }
  if (post.hashtagsShortfall > 0) {
    notes.push(
      `${post.hashtagsShortfall} short of this channel's hashtag minimum — none were ` +
        `invented to fill it`,
    );
  }
  if (post.overTarget && post.characterTarget !== null) {
    notes.push(
      `over the ${post.characterTarget.toLocaleString()}-character target, inside the ` +
        `platform limit — publishable, and longer than it should be`,
    );
  }
  if (notes.length === 0) return null;

  return (
    <ul className="mt-3 space-y-1">
      {notes.map((note) => (
        <li key={note} className="text-[11px]" style={{ color: "var(--warn)" }}>
          {note}
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------------- */
/* Delivery — what EXPORT actually did, and what MEASURE could not do         */
/* ------------------------------------------------------------------------- */

/**
 * The one panel in this product where an over-claim would be worst.
 *
 * EXPORT can simulate. With no credential for a destination it produces a real
 * `Outcome` with `fake=true` and a `fake://…` reference, and with no platform connection
 * it REFUSES with a reason. Both are successes of the design and neither is a post. So
 * this panel is built around a single rule: **a simulated or refused destination must be
 * impossible to read as a delivered one.** The word "Published" appears only on a row
 * that genuinely was, and every simulated row is labelled in words as well as colour.
 *
 * The headline is the server's own sentence, not one recomputed here. EXPORT already
 * folds "published N of M", which destinations failed, and whether anything was
 * simulated; deriving a second headline from the rows would be a second place for that
 * arithmetic to be wrong, and the two would disagree on exactly the runs that matter.
 */
function DeliveryPanel({
  published,
  publishedNote,
  measurement,
  measurementNote,
}: {
  published: Published | null;
  publishedNote: string | null;
  measurement: Measurement | null;
  measurementNote: string | null;
}) {
  if (!published) {
    return (
      <div className="space-y-4">
        <Nothing note={publishedNote} />
        <p className="max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
          Approving a run is what lets it publish. Until then the content is stored and
          reviewable — which is the whole point of the gate, not a limitation of it.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <SoftWell className="p-4">
        {/* EXPORT's own headline, verbatim. */}
        <p className="max-w-[70ch] text-sm">{published.note}</p>
        {published.simulated && (
          <p className="mt-2 max-w-[70ch] text-xs font-medium" style={{ color: "var(--warn)" }}>
            At least one destination was SIMULATED: no credential is configured for it, so
            nothing left this process. Treat those rows as a dry run, not a delivery.
          </p>
        )}
      </SoftWell>

      <ul className="space-y-2.5">
        {published.targets.map((target) => (
          <li key={`${target.actionType}:${target.target}`}>
            <div className="soft-flat soft-edge px-4 py-3" style={{ borderRadius: "var(--r-sm)" }}>
              <div className="flex flex-wrap items-center gap-2">
                <Pill tone={deliveryTone(target)}>{deliveryLabel(target)}</Pill>
                <span className="text-sm font-medium">
                  {/* `landing_page` is a destination, not a channel, so the shared
                      label helper is asked and its fallback is the raw target. */}
                  {channelLabel(target.target)}
                </span>
                <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>
                  {target.actionType}
                </span>
              </div>
              {/* The actuator's own line: it already refuses to overstate, and a screen
                  that showed only this still could not claim a real post. */}
              <p className="mt-1.5 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
                {target.summary}
              </p>
              {target.error && (
                <p className="mt-1 max-w-[70ch] text-xs" style={{ color: "var(--warn)" }}>
                  {target.error}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>

      <SoftWell className="p-4">
        <h3 className="text-sm font-semibold">Was the owner told?</h3>
        <p className="mt-1.5 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
          {published.notified
            ? "Yes — an email naming what went live and what did not."
            : published.notifyNote ||
              "No notification was sent, and no reason was recorded for that."}
        </p>
      </SoftWell>

      {measurement ? (
        <SoftWell className="p-4">
          <h3 className="text-sm font-semibold">What can be measured yet</h3>
          <p className="mt-1.5 max-w-[70ch] text-sm">
            {/* `leadsMeasured` is carried rather than derived from a count, because a
                count of zero and "nobody has arrived through a tracked link yet" are the
                same number and different claims — and only the second is true minutes
                after publishing. */}
            {measurement.leadsMeasured
              ? `${measurement.channels.length} channel(s) with attributable traffic.`
              : measurement.attributionNote ||
                "No leads are attributable yet. This is the attribution path, not a result."}
          </p>
          {measurement.gaps.length > 0 && (
            <>
              <h4 className="mt-3 text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
                Not measured
              </h4>
              <ul className="mt-1.5 space-y-1">
                {measurement.gaps.map((gap) => (
                  <li key={gap} className="max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
                    {gap}
                  </li>
                ))}
              </ul>
            </>
          )}
        </SoftWell>
      ) : (
        <Nothing note={measurementNote} />
      )}
    </div>
  );
}

/**
 * The pill for one destination. `simulated` beats `succeeded`, deliberately.
 *
 * A simulated send has `status: "succeeded"` — it did succeed, at simulating — so
 * colouring by status alone would paint a dry run green. Checking `simulated` first is
 * what makes the rule "a simulated destination cannot be read as a delivered one" hold
 * in the code rather than in a comment.
 */
function deliveryTone(target: PublishedTarget): "ok" | "warn" | "err" | "muted" {
  if (target.simulated) return "warn";
  if (target.status === "succeeded") return "ok";
  if (target.status === "failed") return "err";
  return "muted";
}

function deliveryLabel(target: PublishedTarget): string {
  if (target.simulated) return "simulated";
  if (target.status === "succeeded") return "published";
  return target.status;
}

/* ------------------------------------------------------------------------- */
/* Tab 4 — AI answer blocks                                                   */
/* ------------------------------------------------------------------------- */

function AiBlocksPanel({ blocks, note }: { blocks: AiBlocks | null; note: string | null }) {
  if (!blocks) return <Nothing note={note} />;

  return (
    <div className="space-y-5">
      <p className="text-sm" style={{ color: "var(--text-muted)" }}>
        Answers written to stand on their own, without the page around them — which is what
        an AI answer engine can quote. Each one should still make sense when it is the only
        sentence someone sees.
      </p>

      {blocks.targetKeyword && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
            written for
          </span>
          <Pill tone="accent">{blocks.targetKeyword}</Pill>
        </div>
      )}

      {blocks.blocks.length > 0 ? (
        <ol className="space-y-3">
          {blocks.blocks.map((block, index) => (
            <li key={block}>
              <SoftCard className="p-4" size="md" as="div">
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm leading-relaxed">{block}</p>
                  <CopyButton text={block} label={`Copy answer block ${index + 1}`} />
                </div>
              </SoftCard>
            </li>
          ))}
        </ol>
      ) : (
        <Nothing note={note} />
      )}

      {blocks.headings.length > 0 && (
        <SoftCard className="p-5" size="md" as="div">
          <h3 className="text-sm font-semibold">Section headings the page was built on</h3>
          <ul className="mt-2.5 space-y-1 text-sm" style={{ color: "var(--text-muted)" }}>
            {blocks.headings.map((heading) => (
              <li key={heading}>{heading}</li>
            ))}
          </ul>
        </SoftCard>
      )}

      {blocks.cta && (
        <SoftCard className="p-5" size="md" as="div">
          <h3 className="text-sm font-semibold">Call to action</h3>
          <p className="mt-1.5 text-sm">{blocks.cta}</p>
        </SoftCard>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* `CopyButton` lives in ./copy-button.tsx — shared with the export pack, which needs the
   same control and the same "no clipboard on a plain-HTTP origin" branch. */
