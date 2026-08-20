"use client";

/**
 * The review surface: five tabs over one run's output.
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
      // "Export pack", not "Publish": this tab produces text to paste and nothing else
      // reaches a platform. See `export.tsx` — the naming rule is asserted in its tests.
      id: "export",
      label: "Export pack",
      panel: <ExportPanel runId={runId} runState={runState} />,
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
