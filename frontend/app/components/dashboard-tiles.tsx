"use client";

/**
 * The KPI tile row at the top of the owner's dashboard.
 *
 * Six numbers an owner would actually check: clicks (and where they came from), runs
 * (and how many are waiting on them), leads, problems on their own site, AI share of
 * voice, and what the models cost. Layout borrows the reference dashboard's tile row;
 * the surfaces, palette and radii are the existing soft-UI tokens, so nothing here
 * introduces a colour.
 *
 * **The one rule this file exists to enforce: a metric that was not measured renders as
 * a SENTENCE, never as `0` and never as a bare dash.** `KpiTile` therefore takes `note`
 * as a REQUIRED prop, so the type system refuses a tile whose author did not decide what
 * the absence of that number means. A dash with no explanation is the failure mode that
 * looks fine in review — it reads as "nothing happened" when the truth is "nobody has
 * measured this yet", and an owner who acts on the first has been misled by us.
 *
 * Three states, kept separate on purpose. **Loading** is not "not measured": rendering
 * the unmeasured copy during a round trip would tell a business with 400 clicks that it
 * has none, for as long as the request takes. **Unreachable** is not "zero" either, so a
 * failed read replaces the tiles with the refusal the API gave and a way to retry rather
 * than six confident zeroes. Only **ready** draws numbers.
 *
 * A client component, like every screen here: the call carries the session cookie and
 * the API's Origin-CSRF guard refuses a cookie-bearing request with no `Origin` header,
 * which is exactly what `fetch` from a server component sends.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";

import { Pill, SoftButton, SoftCard } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import {
  channelLabel,
  fetchDashboard,
  topChannel,
  type DashboardSummary,
} from "@/app/lib/dashboard-api";

/** Fixed locale so the grouping separator does not depend on the runner's environment. */
const COUNT = new Intl.NumberFormat("en-US");

type State =
  | { kind: "loading" }
  | { kind: "ready"; summary: DashboardSummary }
  | { kind: "error"; message: string };

/**
 * The message for a read that failed.
 *
 * A 404 gets its own sentence because it is the one failure that is not an outage: the
 * summary endpoint is simply not present on the API this build is talking to. `ApiError`
 * renders that as "Request failed (404)", which sends an owner to check their network
 * for a route that was never deployed.
 */
function failureMessage(exc: unknown): string {
  if (exc instanceof ApiError) {
    if (exc.status === 404) {
      return "This API build has no dashboard summary endpoint, so none of these numbers can be measured yet.";
    }
    return exc.message;
  }
  return "Your numbers could not be loaded.";
}

function useDashboard(): { state: State; reload: () => void } {
  const [state, setState] = useState<State>({ kind: "loading" });
  // Bumped by the retry button. A counter rather than calling the loader directly, so a
  // retry cannot start a second request while the first is still in flight.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setState({ kind: "loading" });
    void fetchDashboard()
      .then((summary) => {
        if (live) setState({ kind: "ready", summary });
      })
      .catch((exc: unknown) => {
        if (live) setState({ kind: "error", message: failureMessage(exc) });
      });
    return () => {
      live = false;
    };
  }, [attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, reload };
}

export function DashboardTiles({ hasBusiness = true }: { hasBusiness?: boolean }) {
  const { state, reload } = useDashboard();

  // A business-less account is NOT an error state, and rendering it as one was the
  // inverse of the failure this component exists to prevent. `GET /api/v1/dashboard`
  // sits behind `current_business`, which raises 409 `no_business` when the account owns
  // none — so the first thing such an owner saw on their own dashboard was
  // "This account has no business yet. Complete onboarding first." in red, announced
  // assertively, above the very panel that exists to fix it, beside a "Try again" button
  // that could never succeed. Not a fabricated measurement; a fabricated failure.
  //
  // The caller already knows the answer, so it is passed down rather than inferred from
  // the error text — matching on a message is how this breaks again the day the wording
  // changes.
  if (!hasBusiness) return null;

  return (
    <section aria-labelledby="kpi-heading" className="mt-10">
      <h2
        id="kpi-heading"
        className="text-[11px] font-semibold uppercase tracking-wider"
        style={{ color: "var(--text-muted)" }}
      >
        Where you stand
      </h2>

      {/* `aria-live` because these arrive from a fetch rather than from a click: a screen
          reader user who has already moved past this block would otherwise never learn
          the numbers appeared. */}
      <div className="mt-3" aria-live="polite">
        {state.kind === "loading" && (
          <SoftCard as="div" size="md" className="p-5">
            <p className="text-sm" style={{ color: "var(--text-muted)" }}>
              Reading your numbers…
            </p>
          </SoftCard>
        )}

        {state.kind === "error" && (
          <SoftCard as="div" size="md" className="p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p
                role="alert"
                className="max-w-[70ch] text-sm font-medium"
                style={{ color: "var(--err)" }}
              >
                {state.message}
              </p>
              <SoftButton onClick={reload} variant="quiet" ariaLabel="Try loading your numbers again">
                Try again
              </SoftButton>
            </div>
            {/* Said explicitly, because the tempting alternative is six zeroes. */}
            <p className="mt-2 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
              No figures are shown rather than zeroes: a number that could not be read is
              not a number that is nought.
            </p>
          </SoftCard>
        )}

        {state.kind === "ready" && <Tiles summary={state.summary} />}
      </div>
    </section>
  );
}

