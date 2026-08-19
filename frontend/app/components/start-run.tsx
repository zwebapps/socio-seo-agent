"use client";

/**
 * "What should the agent work on?" — the control that starts a run.
 *
 * `POST /api/v1/runs` has worked since the runs API landed and **nothing in the frontend
 * called it**. A run could only be started with curl, which made every screen downstream of
 * one — the timeline, the four review tabs, the whole review gate — unreachable for the
 * person the product is for. This component is that missing call.
 *
 * Three decisions worth naming.
 *
 * **It is a real `<form>`.** Not a button with a click handler beside an input: a form gives
 * Enter-to-submit for free, which is what everyone actually does after typing in a single
 * text field, and it is what makes the whole thing operable from the keyboard without any
 * key handling of our own.
 *
 * **The goal is validated here against the API's own bounds, and the API's refusal is shown
 * verbatim when one gets through.** The local check exists to save a round trip that comes
 * back 422; it is not the authority, and `GOAL_MIN`/`GOAL_MAX` are imported rather than
 * retyped so the two cannot drift.
 *
 * **On success it navigates to the run.** The API answers 202 — accepted, not finished — and
 * the work then takes minutes. Staying here with a "started!" message would leave the owner
 * on a screen that tells them nothing while the interesting thing happens elsewhere; the
 * timeline is built to show exactly that, and it already polls.
 */

import { useRouter } from "next/navigation";
import { useId, useState } from "react";

import { SoftButton, SoftInput } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import { GOAL_MAX, GOAL_MIN, startRun } from "@/app/lib/runs-api";

export function StartRunForm() {
  const router = useRouter();
  const [goal, setGoal] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `useId` rather than a hard-coded string: this form is on the dashboard today and could
  // sit beside another instance tomorrow, and two inputs sharing an id bind the visible
  // label to the wrong one. That exact bug is documented on `SoftRange.controlId`.
  const inputId = useId();
  const hintId = useId();

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = goal.trim();

    if (trimmed.length < GOAL_MIN) {
      setError(`Describe the goal in at least ${GOAL_MIN} characters.`);
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const started = await startRun(trimmed);
      // A router push, not `window.location`: the session and the fetched state on the run
      // page belong to the same account, so there is nothing to tear down — unlike sign-out,
      // where the full navigation is deliberate.
      router.push(`/runs/${started.runId}`);
    } catch (exc) {
      // The API's own words. "This account has no business yet. Complete onboarding first."
      // is a 409 an owner can act on; "Request failed (409)" is not.
      setError(exc instanceof ApiError ? exc.message : "The run could not be started.");
      setSubmitting(false);
    }
    // Deliberately no `finally`: on success this component is being navigated away from, and
    // clearing `submitting` would flash the button back to "Start a run" as it unmounts,
    // which reads as though the click had not worked.
  }

  return (
    <form onSubmit={submit} className="mt-6">
      <label
        htmlFor={inputId}
        className="block text-[11px] font-semibold uppercase tracking-wider"
        style={{ color: "var(--text-muted)" }}
      >
        What should the agent work on?
      </label>

      <div className="mt-2 flex flex-wrap items-start gap-3">
        <SoftInput
          controlId={inputId}
          describedBy={hintId}
          label="What should the agent work on?"
          value={goal}
          onChange={(next) => {
            setGoal(next);
            // Clear a stale refusal as soon as the person starts fixing it, rather than
            // leaving an error sitting under a field they have already changed.
            if (error) setError(null);
          }}
          placeholder="more local leads for emergency plumbing"
          maxLength={GOAL_MAX}
          className="min-w-0 flex-1"
        />
        <SoftButton type="submit" variant="primary" disabled={submitting}>
          {submitting ? "Starting…" : "Start a run"}
        </SoftButton>
      </div>

      <p id={hintId} className="mt-2.5 text-xs" style={{ color: "var(--text-muted)" }}>
        A run takes a few minutes. You will land on its timeline and can leave the page —
        it keeps going. Runs sometimes stop short of the end; when one does, the timeline
        says which step it reached and why.
      </p>

      {/*
        `role="alert"` so a refusal is announced the moment it arrives. A screen-reader user
        who submits and hears nothing has no way to know the form refused them — and this
        message is the only place the reason exists.
      */}
      {error && (
        <p role="alert" className="mt-2 text-sm font-medium" style={{ color: "var(--err)" }}>
          {error}
        </p>
      )}
    </form>
  );
}
