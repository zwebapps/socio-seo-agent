/**
 * Typed client for starting a run and listing runs.
 *
 * Mirrors `backend/app/api/runs.py`. Two things about this file are load-bearing.
 *
 * **Every call here must run in the browser.** `request` sends the session cookie, and the
 * API's Origin-CSRF guard refuses a cookie-bearing write that arrives with no `Origin`
 * header — which is exactly what `fetch` from a server component sends. So a page that
 * imports this is `"use client"`, without exception. The same warning is on
 * `app/lib/admin-api.ts`, and it is the reason it is there.
 *
 * **`state` is typed as `string`, not as a union.** It is tempting to write
 * `"queued" | "running" | ...`, and it would be a lie: the server owns that vocabulary and
 * can add to it, at which point a union makes the compiler assert something false and any
 * `switch` over it silently takes a branch meant for a different state. Rendering code
 * therefore has to cope with a value it does not recognise, which `runStateTone` does.
 */

import { request } from "./api";

/** One row of the runs list. No timeline, and never the checkpoint. */
export type RunSummary = {
  runId: string;
  goal: string;
  /** See the module note: deliberately not a union. */
  state: string;
  currentNode: string | null;
  resumedCount: number;
  /**
   * Why the run stopped, when it stopped short.
   *
   * The field a screen is most tempted to drop, and the one that must not be. A run on
   * this deployment legitimately ends `partial` because the configured credential cannot
   * reach the mid tier; showing "partial" without this is announcing a terminal state the
   * owner cannot account for.
   */
  finishedReason: string | null;
  createdAt: string;
};

export type RunListResponse = {
  runs: RunSummary[];
  /**
   * Pass back as `?cursor=` for the next page, or `null` when this is the last one.
   *
   * There is deliberately no total: the list used to be a CAP, so a business past the
   * ceiling could not reach its older runs at all, and counting every run a business
   * has ever had would cost a query to render a number nobody acts on. "There is more"
   * is exactly what the button needs to know.
   */
  nextCursor: string | null;
};

/** What `POST /api/v1/runs` answers with: an id, and the state it starts in. */
export type StartedRun = { runId: string; state: string };

/**
 * How many runs the dashboard's "recent" panel shows, versus the full list.
 *
 * Both are below the API's own ceiling of 100, and the API is what enforces it — these are
 * a choice about how much to show, not a limit. Asking for more than the server allows is
 * a 422, so they are kept honest by the endpoint rather than by a comment.
 */
export const RECENT_RUNS = 5;
export const ALL_RUNS = 50;

export function fetchRuns(
  limit: number = ALL_RUNS,
  cursor: string | null = null,
): Promise<RunListResponse> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  return request<RunListResponse>(`/api/v1/runs?${query.toString()}`);
}

/**
 * Pick a stalled run back up from its checkpoint.
 *
 * The endpoint has existed since runs were made resumable and had NO caller: a run left
 * `running` by a process that died was recoverable only by curl. It is deliberately
 * narrow — the API refuses a finished run (re-running one would spend money to overwrite
 * work somebody may have approved) and refuses a run awaiting approval (that one is not
 * stalled, it is waiting for a person, and resuming it would step past the review gate).
 *
 * A run that is genuinely executing right now is also refused, with `run_already_executing`.
 * That is not an error to hide: only the executor can tell "a task is driving this" from
 * "a process died and left it there", and the caller should see which it was.
 */
export function resumeRun(runId: string): Promise<StartedRun> {
  return request<StartedRun>(`/api/v1/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
  });
}

/**
 * Whether a run is in a state the resume endpoint will accept.
 *
 * `queued` and `running` only. Mirrors `resume_run`'s own refusals rather than guessing
 * at them: offering a button that always 409s is worse than offering none.
 */
export function canResume(run: RunSummary): boolean {
  return run.state === "running" || run.state === "queued";
}

/**
 * Ask for a run. Returns as soon as the API has accepted it — 202, not 200.
 *
 * The work then happens in the background and takes MINUTES, so a caller must not present
 * this resolving as the run having finished. What it means is "there is now a run with this
 * id"; the timeline at `/runs/{runId}` is where the rest of the story arrives.
 *
 * `surfaces` is left off the body on purpose so the server's own default applies. Sending a
 * copy of a default from the client is how the two drift.
 */
export function startRun(goal: string): Promise<StartedRun> {
  return request<StartedRun>("/api/v1/runs", {
    method: "POST",
    body: JSON.stringify({ goal }),
  });
}

/** The API's own bounds on a goal, so the form can refuse early and say why. */
export const GOAL_MIN = 3;
export const GOAL_MAX = 500;

/**
 * Which states mean "nothing more is coming".
 *
 * The same set as `TERMINAL` in `backend/app/api/runs.py` and in the run timeline screen.
 * A list uses it to decide whether to keep polling at all: a dashboard of finished runs
 * should not hold a timer forever, and one with a live run must not go stale.
 */
const TERMINAL_STATES = new Set(["awaiting_approval", "done", "failed", "partial"]);

export function isLive(run: RunSummary): boolean {
  return !TERMINAL_STATES.has(run.state);
}

/**
 * The pill colour for a run state.
 *
 * One function, used by the dashboard, the runs list and the run timeline, because three
 * copies is three chances for the screens to disagree about what `partial` looks like.
 *
 * `partial` is `warn` and NOT `ok`, and that is the whole point of it having its own
 * colour: a partial run produced something but did not finish, and painting it green is
 * the exact failure this product cares most about avoiding.
 *
 * An unrecognised state falls through to `muted` rather than throwing — see the module
 * note on why `state` is a `string`.
 */
export function runStateTone(state: string): "ok" | "warn" | "err" | "accent" | "muted" {
  if (state === "done") return "ok";
  if (state === "awaiting_approval") return "accent";
  if (state === "failed") return "err";
  if (state === "partial") return "warn";
  return "muted";
}

/** `awaiting_approval` reads badly in a pill; the underscores are not information. */
export function runStateLabel(state: string): string {
  return state.replace(/_/g, " ");
}
