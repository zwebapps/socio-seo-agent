"use client";

/**
 * `/automation` — the switch that decides whether this thing works while you are asleep.
 *
 * The gap this fills was the inverse of the usual one. The worker has read
 * `automation_settings` since the scheduler shipped, and until `GET`/`PUT
 * /api/v1/automation` landed there was nothing anywhere that could write it: a row could
 * only come into existence by hand in SQL. So "the scheduler executes automations" was
 * true and "a business can automate its marketing" was not. The route closed half of
 * that; a route an owner cannot reach is the same gap one layer up, and this is the rest.
 *
 * A client component, and it has to be: every call carries the session cookie, and the
 * API's Origin-CSRF guard refuses a cookie-bearing write that arrives with no `Origin`
 * header — which is exactly what `fetch` from a server component sends.
 *
 * Four rules shape it, and each is about not overstating what is true.
 *
 * **The schedule is the server's answer, rendered.** `nextRunAt` is computed by the same
 * pure function the worker compares against, and this page never recomputes it from the
 * cadence. That is the whole reason the API returns it: a screen doing its own weekday
 * arithmetic would disagree with the worker twice a year, at the daylight-saving
 * boundaries, and be confidently wrong in between if anyone touched either copy.
 *
 * **A due run nobody picked up is SAID.** If `nextRunAt` is well past and still there,
 * nothing is claiming it — in practice the scheduler process is not running. Rendering
 * "next run Thursday 06:00" in that state would be the confident wrong answer;
 * `isOverdue` derives it instead of a static caveat nobody reads.
 *
 * **The system's pause is not the owner's switch.** `pausedReason` is rendered verbatim
 * and prominently, because the sentence the platform wrote (usually about a spent budget,
 * with the figures in it) is more useful than any summary of it, and because an
 * automation that reads "on" while doing nothing is the failure this page exists to make
 * impossible.
 *
 * **The form's vocabulary comes from the server.** Channels, cadences and the goal length
 * are all read off the response rather than restated here, so a picker cannot offer a
 * channel the API refuses — the drift that would otherwise show up as a 422 the owner
 * cannot act on.
 */

import Link from "next/link";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { Prose, Shell } from "@/app/components/page-shell";
import { Pill, SoftButton, SoftCard, SoftInput, SoftSelect, SoftToggle, SoftWell } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import {
  type Automation,
  type AutomationDraft,
  WEEKDAYS,
  cadenceLabel,
  channelLabel,
  fetchAutomation,
  hourLabel,
  isOverdue,
  nextRunLabel,
  saveAutomation,
  scheduleSummary,
  toDraft,
} from "@/app/lib/automation-api";

type State =
  | { kind: "loading" }
  | { kind: "ready"; automation: Automation }
  | { kind: "error"; message: string };

type Save =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; enabled: boolean }
  | { kind: "error"; message: string };

/**
 * The zones offered, when the browser cannot list them itself.
 *
 * `Intl.supportedValuesOf("timeZone")` is the right source — it is the browser's own IANA
 * database, so it cannot go stale against the server's — and this is only the fallback
 * for a runtime that lacks it. The stored value is always added, so a business configured
 * for a zone outside this list can never lose it by opening the page.
 */
const FALLBACK_ZONES = [
  "Europe/Berlin",
  "Europe/Vienna",
  "Europe/Zurich",
  "Europe/London",
  "Europe/Paris",
  "Europe/Madrid",
  "Europe/Rome",
  "Europe/Warsaw",
  "UTC",
];

function zoneOptions(current: string): string[] {
  const listed =
    typeof Intl.supportedValuesOf === "function"
      ? Intl.supportedValuesOf("timeZone")
      : FALLBACK_ZONES;
  return listed.includes(current) ? [...listed] : [current, ...listed];
}

