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
 * Let a run past the review gate, so it can publish.
 *
 * The human decision the whole machine is built around. `REVIEW` is an interrupt: EXPORT
 * and MEASURE sit AFTER it in the graph and are unreachable without passing through, so
 * until this is called a run can be read in full and never publish anything.
 *
 * **No body, and that is the point.** The approver is the AUTHENTICATED USER, resolved by
 * the API from the session — it lands in `approved_by`, reaches `Actuation.approved_by`
 * and is persisted on every `actions` row, which is how "who authorised this post" stays
 * answerable months later. Sending an approver from here would be the client making an
 * authorisation decision, which is the same mistake `current_business` exists to avoid.
 * There is deliberately nothing in this function to send.
 *
 * 202, not 200: this returns as soon as the API has accepted the approval, and the work
 * then takes MINUTES. The state it answers with is `running`, and a caller must not
 * present it as `done`.
 *
 * Deliberately NOT idempotent: approving an already-running run is a 409
 * (`run_not_awaiting_approval`) rather than a quiet no-op, because the caller believes
 * they are approving something and the honest answer is that it is already going. The
 * other refusal is `no_checkpoint` — the run was parked before it produced anything, so
 * there is nothing to approve. Both are different sentences and a caller must not lump
 * them together.
 */
export function approveRun(runId: string): Promise<StartedRun> {
  return request<StartedRun>(`/api/v1/runs/${encodeURIComponent(runId)}/approve`, {
    method: "POST",
  });
}

/**
 * Whether a run is in the one state the approve endpoint will accept.
 *
 * `awaiting_approval` only, mirroring `approve_run`'s own refusal rather than guessing at
 * it — same rule as `canResume`. Takes the state rather than a `RunSummary` because the
 * run timeline screen carries its own richer run shape and the state is the only fact
 * that decides this.
 *
 * The consequence for a screen: a control that cannot ever work is worse than no control,
 * so this gates whether the button EXISTS, not whether it is disabled.
 */
export function canApprove(state: string): boolean {
  return state === "awaiting_approval";
}

/**
 * What `POST /api/v1/runs/{id}/reject` answers with.
 *
 * Its own type rather than `StartedRun`, and the third field is the reason it exists: the
 * API reads the run back after writing and reports the STORED reason, so a screen renders
 * what was persisted rather than what it typed. Those differ — the API collapses
 * whitespace before it stores — and the difference is exactly the kind of thing a screen
 * should not be guessing at.
 */
export type RunDecision = { runId: string; state: string; finishedReason: string | null };

/**
 * The API's own bounds on a rejection reason, so the field can refuse early and say why.
 *
 * Mirrored from `REJECT_REASON_MIN`/`REJECT_REASON_MAX` in `backend/app/api/runs.py`, the
 * same way `GOAL_MIN`/`GOAL_MAX` are — imported at the call site rather than retyped, so
 * the client and the API cannot drift into disagreeing about what a valid reason is.
 *
 * `MAX` is 240 and not the column's 255 because `clamp_reason` TRUNCATES: silently
 * shortening a machine-authored stack trace is cosmetic, silently shortening a person's
 * stated reason is not. So the API's 422 is the only length refusal a human can meet, and
 * this is what stops them meeting it.
 */
export const REJECT_REASON_MIN = 10;
export const REJECT_REASON_MAX = 240;

/**
 * The reason, measured the way the API measures it.
 *
 * `RejectRunRequest` collapses whitespace in a `mode="before"` validator and THEN applies
 * the bounds, so `"          "` is not a ten-character reason and neither is a newline
 * pressed forty times. A client that checked `raw.length` would pass those and be refused
 * by a 422 it had told the person could not happen — so the client measures the same
 * string, and sends the same string it measured.
 */
export function cleanRejectReason(raw: string): string {
  return raw.split(/\s+/).filter(Boolean).join(" ");
}

/**
 * Refuse a parked run's output, terminally.
 *
 * The other half of the review gate. Rejecting is **not reversible**: the run ends
 * `rejected` and the recovery is a NEW run, which re-derives from current documents rather
 * than republishing what was refused. A second reject, and an approve-after-reject, are
 * both the existing 409 `run_not_awaiting_approval` — never a silent no-op.
 *
 * 200, not approve's 202: rejecting starts no work, so it is complete when it returns. A
 * caller must not present it as "publishing", and must not poll for anything afterwards.
 *
 * **A reason is REQUIRED and is the whole record.** Nothing is written to `feedback` — no
 * run has a content piece at review — so `state` plus `finished_reason` is all a rejection
 * ever leaves behind, and the reviewer is the only person who will ever know why. That is
 * why the bounds are the API's and not a suggestion.
 *
 * **No rejecter is sent, and none is recorded** — a difference from approve, not an
 * oversight. `approved_by` exists because an approval authorises an outward publish and
 * lands on every `actions` row; a rejection authorises nothing and sends nothing.
 *
 * There is deliberately no `no_checkpoint` refusal here: a run parked having produced
 * nothing is precisely what a reviewer should be able to dismiss. A reviewer must always
 * be able to say no.
 */
