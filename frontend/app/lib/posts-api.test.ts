/**
 * The calendar's arithmetic, and the tone rules that keep it readable.
 *
 * `monthGrid` is tested because an off-by-one puts every post on the wrong weekday,
 * which looks like a scheduling bug rather than a grid bug — and `getDay()` returning 0
 * for Sunday is exactly the off-by-one waiting to happen in a Monday-first calendar.
 */

import { describe, expect, it } from "vitest";

import { dayKey, monthGrid, platformLabel, statusTone } from "@/app/lib/posts-api";

describe("monthGrid", () => {
  it("always returns six whole weeks", () => {
    // Fixed height on purpose: a grid that changes rows as you page makes the panel
    // jump, and a 31-day month starting on a Sunday genuinely needs six rows.
    for (const month of [0, 1, 5, 11]) {
      expect(monthGrid(2026, month)).toHaveLength(42);
    }
  });

  it("starts on a Monday", () => {
    // The product's locale default is `de`, and a German calendar starts on Monday.
    const [first] = monthGrid(2026, 8);
    expect(first).toBeDefined();
    expect(first!.getUTCDay()).toBe(1);
  });

  it("leads with the tail of the previous month when the 1st is not a Monday", () => {
    // 1 September 2026 is a Tuesday, so the grid opens on 31 August.
    const days = monthGrid(2026, 8);
    expect(dayKey(days[0]!)).toBe("2026-08-31");
    expect(dayKey(days[1]!)).toBe("2026-09-01");
  });

  it("does not pad when the 1st IS a Monday", () => {
    // 1 June 2026 is a Monday. A grid that always padded would push the month a week on.
    const days = monthGrid(2026, 5);
    expect(dayKey(days[0]!)).toBe("2026-06-01");
  });

  it("contains every day of the month exactly once", () => {
    const keys = monthGrid(2026, 1).map(dayKey);
    const february = keys.filter((key) => key.startsWith("2026-02"));
    expect(new Set(february).size).toBe(28);
  });

  it("crosses a year boundary without repeating a day", () => {
    const keys = monthGrid(2026, 11).map(dayKey);
    expect(new Set(keys).size).toBe(42);
    expect(keys.some((key) => key.startsWith("2027-01"))).toBe(true);
  });
});

describe("dayKey", () => {
  it("groups an ISO instant onto its UTC day", () => {
    expect(dayKey("2026-09-01T09:30:00.000Z")).toBe("2026-09-01");
  });

  it("does not shift the day for a late-evening UTC instant", () => {
    // The grid is built in UTC, so the key has to be too — mixing the two is how a post
    // lands one square to the left of where its date says.
    expect(dayKey("2026-09-01T23:59:00.000Z")).toBe("2026-09-01");
  });
});

describe("statusTone", () => {
  it("does not paint a refusal as an error", () => {
    // Every social publish is refused today, because posting for other people is gated
    // on App Review. Red for the ordinary state would make the screen look broken and
    // train the owner to ignore the colour that matters when something really fails.
    expect(statusTone("refused")).toBe("warn");
    expect(statusTone("failed")).toBe("err");
  });

  it("distinguishes published from scheduled from queued", () => {
    expect(statusTone("published")).toBe("ok");
    expect(statusTone("scheduled")).toBe("accent");
    expect(statusTone("queued")).toBe("muted");
  });
});

describe("platformLabel", () => {
  it("uses the words a person uses", () => {
    expect(platformLabel("linkedin")).toBe("LinkedIn");
    expect(platformLabel("link_hub")).toBe("Link hub");
  });

  it("falls back to the stored id for a channel it has no name for", () => {
    // Better than blank: an unnamed channel is still identifiable, and a blank pill on
    // the calendar is indistinguishable from a rendering bug.
    expect(platformLabel("mastodon")).toBe("mastodon");
  });
});
