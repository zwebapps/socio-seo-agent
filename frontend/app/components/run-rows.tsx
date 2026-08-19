"use client";

/**
 * The list of runs, and the hook that keeps it current.
 *
 * Shared by the dashboard's "recent runs" panel and the full `/runs` page, because they are
 * the same list at two lengths — and because the alternative is two components that
 * gradually disagree about what a `partial` run looks like.
 *
 * The rule this component exists to enforce: **a row states the run's state and, when the
 * run stopped short, why.** A run here legitimately ends `partial` — the configured
 * credential cannot reach the mid tier, so the graph stops at OPPORTUNITY and says so in
 * `finishedReason`. Rendering that as "done", or as "partial" with no explanation, is the
 * failure this product cares most about: an owner who reads success for a run that produced
 * nothing stops believing anything else the product tells them.
 *
 * **`--text-faint` is deliberately not used on this screen.** Measured in the browser against
 * the light palette it is 2.28:1 on `--bg` and 2.39:1 on `--surface-raised`, well under the
 * 4.5:1 WCAG 1.4.3 asks of normal-size text — and the smallest text in a row is not decoration,
 * it is the node the run stopped at and the time it started. `--text-muted` (3.79 / 3.96) is
 * used instead: still short of 4.5, but the best the current palette offers without changing a
 * brand token, and a 1.66× improvement on the information that matters most here. The token
 * values themselves are left alone on purpose — raising them touches every existing screen and
 * is the palette owner's call, not this component's.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Pill, SoftWell } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import {
  fetchRuns,
  isLive,
  runStateLabel,
  runStateTone,
  type RunSummary,
} from "@/app/lib/runs-api";

/**
 * How often a list re-reads while a run is in flight.
 *
 * Slower than the run timeline's 2 s, on purpose. The timeline is watching one run
 * step-by-step and a late frame is visible as a stalled agent; a list only has to notice a
 * state CHANGE, of which there are at most a handful in a run that takes minutes. Five
 * seconds keeps it honest at a twentieth of the request volume.
 */
const POLL_MS = 5000;

type State =
  | { kind: "loading" }
  | { kind: "ready"; runs: RunSummary[] }
  | { kind: "error"; message: string };

/**
 * Load the runs list, and keep polling ONLY while something is actually running.
 *
 * The conditional is the point. A dashboard left open on an account whose runs have all
 * finished should not hold a timer and a request every five seconds forever — nothing can
 * change without the owner starting a run, and starting one refreshes the list anyway. A
 * list with a live run must not go stale, which is the other half.
 */
export function useRuns(limit: number): {
  state: State;
  live: boolean;
  reload: () => Promise<void>;
} {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [live, setLive] = useState(false);

  const reload = useCallback(async () => {
    try {
      const body = await fetchRuns(limit);
      setLive(body.runs.some(isLive));
      setState({ kind: "ready", runs: body.runs });
    } catch (exc) {
      // A 409 `no_business` is not a broken screen: it is an account that has not finished
      // onboarding, and the message the API sends says exactly that. Passed through rather
      // than replaced, for the same reason the memory panel shows refusals verbatim.
      setState({
        kind: "error",
        message: exc instanceof ApiError ? exc.message : "Could not load your runs.",
      });
    }
  }, [limit]);

  // The first read, and a fresh one if the caller changes how many it wants.
  useEffect(() => {
    void reload();
  }, [reload]);

  // Polling is keyed on `live`, which is what makes it start as well as stop: a manual
  // refresh, or a run started from the dashboard, flips `live` true and arms this; the poll
  // that sees the last run reach a terminal state flips it false and the cleanup disarms
  // it. Keying it on the data rather than mounting a permanent timer is the difference
  // between a dashboard that costs nothing when idle and one that polls a finished account
  // forever.
  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    let timer: number | null = null;

    // Chained timeouts rather than an interval, so the next request is scheduled only once
    // the previous one has settled. An interval against a slow API stacks overlapping
    // requests, and the responses can then land out of order.
    const tick = async () => {
      await reload();
      if (!cancelled) timer = window.setTimeout(() => void tick(), POLL_MS);
    };
    timer = window.setTimeout(() => void tick(), POLL_MS);

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [live, reload]);

  return { state, live, reload };
}

export function RunRows({
  state,
  emptyNote,
}: {
  state: State;
  /** What to say when there are no runs. Differs by screen, so the caller owns it. */
  emptyNote: string;
}) {
  if (state.kind === "loading") {
    return (
      <p className="py-3 text-sm" style={{ color: "var(--text-muted)" }}>
        Loading your runs…
      </p>
    );
  }

  if (state.kind === "error") {
    return (
      <SoftWell className="p-4">
        <p className="text-sm font-medium" style={{ color: "var(--err)" }}>
          {state.message}
        </p>
      </SoftWell>
    );
  }

  if (state.runs.length === 0) {
    return (
      <SoftWell className="p-4">
        <p className="text-sm" style={{ color: "var(--text-muted)" }}>
          {emptyNote}
        </p>
      </SoftWell>
    );
  }

  return (
    <ul className="space-y-2.5">
      {state.runs.map((run) => (
        <RunRow key={run.runId} run={run} />
      ))}
    </ul>
  );
}