export default function AutomationPage() {
  const [state, setState] = useState<State>({ kind: "loading" });

  const load = useCallback(async () => {
    try {
      setState({ kind: "ready", automation: await fetchAutomation() });
    } catch (exc) {
      // A 409 `no_business` is an account that has not finished onboarding and a 401 is a
      // session that has gone; the API's own message says which. Passed through rather
      // than replaced with a guess.
      setState({
        kind: "error",
        message:
          exc instanceof ApiError ? exc.message : "Could not load your automation setting.",
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Shell className="py-14">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Work
      </p>
      <h1 className="mt-2 text-[28px] font-semibold tracking-tight">Automation</h1>
      <Prose className="mt-3">
        <p className="text-base" style={{ color: "var(--text-muted)" }}>
          Switch this on and the agent starts a run on its own, on the day and at the hour
          you choose. It stops where every run stops — at the review gate, with drafts
          waiting for your yes. Nothing is ever published without you.
        </p>
      </Prose>

      {state.kind === "loading" && (
        <p className="mt-10 text-sm" style={{ color: "var(--text-muted)" }}>
          Loading…
        </p>
      )}

      {state.kind === "error" && (
        <SoftCard className="mt-10 p-6" size="lg">
          <p role="alert" className="text-sm font-medium" style={{ color: "var(--err)" }}>
            {state.message}
          </p>
        </SoftCard>
      )}

      {state.kind === "ready" && (
        <Editor
          automation={state.automation}
          onSaved={(next) => setState({ kind: "ready", automation: next })}
        />
      )}
    </Shell>
  );
}

function Editor({
  automation,
  onSaved,
}: {
  automation: Automation;
  onSaved: (next: Automation) => void;
}) {
  const [draft, setDraft] = useState<AutomationDraft>(() => toDraft(automation));
  const [save, setSave] = useState<Save>({ kind: "idle" });

  // Re-seed when the server's answer changes, so the form shows what was actually stored
  // — including anything it normalised on the way in (a channel folded to its canonical
  // name, a blank goal stored as nothing).
  useEffect(() => {
    setDraft(toDraft(automation));
    setSave({ kind: "idle" });
  }, [automation]);

  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(toDraft(automation)),
    [draft, automation],
  );

  async function submit() {
    setSave({ kind: "saving" });
    try {
      const next = await saveAutomation(draft);
      onSaved(next);
      setSave({ kind: "saved", enabled: next.enabled });
    } catch (exc) {
      // The API's sentence names the bound it refused — an unknown zone, an unknown
      // channel, a goal that is too long — and is written for the person in this form.
      setSave({
        kind: "error",
        message: exc instanceof ApiError ? exc.message : "Could not save that.",
      });
    }
  }

  function set<K extends keyof AutomationDraft>(key: K, value: AutomationDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  return (
    <div className="mt-10 grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_360px] lg:gap-10">
      <SoftCard className="p-6" size="lg">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="text-sm font-semibold">Run on a schedule</h2>
            <p className="mt-1 text-xs" style={{ color: "var(--text-muted)" }}>
              {draft.enabled
                ? "The agent will start runs for you."
                : "Nothing runs unless you start it yourself."}
            </p>
          </div>
          <SoftToggle
            checked={draft.enabled}
            onChange={(next) => set("enabled", next)}
            label="Run on a schedule"
          />
        </div>

        <div className="mt-6 grid gap-5 sm:grid-cols-2">
          <Field id="cadence" label="How often">
            <SoftSelect
              value={draft.cadence}
              onChange={(next) => set("cadence", next)}
              label="How often"
              className="w-full"
              options={automation.knownCadences.map((cadence) => ({
                value: cadence,
                label: cadenceLabel(cadence),
              }))}
            />
          </Field>

          <Field id="dayOfWeek" label="Which day">
            <SoftSelect
              value={String(draft.dayOfWeek)}
              onChange={(next) => set("dayOfWeek", Number(next))}
              label="Which day"
              className="w-full"
              options={WEEKDAYS.map((day, index) => ({ value: String(index), label: day }))}
            />
          </Field>

          <Field id="hour" label="At what time">
            <SoftSelect
              value={String(draft.hour)}
              onChange={(next) => set("hour", Number(next))}
              label="At what time"
              className="w-full"
              options={Array.from({ length: 24 }, (_, hour) => ({
                value: String(hour),
                label: hourLabel(hour),
              }))}
            />
          </Field>

          <Field id="timezone" label="In which timezone">
            <SoftSelect
              value={draft.timezone}
              onChange={(next) => set("timezone", next)}
              label="In which timezone"
              className="w-full"
              options={zoneOptions(draft.timezone).map((zone) => ({ value: zone, label: zone }))}
            />
          </Field>
        </div>

        {/* A fieldset, not a div: a group of checkboxes with one question needs a legend
            or a screen reader hears seven unrelated switches. */}
        <fieldset className="mt-6 border-0 p-0">
          <legend className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
            Which channels
          </legend>
          <div className="mt-3 flex flex-wrap gap-2">
            {automation.knownChannels.map((channel) => {
              const on = draft.channels.includes(channel);
              return (
                <label
                  key={channel}
                  className="soft-edge inline-flex cursor-pointer items-center gap-2 px-3 py-2 text-sm"
                  style={{
                    borderRadius: "var(--r-pill)",
                    background: on ? "var(--surface-sunken)" : "transparent",
                    color: on ? "var(--text)" : "var(--text-muted)",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() =>
                      set(
                        "channels",
                        on
                          ? draft.channels.filter((c) => c !== channel)
                          : [...draft.channels, channel],
                      )
                    }
                  />
                  {channelLabel(channel)}
                </label>
              );
            })}
          </div>
          <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
            Choose none and each run uses the default set — the same thing that happens
            when you start a run without picking channels.
          </p>
        </fieldset>

        <div className="mt-6">
          <Field id="goalTemplate" label="What each run should aim for">
            <SoftInput
              value={draft.goalTemplate ?? ""}
              onChange={(next) => set("goalTemplate", next)}
              label="What each run should aim for"
              controlId="goalTemplate"
              className="w-full"
              maxLength={automation.maxGoalLength}
              placeholder="more local enquiries for emergency call-outs"
            />
          </Field>
          <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
            Leave it empty and each run uses a sensible default goal. This is the same
            sentence you would type when starting a run by hand.
          </p>
        </div>

        <div className="mt-7 flex flex-wrap items-center gap-4">
          <SoftButton onClick={() => void submit()} disabled={!dirty || save.kind === "saving"}>
            {save.kind === "saving" ? "Saving…" : "Save"}
          </SoftButton>

          {dirty && save.kind !== "saving" && (
            <SoftButton variant="quiet" onClick={() => setDraft(toDraft(automation))}>
              Discard changes
            </SoftButton>
          )}

          {/* Politely announced rather than shouted: the schedule panel beside it has
              already repainted with the server's own answer, which is the real receipt. */}
          <p aria-live="polite" className="text-xs" style={{ color: "var(--text-muted)" }}>
            {save.kind === "saved" &&
              (save.enabled ? "Saved. The next run is scheduled." : "Saved. Automation is off.")}
          </p>
        </div>

        {save.kind === "error" && (
          <p role="alert" className="mt-4 text-sm font-medium" style={{ color: "var(--err)" }}>
            {save.message}
          </p>
        )}
      </SoftCard>

      <Status automation={automation} />
    </div>
  );
}

/**
 * What the worker will actually do, and what it last did.
 *
 * Reads only from the loaded `automation`, never from the draft: a panel that followed
 * unsaved edits would show a next run for a schedule nobody has stored.
 */
function Status({ automation }: { automation: Automation }) {
  const overdue = isOverdue(automation);
  const next = nextRunLabel(automation);

  return (
    <div className="space-y-6">
      <SoftCard className="p-6" size="lg">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-semibold">Right now</h2>
          {automation.enabled ? (
            <Pill tone="accent">on</Pill>
          ) : (
            <Pill>{automation.configured ? "off" : "not set up"}</Pill>
          )}
        </div>

        <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
          {scheduleSummary(automation)}
        </p>

        {/* The system's own words, not a summary of them. Usually the budget sentence,
            with both figures in it — which is what an owner needs to act on. */}
        {automation.pausedReason && (
          <SoftWell className="mt-4 p-4">
            <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--warn)" }}>
              Paused by us
            </p>
            <p className="mt-2 text-sm">{automation.pausedReason}</p>
            <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
              Switching automation back on above clears this and schedules the next run.
            </p>
          </SoftWell>
        )}

        {overdue && (
          <SoftWell className="mt-4 p-4">
            <p role="alert" className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--err)" }}>
              Overdue
            </p>
            <p className="mt-2 text-sm">
              This run was due {next} and has not started. Scheduled runs need the
              scheduler process running alongside the API — on this machine that is{" "}
              <code>make worker</code>.
            </p>
          </SoftWell>
        )}

        <dl className="mt-5 space-y-3 text-sm">
          <Row label="Next run" value={automation.enabled ? next : null} />
          <Row
            label="Last run"
            value={
              automation.lastRunAt
                ? new Date(automation.lastRunAt).toLocaleString(undefined, {
                    day: "numeric",
                    month: "long",
                    hour: "2-digit",
                    minute: "2-digit",
                  })
                : null
            }
          />
        </dl>

        <Link
          href="/runs"
          className="mt-5 inline-block text-sm font-medium underline"
          style={{ color: "var(--primary)" }}
        >
          See what the runs produced
        </Link>
      </SoftCard>

      <SoftCard className="p-6" size="lg">
        <h2 className="text-sm font-semibold">What a scheduled run does</h2>
        <ol className="mt-3 space-y-2 text-sm" style={{ color: "var(--text-muted)" }}>
          <li>Audits your site and looks for what people are searching for.</li>
          <li>Writes the posts for the channels you chose.</li>
          <li>Stops at the review gate and waits.</li>
        </ol>
        <p className="mt-4 text-xs" style={{ color: "var(--text-muted)" }}>
          It never publishes on its own. A scheduled run reaches exactly the same review
          screen as one you start yourself, and it is checked against your monthly
          spending ceiling first — if that is used up, automation pauses itself and says
          so here rather than spending past it.
        </p>
      </SoftCard>
    </div>
  );
}

/** A labelled control. The `<label>` is bound by id, so clicking the text focuses it. */
function Field({
  id,
  label,
  children,
}: {
  id: string;
  label: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label
        htmlFor={id}
        className="block text-xs font-semibold uppercase tracking-wider"
        style={{ color: "var(--text-muted)" }}
      >
        {label}
      </label>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: ReactNode | null }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="shrink-0 text-xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
        {label}
      </dt>
      <dd className="text-right">{value ?? <span style={{ color: "var(--text-faint)" }}>—</span>}</dd>
    </div>
  );
}