function Tiles({ summary }: { summary: DashboardSummary }) {
  const top = topChannel(summary.clicksByChannel);

  return (
    <>
      {/* A list, because it is one — six sibling metrics, announced as six items rather
          than as one run-on paragraph. `min-w-0` on each cell is what keeps a long
          channel name from pushing the grid into a horizontal scroll. */}
      <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
        <KpiTile
          label="Tracked clicks"
          value={summary.clicksTotal === null ? null : COUNT.format(summary.clicksTotal)}
          note="No clicks measured yet. A click is only counted when a published post carries a tracked link and somebody follows it."
          detail={
            <>
              {top === null ? (
                <span>No per-channel breakdown yet.</span>
              ) : (
                <span>
                  Most from {channelLabel(top.channel)} · {COUNT.format(top.clicks)}
                </span>
              )}
              {summary.clicksFromBots !== null && summary.clicksFromBots > 0 && (
                <span className="mt-0.5 block">
                  {COUNT.format(summary.clicksFromBots)} bot hits excluded from the count.
                </span>
              )}
            </>
          }
        />

        <KpiTile
          label="Runs"
          value={summary.runsTotal === null ? null : COUNT.format(summary.runsTotal)}
          note="No run count reported, so this is unknown rather than none. Start a run below and it will be counted."
          flag={
            summary.runsAwaitingApproval !== null && summary.runsAwaitingApproval > 0 ? (
              <Pill tone="accent">{summary.runsAwaitingApproval} await approval</Pill>
            ) : undefined
          }
          detail={
            <>
              <span>
                {summary.runsAwaitingApproval === null
                  ? "How many await approval was not reported."
                  : summary.runsAwaitingApproval === 0
                    ? "None awaiting your approval."
                    : `${COUNT.format(summary.runsAwaitingApproval)} waiting on you.`}
              </span>
              {/* A run that stopped short is the number this product is least allowed to
                  round off — see `components/run-rows.tsx`. */}
              {summary.runsPartial !== null && summary.runsPartial > 0 && (
                <span className="mt-0.5 block">
                  {COUNT.format(summary.runsPartial)} stopped short of finishing.
                </span>
              )}
            </>
          }
        />

        <KpiTile
          label="Leads"
          value={summary.leadsTotal === null ? null : COUNT.format(summary.leadsTotal)}
          note="No lead count reported. A lead is counted when somebody submits a form on one of your landing pages."
          detail={<span>People who got in touch through your content.</span>}
        />

        <KpiTile
          label="SEO problems on your site"
          value={summary.seoProblems === null ? null : COUNT.format(summary.seoProblems)}
          note="Your own site has not been audited yet, so nothing has been counted. The audit reads your pages — it is not a guess about them."
          // No flag pill here, and that is a measurement rather than a taste call. The
          // obvious one was `<Pill tone="warn">to fix</Pill>`, but `--warn` (#a35c07) on
          // the pill's own `soft-flat` background (`--surface`, #eceeec) measures
          // 4.40:1 in the browser — under the 4.5:1 SC 1.4.3 asks of 11px text. The
          // count is the signal anyway; a pill restating it would buy noise and a
          // contrast failure. (`soft.tsx` claims 4.71:1 for this tone, measured against
          // `--bg` rather than against the background the pill actually paints — see the
          // handover note, it is not this file's to fix.)
          detail={
            <>
              <span>
                {summary.seoPagesAudited === null
                  ? "How many pages were audited was not reported."
                  : `Across ${COUNT.format(summary.seoPagesAudited)} pages of yours.`}
              </span>
              {summary.seoTruncated && (
                <span className="mt-0.5 block">
                  The crawl stopped early, so this covers part of the site, not all of it.
                </span>
              )}
            </>
          }
        />

        <KpiTile
          label="AI share of voice"
          // One decimal even for a whole number: the backend rounds to one, and dropping
          // it would present 12.0% as if it were exact.
          value={summary.shareOfVoice === null ? null : `${summary.shareOfVoice.toFixed(1)}%`}
          note="Not sampled yet. This is measured by putting a fixed set of questions to the models and counting how often you are named — a sample, never a census."
          detail={
            // Required by docs/ARCHITECTURE.md §15.2: model answers are non-deterministic
            // and move with model updates, so this figure is a sample and saying otherwise
            // would present it as a market share it is not.
            <span>
              A sample of model answers, not a census — it shifts with model updates.
            </span>
          }
        />

        <KpiTile
          label="Model spend"
          // A string, straight through. Money is Decimal server-side and parsing it here
          // would put binary floating point into the one figure a customer checks against
          // an invoice.
          value={summary.spendUsd === null ? null : `$${summary.spendUsd}`}
          note="No model spend reported. Spend is recorded once a run reaches a paid provider — a run served by the fake provider costs nothing to record."
          detail={<span>Real provider cost recorded against your business.</span>}
        />
      </ul>

      <Gaps gaps={summary.gaps} />
    </>
  );
}

