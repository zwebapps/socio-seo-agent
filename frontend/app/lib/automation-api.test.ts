/**
 * The automation panel's derived answers.
 *
 * Three of these carry real weight. `isOverdue` is the difference between a screen that
 * says "next run Thursday 06:00" and one that admits nothing is picking the run up —
 * which, with no scheduler process running, is the honest answer. `scheduleSummary`
 * must not describe a schedule that will not run. And `toDraft` must carry exactly the
 * fields `PUT` accepts, because that route is a full replacement: a field it drops is a
 * setting silently reverted to a default on the next save.
 */

import { describe, expect, it } from "vitest";

import {
  type Automation,
  cadenceLabel,
  channelLabel,
  hourLabel,
  isOverdue,
  nextRunLabel,
  scheduleSummary,
  toDraft,
} from "@/app/lib/automation-api";

const BASE: Automation = {
  businessId: "11111111-1111-4111-8111-111111111111",
  configured: true,
  enabled: true,
  mode: "scheduled_draft",
  cadence: "weekly",
  dayOfWeek: 3,
  hour: 8,
  timezone: "Europe/Berlin",
  channels: ["linkedin"],
  goalTemplate: "more local enquiries",
  nextRunAt: "2026-08-27T06:00:00Z",
  lastRunAt: null,
  pausedReason: null,
  knownChannels: ["linkedin", "facebook"],
  knownCadences: ["weekly", "biweekly", "monthly"],
  maxGoalLength: 500,
  pollIntervalSeconds: 60,
  editableFields: ["enabled"],
};

function at(iso: string): Date {
  return new Date(iso);
}

describe("isOverdue", () => {
  it("is false before the slot", () => {
    expect(isOverdue(BASE, at("2026-08-27T05:00:00Z"))).toBe(false);
  });

  it("is false just after it, because the worker gets a poll interval to claim it", () => {
    // Four minutes past, on a 60s interval: a run being picked up right now must not be
    // reported as a broken scheduler.
    expect(isOverdue(BASE, at("2026-08-27T06:04:00Z"))).toBe(false);
  });

  it("is true once the grace has passed", () => {
    // Five intervals is the grace, so six minutes past is late. The worker advances
    // `nextRunAt` BEFORE starting a run, so a timestamp still sitting here means
    // nothing claimed it.
    expect(isOverdue(BASE, at("2026-08-27T06:06:00Z"))).toBe(true);
  });

  it("is false while automation is off, however stale the timestamp", () => {
    expect(isOverdue({ ...BASE, enabled: false }, at("2027-01-01T00:00:00Z"))).toBe(false);
  });

  it("is false for a paused automation, whose stale timestamp is deliberate", () => {
    // `enabled` is false whenever `pausedReason` is set — the pause is already on
    // screen and explains itself, so a second alarm about it would be noise.
    const paused = { ...BASE, enabled: false, pausedReason: "ceiling used up" };
    expect(isOverdue(paused, at("2027-01-01T00:00:00Z"))).toBe(false);
  });

  it("is false when there is no slot at all", () => {
    expect(isOverdue({ ...BASE, nextRunAt: null }, at("2027-01-01T00:00:00Z"))).toBe(false);
  });

  it("is false for an unparseable timestamp rather than throwing", () => {
    // A screen is not the place to discover a malformed date: the panel still has a
    // schedule to render.
    expect(isOverdue({ ...BASE, nextRunAt: "not a date" }, at("2027-01-01T00:00:00Z"))).toBe(
      false,
    );
  });

  it("scales with the interval the server reports", () => {
    // The grace is derived rather than hardcoded, so a slower worker does not read as
    // permanently overdue.
    const slow = { ...BASE, pollIntervalSeconds: 600 };
    expect(isOverdue(slow, at("2026-08-27T06:30:00Z"))).toBe(false);
    expect(isOverdue(slow, at("2026-08-27T07:00:00Z"))).toBe(true);
  });
});

describe("scheduleSummary", () => {
  it("says nothing is scheduled when automation is off", () => {
    // A stored cadence is not a promise. Describing one would be the panel asserting
    // work that will not happen.
    expect(scheduleSummary({ ...BASE, enabled: false })).toBe("Nothing is scheduled.");
  });

  it("names the cadence, the local slot and the next run", () => {
    const summary = scheduleSummary(BASE);
    expect(summary).toContain("every week");
    expect(summary).toContain("Thursday at 08:00 Europe/Berlin");
    expect(summary).toContain("Next run");
  });

  it("still describes the slot when the server sent no next run", () => {
    const summary = scheduleSummary({ ...BASE, nextRunAt: null });
    expect(summary).toContain("Thursday at 08:00");
    expect(summary).not.toContain("Next run");
  });
});

describe("nextRunLabel", () => {
  it("is null when there is nothing scheduled", () => {
    expect(nextRunLabel({ ...BASE, nextRunAt: null })).toBeNull();
  });

  it("is null rather than 'Invalid Date' for a malformed instant", () => {
    expect(nextRunLabel({ ...BASE, nextRunAt: "tomorrow-ish" })).toBeNull();
  });

  it("humanises the instant instead of echoing the ISO string", () => {
    // Not asserted against a fixed string: the label is rendered in the READER's zone by
    // design, so pinning one would only assert the test runner's timezone. What IS
    // asserted is that it stopped being machine text — an ISO string leaking onto the
    // panel is the failure this function exists to prevent.
    const label = nextRunLabel(BASE);
    expect(label).toBeTruthy();
    expect(label).not.toContain("T06:00:00Z");
    expect(label).toMatch(/\d/);
  });
});

describe("toDraft", () => {
  it("carries exactly the fields the API accepts, and no more", () => {
    // PUT is a full replacement. A field missing here is one reverted to its default on
    // the next save; a read-only field present here is one the API would ignore, which
    // is the behaviour a form must never have.
    expect(Object.keys(toDraft(BASE)).sort()).toEqual([
      "cadence",
      "channels",
      "dayOfWeek",
      "enabled",
      "goalTemplate",
      "hour",
      "timezone",
    ]);
  });

  it("copies the channel list rather than aliasing it", () => {
    // The draft is edited in place by the form; mutating the loaded setting's array
    // would make "discard changes" discard nothing.
    const draft = toDraft(BASE);
    draft.channels.push("facebook");
    expect(BASE.channels).toEqual(["linkedin"]);
  });
});

describe("labels", () => {
  it("renders channel names as a person writes them", () => {
    expect(channelLabel("linkedin")).toBe("LinkedIn");
    expect(channelLabel("blog_article")).toBe("Blog article");
  });

  it("falls back to the raw name for a channel it has never heard of", () => {
    // The server owns the vocabulary, so an unknown value here means the API grew a
    // channel — which must render as itself rather than as blank.
    expect(channelLabel("threads")).toBe("threads");
  });

  it("says what a cadence means, not what it is called", () => {
    expect(cadenceLabel("biweekly")).toBe("Every other week");
    expect(cadenceLabel("fortnightly")).toBe("fortnightly");
  });

  it("pads the hour, because 8:00 and 08:00 in one column read as different times", () => {
    expect(hourLabel(8)).toBe("08:00");
    expect(hourLabel(17)).toBe("17:00");
  });
});