export function rejectRun(runId: string, reason: string): Promise<RunDecision> {
  return request<RunDecision>(`/api/v1/runs/${encodeURIComponent(runId)}/reject`, {
    method: "POST",
    // Only a reason. See the docstring: there is nothing else this route accepts, and
    // nothing else it should be given.
    body: JSON.stringify({ reason: cleanRejectReason(reason) }),
  });
}

/**
 * Whether a run is in the one state the reject endpoint will accept.
 *
 * The same predicate as `canApprove` today, and deliberately its own name rather than a
 * shared call: the two endpoints answer the same 409 for the same condition NOW, and the
 * day one of them accepts a state the other refuses — reject has no `no_checkpoint`
 * refusal already — the seam is where it needs to be instead of being introduced under
 * pressure.
 */
export function canReject(state: string): boolean {
  return state === "awaiting_approval";
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
export function startRun(goal: string, channels?: readonly string[]): Promise<StartedRun> {
  return request<StartedRun>("/api/v1/runs", {
    method: "POST",
    // `channels` is omitted rather than sent empty when nobody chose. The API reads an
    // absent field as "use the default set" and records `[]` on the row, which keeps
    // "nobody chose" distinguishable from "chose all three" — sending `[]` explicitly
    // would be the same request with a claim attached to it.
    body: JSON.stringify(channels && channels.length > 0 ? { goal, channels } : { goal }),
  });
}

/**
 * The channels a run can render posts for, and the label each one gets.
 *
 * Mirrors `CHANNEL_SPECS` in `backend/app/engines/channel/specs.py`, restricted to the
 * three the product renders by default. It is a short list rather than a fetch because
 * the API refuses an unknown channel by name — so the failure mode of drift here is a
 * 422 the form shows verbatim, not a silently dropped channel.
 */
export const RUN_CHANNELS = [
  { id: "linkedin", label: "LinkedIn" },
  { id: "facebook", label: "Facebook" },
  { id: "instagram", label: "Instagram" },
] as const;

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
const TERMINAL_STATES = new Set([
  "awaiting_approval",
  "done",
  "failed",
  "partial",
  // A human said no. Nothing in the machine will ever move this run again -- the recovery
  // from a rejection is a NEW run -- so a screen that left it out would poll a finished
  // run forever and hold an event stream open for events that cannot come.
  "rejected",
]);

/**
 * Whether nothing more is coming for a run in this state.
 *
 * Exported because the run timeline needs the same answer for a run shape that is not a
 * `RunSummary`, and a second copy of the set there is a second chance for the two screens
 * to disagree about which states are over — which is precisely how `rejected` would have
 * been added to one and not the other.
 */
export function isTerminalState(state: string): boolean {
  return TERMINAL_STATES.has(state);
}

export function isLive(run: RunSummary): boolean {
  return !isTerminalState(run.state);
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
 * `rejected` is `muted`, and it is written as its own branch rather than left to the
 * default so that it is INTENT and not the accident of falling through. The reasoning is
 * `partial`'s, pointed the other way: `partial` is `warn` so a shortfall is not read as a
 * success, and `rejected` must not be `warn` or `err` because a person deciding "no" is
 * neither a shortfall nor a fault — and a fault colour on it tells the owner the machine
 * broke when what actually happened is that they made a call.
 *
 * An unrecognised state falls through to `muted` rather than throwing — see the module
 * note on why `state` is a `string`.
 */
export function runStateTone(state: string): "ok" | "warn" | "err" | "accent" | "muted" {
  if (state === "done") return "ok";
  if (state === "awaiting_approval") return "accent";
  if (state === "failed") return "err";
  if (state === "partial") return "warn";
  if (state === "rejected") return "muted";
  return "muted";
}

/** `awaiting_approval` reads badly in a pill; the underscores are not information. */
export function runStateLabel(state: string): string {
  return state.replace(/_/g, " ");
}