/**
 * One metric.
 *
 * `note` is required, and that is the whole design: a caller cannot add a tile without
 * having decided what the ABSENCE of its number means. The alternative — an optional
 * note with a dash as the fallback — is how "—" ends up on screen next to five real
 * figures, indistinguishable from a measured nothing.
 */
function KpiTile({
  label,
  value,
  note,
  detail,
  flag,
}: {
  label: string;
  /** Already formatted for display, or `null` when the metric was not measured. */
  value: string | null;
  /** What to say when `value` is `null`. Words, not a symbol. Required — see above. */
  note: string;
  /** Sub-line shown when there IS a value: what it is made of, or its caveat. */
  detail?: ReactNode;
  /**
   * A pill beside the figure, for a metric asking for attention. Only rendered when
   * there IS a figure — a flag next to "not measured" would be a claim about a number
   * nobody has.
   */
  flag?: ReactNode;
}) {
  const measured = value !== null;
  return (
    <li className="min-w-0">
      <SoftCard as="div" size="md" className="flex h-full flex-col p-5">
        <h3
          className="text-[11px] font-semibold uppercase tracking-wider"
          style={{ color: "var(--text-muted)" }}
        >
          {label}
        </h3>

        {/* The flag sits BESIDE the figure rather than up in the header row, which is
            where it started. A long label ("SEO problems on your site") wrapped the pill
            onto its own line, which pushed that tile's number a line lower than its
            neighbours' — six numbers on six different baselines reads as a broken grid.
            Beside the value it also qualifies the thing it is about. */}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
          {measured ? (
            <p
              className="tabular text-[30px] font-semibold leading-none tracking-tight break-words"
              style={{ color: "var(--text)" }}
            >
              {value}
            </p>
          ) : (
            // The pill states the condition in two words and the sentence below says why.
            // Both, because the pill alone is a label an owner has to interpret and the
            // sentence alone is easy to skim past when five siblings show big numbers.
            <Pill tone="muted">not measured</Pill>
          )}
          {measured && flag}
        </div>

        {/* Guarded rather than always rendered: an empty `<p>` still carries its top
            margin, so a tile with no detail line would sit a few pixels taller than its
            neighbours and read as a misaligned grid. */}
        {(measured ? detail !== undefined : true) && (
          <p
            className="mt-2 text-xs leading-relaxed break-words"
            style={{ color: "var(--text-muted)" }}
          >
            {measured ? detail : note}
          </p>
        )}
      </SoftCard>
    </li>
  );
}

/**
 * What the API says it could not measure, rendered verbatim.
 *
 * Verbatim because these strings are the API's own account of its gaps, and a frontend
 * paraphrase is a second source of truth for why a number is missing. Hidden when there
 * are none, so an empty "no gaps" panel does not become permanent furniture.
 */
function Gaps({ gaps }: { gaps: readonly string[] }) {
  if (gaps.length === 0) return null;
  return (
    <SoftCard as="div" size="md" className="mt-4 p-5">
      <h3 className="text-sm font-semibold">What is not measured yet</h3>
      <p className="mt-1 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
        Reported by the API itself. Nothing above fills one of these in with a zero.
      </p>
      <ul className="mt-3 space-y-1.5">
        {gaps.map((gap) => (
          <li key={gap} className="flex gap-2 text-sm" style={{ color: "var(--text)" }}>
            {/* aria-hidden, so the brand orange is legal here: it measures 2.54:1 and
                would fail 1.4.3 as text, but a decorative marker carries no information
                the sentence beside it does not. */}
            <span aria-hidden style={{ color: "var(--accent)" }}>
              ·
            </span>
            <span className="min-w-0 break-words">{gap}</span>
          </li>
        ))}
      </ul>
    </SoftCard>
  );
}