/**
 * The node, with a word in front of it saying what the node MEANS for this state.
 *
 * `currentNode` is one field carrying three different facts, and printing it bare lets the
 * reader take the wrong one:
 *
 * - while a run is live it is where the agent **is**;
 * - on `awaiting_approval` it is where the run is **waiting** — that run has not stopped and
 *   nothing went wrong with it, it is parked at the review gate for a human, and calling
 *   that "stopped" would report the product's one deliberate pause as a fault;
 * - on `partial` or `failed` it is where the run **got to**, which is the most useful single
 *   fact about such a run. A run on this deployment typically stops at OPPORTUNITY, and
 *   "stopped at OPPORTUNITY" tells an owner it never reached GENERATE and so produced no
 *   draft — which is the difference between reading the state and understanding it.
 *
 * A `done` run carries a null node from the API, so there is nothing here to mislabel.
 */
function nodeCaption(state: string, node: string): string {
  if (state === "awaiting_approval") return `waiting at ${node}`;
  if (TERMINAL_FOR_CAPTION.has(state)) return `stopped at ${node}`;
  return node;
}

/** `awaiting_approval` is deliberately absent — see `nodeCaption`. */
const TERMINAL_FOR_CAPTION = new Set(["done", "failed", "partial"]);

function RunRow({ run }: { run: RunSummary }) {
  return (
    <li>
      {/*
        A `Link`, so the whole row is one target and the run is reachable by keyboard with a
        single Tab — and so middle-click, ctrl-click and "copy link address" all behave,
        which a clickable `div` breaks. `soft-flat` + `soft-edge` because it IS an
        interactive control: a neumorphic shadow measures about 1.2:1, so the hairline is
        what satisfies SC 1.4.11 and it is not optional. The focus ring comes from the
        global `:focus-visible` rule.
      */}
      <Link
        href={`/runs/${run.runId}`}
        className="soft-flat soft-edge soft-press block px-4 py-3"
        style={{ borderRadius: "var(--r-sm)", color: "var(--text)" }}
      >
        <span className="flex flex-wrap items-center gap-2">
          <Pill tone={runStateTone(run.state)}>{runStateLabel(run.state)}</Pill>
          {run.currentNode && (
            <span
              className="text-[11px] font-semibold uppercase tracking-wider"
              style={{ color: "var(--text-muted)" }}
            >
              {nodeCaption(run.state, run.currentNode)}
            </span>
          )}
          {run.resumedCount > 0 && <Pill tone="warn">resumed {run.resumedCount}×</Pill>}
        </span>

        <span className="mt-2 block text-sm font-medium">{run.goal}</span>

        {/*
          The reason it stopped, whenever there is one. Not folded away behind a click:
          "partial" on its own is a word an owner cannot act on, and the sentence next to it
          is the difference between "this product is broken" and "this deployment's
          credential cannot reach the mid tier".
        */}
        {run.finishedReason && (
          <span className="mt-1.5 block text-xs" style={{ color: "var(--warn)" }}>
            {run.finishedReason}
          </span>
        )}

        <span className="mt-1.5 block text-[11px]" style={{ color: "var(--text-muted)" }}>
          Started <RunTime iso={run.createdAt} />
        </span>
      </Link>
    </li>
  );
}

/**
 * When the run started, as a `<time>`.
 *
 * Absolute and locale-formatted, not "3 minutes ago": a relative string is wrong the moment
 * it is painted and needs its own timer to stay true, and an owner comparing this morning's
 * run to yesterday's wants the timestamp anyway. The machine-readable `dateTime` keeps the
 * exact instant available to anything that wants to do better.
 *
 * `toLocaleString` is safe from a hydration mismatch here even though this is a client
 * component that also renders on the server: rows exist only in the `ready` state, which is
 * reached by a fetch that cannot have resolved during the server render, so the server's
 * HTML for this subtree is the loading line and never a formatted date.
 *
 * `NaN` is handled rather than assumed away — `createdAt` is a string on the wire, and
 * `new Date("nonsense")` renders "Invalid Date" without complaining.
 */
function RunTime({ iso }: { iso: string }) {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return <>at an unrecorded time</>;
  return (
    <time className="tabular" dateTime={iso}>
      {parsed.toLocaleString()}
    </time>
  );
}
