/**
 * Typed client for the automation setting.
 *
 * Mirrors `backend/app/api/automation.py`. Three things worth knowing before reading
 * callers:
 *
 * - **The route takes no business id** — it comes from the session, like `/memory`. The
 *   id is returned in the response.
 * - **`PUT` is a full replacement, and the response is the whole setting.** So the panel
 *   repaints from the server's account of what the scheduler will now do, never from an
 *   optimistic local guess — which is what lets it show a `nextRunAt` it did not compute.
 * - **`nextRunAt`, `lastRunAt` and `pausedReason` are read-only.** The first is a cache of
 *   the server's schedule arithmetic and the other two are the worker's record of what
 *   happened. `editableFields` is the server's own list of what it accepts, so the form's
 *   read-only markers cannot drift from the truth.
 */

import { request } from "./api";

/** Monday=0 .. Sunday=6, matching Python's `date.weekday()`. NOT ISO, NOT Postgres DOW. */
export type Automation = {
  businessId: string;
  /** False when no row exists: the values beside it are the defaults a first save applies. */
  configured: boolean;
  /** The owner's switch AND the absence of a system pause. Not the same as `mode`. */
  enabled: boolean;
  mode: string;
  cadence: string;
  dayOfWeek: number;
  hour: number;
  timezone: string;
  channels: string[];
  goalTemplate: string | null;
  /** The exact instant the worker compares against. Server-computed, always. */
  nextRunAt: string | null;
  lastRunAt: string | null;
  /** Why the system stopped by itself, in its own words. Rendered verbatim. */
  pausedReason: string | null;
  knownChannels: string[];
  knownCadences: string[];
  maxGoalLength: number;
  pollIntervalSeconds: number;
  editableFields: string[];
};

/** Exactly the fields `PUT` accepts. Every one, every time — it is a replacement. */
export type AutomationDraft = {
  enabled: boolean;
  cadence: string;
  dayOfWeek: number;
  hour: number;
  timezone: string;
  channels: string[];
  goalTemplate: string | null;
};

const AUTOMATION = "/api/v1/automation";

export function fetchAutomation(): Promise<Automation> {
  return request<Automation>(AUTOMATION);
}

export function saveAutomation(draft: AutomationDraft): Promise<Automation> {
  return request<Automation>(AUTOMATION, {
    method: "PUT",
    body: JSON.stringify(draft),
  });
}

/** The editable half of a loaded setting, ready to hand to `saveAutomation`. */
export function toDraft(automation: Automation): AutomationDraft {
  return {
    enabled: automation.enabled,
    cadence: automation.cadence,
    dayOfWeek: automation.dayOfWeek,
    hour: automation.hour,
    timezone: automation.timezone,
    channels: [...automation.channels],
    goalTemplate: automation.goalTemplate,
  };
}

/**
 * Monday-first, matching the `dayOfWeek` the API uses.
 *
 * Written out rather than derived from `Intl` with a synthetic date: the mapping from 0
 * to "Monday" is the thing most likely to be got wrong here (ISO says Monday is 1,
 * Postgres says Sunday is 0), so it is stated once, in the same order as the values.
 */
export const WEEKDAYS: readonly string[] = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

/** `linkedin` → `LinkedIn`, `blog_article` → `Blog article`. */
export function channelLabel(channel: string): string {
  const known: Record<string, string> = {
    linkedin: "LinkedIn",
    facebook: "Facebook",
    instagram: "Instagram",
    x: "X",
    email: "Email",
    blog_article: "Blog article",
  };
  return known[channel] ?? channel.replace(/_/g, " ");
}

export function cadenceLabel(cadence: string): string {
  const known: Record<string, string> = {
    weekly: "Every week",
    biweekly: "Every other week",
    monthly: "First of the month",
  };
  return known[cadence] ?? cadence;
}

/** `8` → `08:00`. The API stores an hour and no minute, deliberately. */
export function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00`;
}

/**
 * When the next run is due, in the reader's own timezone, or null.
 *
 * Formatted from the server's instant and never recomputed from the cadence: the whole
 * point of the API returning `nextRunAt` is that the screen and the worker quote one
 * answer. Rendered in the BROWSER's zone with the zone named, because the automation's
 * configured zone and the person reading it are not always the same — an owner on
 * holiday should not have to convert.
 */
export function nextRunLabel(automation: Automation): string | null {
  if (!automation.nextRunAt) return null;
  const when = new Date(automation.nextRunAt);
  if (Number.isNaN(when.getTime())) return null;
  return when.toLocaleString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

/**
 * Whether a due run has plainly not been picked up.
 *
 * A real signal rather than a caveat, and worth the arithmetic: the worker advances
 * `nextRunAt` BEFORE it starts a run, so a due row is claimed within one poll interval.
 * A `nextRunAt` still sitting in the past long after that means nothing is claiming it —
 * which in practice means the scheduler process is not running (`make worker`). Saying
 * "next run Thursday 06:00" while no process exists to honour it is exactly the kind of
 * confident wrong answer this project treats as worse than an error.
 *
 * The grace is five poll intervals, taken from the server's own reported interval so the
 * two cannot drift. Gated on `enabled`, because a PAUSED automation keeps its past
 * timestamp deliberately — the pause is the explanation, and it is already on screen.
 */
export function isOverdue(automation: Automation, now: Date = new Date()): boolean {
  if (!automation.enabled || !automation.nextRunAt) return false;
  const due = new Date(automation.nextRunAt).getTime();
  if (Number.isNaN(due)) return false;
  return now.getTime() - due > automation.pollIntervalSeconds * 5 * 1000;
}

/**
 * The one-line answer to "what will happen, and when".
 *
 * Deliberately says nothing when automation is off, rather than describing a schedule
 * that will not run: a stored cadence is not a promise, and the panel already shows the
 * fields it would use.
 */
export function scheduleSummary(automation: Automation): string {
  if (!automation.enabled) return "Nothing is scheduled.";
  const due = nextRunLabel(automation);
  const cadence = cadenceLabel(automation.cadence).toLowerCase();
  const at = `${WEEKDAYS[automation.dayOfWeek] ?? "?"} at ${hourLabel(automation.hour)} ${automation.timezone}`;
  return due ? `${cadence}, ${at}. Next run ${due}.` : `${cadence}, ${at}.`;
}
