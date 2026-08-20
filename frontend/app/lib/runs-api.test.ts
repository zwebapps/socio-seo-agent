/**
 * The run-state vocabulary, and what each screen is allowed to conclude from it.
 *
 * These four functions are tiny and are the reason three screens agree about what a run
 * means. The cases below are the ones where being wrong is INVISIBLE: a `partial` run
 * painted green looks like a success, an unrecognised state that throws takes the whole
 * list down, and a resume button offered on a finished run 409s every time.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ALL_RUNS,
  canResume,
  fetchRuns,
  isLive,
  resumeRun,
  runStateLabel,
  runStateTone,
  type RunSummary,
} from "./runs-api";

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    runId: "r1",
    goal: "Win more plumbing leads in Kaunas",
    state: "done",
    currentNode: null,
    resumedCount: 0,
    finishedReason: null,
    createdAt: "2026-08-20T09:00:00Z",
    ...over,
  };
}

describe("runStateTone", () => {
  /**
   * The single most load-bearing assertion in this file.
   *
   * A `partial` run produced something and did not finish. `ok` is the green pill, and a
   * green pill on a run that never reached GENERATE tells the owner they have a blog post
   * when they have nothing — after which they stop believing the rest of the screen. The
   * test is written as two assertions on purpose: the second one fails loudly if somebody
   * "simplifies" the tone map by folding `partial` in with `done`.
   */
  it("paints partial as a warning and never as a success", () => {
    expect(runStateTone("partial")).toBe("warn");
    expect(runStateTone("partial")).not.toBe("ok");
  });

  it("gives each state the product actually distinguishes its own tone", () => {
    expect(runStateTone("done")).toBe("ok");
    expect(runStateTone("failed")).toBe("err");
    // Not an error and not a success: a run parked at the review gate is the product's
    // one deliberate pause.
    expect(runStateTone("awaiting_approval")).toBe("accent");
  });

  /**
   * `state` is a `string` because the server owns the vocabulary. So the day the API adds
   * one, this function has to render it quietly rather than throw inside a `.map()` and
   * blank the list.
   */
  it("falls through to muted for a state it has never heard of", () => {
    expect(runStateTone("cancelled")).toBe("muted");
    expect(runStateTone("")).toBe("muted");
    expect(runStateTone("DONE")).toBe("muted");
  });
});

describe("runStateLabel", () => {
  it("replaces every underscore, not just the first", () => {
    expect(runStateLabel("awaiting_approval")).toBe("awaiting approval");
    expect(runStateLabel("a_b_c")).toBe("a b c");
  });

  it("leaves a state with no underscores alone", () => {
    expect(runStateLabel("partial")).toBe("partial");
  });
});

describe("isLive", () => {
  it("treats queued and running as live, so a list with one keeps polling", () => {
    expect(isLive(run({ state: "running" }))).toBe(true);
    expect(isLive(run({ state: "queued" }))).toBe(true);
  });

  it("treats every terminal state as not live, so an idle dashboard holds no timer", () => {
    for (const state of ["done", "failed", "partial"]) {
      expect(isLive(run({ state }))).toBe(false);
    }
  });

  /**
   * The one that is easy to get backwards. `awaiting_approval` is not finished — a person
   * is going to act on it — but nothing about it changes on its own, so polling it forever
   * is a request every five seconds that can never return anything new.
   */
  it("treats awaiting_approval as terminal for polling, because only a human moves it", () => {
    expect(isLive(run({ state: "awaiting_approval" }))).toBe(false);
  });

  /**
   * Deliberately the opposite default from `runStateTone`. An unknown state is far more
   * likely to be a new in-flight phase than a new terminal one, and the two failures are
   * not symmetrical: guessing "live" costs one extra poll, guessing "terminal" leaves a
   * running run frozen on screen until the reader reloads the page.
   */
  it("assumes an unrecognised state is still in flight", () => {
    expect(isLive(run({ state: "harvesting" }))).toBe(true);
  });
});

describe("canResume", () => {
  it("offers resume for the two states the endpoint accepts", () => {
    expect(canResume(run({ state: "running" }))).toBe(true);
    expect(canResume(run({ state: "queued" }))).toBe(true);
  });

  /**
   * Each `false` here mirrors a specific refusal in `resume_run`, and offering a button
   * that always 409s is worse than offering none. `awaiting_approval` matters most:
   * resuming it would step past the review gate the whole product is built around.
   */
  it("refuses the states the endpoint refuses, rather than 409-ing on click", () => {
    for (const state of ["done", "failed", "partial", "awaiting_approval"]) {
      expect(canResume(run({ state }))).toBe(false);
    }
  });

  it("does not offer resume for a state it has never heard of", () => {
    expect(canResume(run({ state: "cancelled" }))).toBe(false);
  });
});

/* ------------------------------------------------------------------------- */
/* The request shapes. Both of these have a silent-wrong-answer failure mode. */
/* ------------------------------------------------------------------------- */

describe("fetchRuns", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // The parameters are declared even though the body ignores them: it is what gives
  // `mock.calls` a real tuple type, so asserting on the URL type-checks.
  function stubOk() {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ runs: [], nextCursor: null }),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  function calledUrl(fetchMock: ReturnType<typeof stubOk>): string {
    const first = fetchMock.mock.calls[0];
    expect(first).toBeDefined();
    return String(first?.[0]);
  }

  /**
   * A `cursor=null` on the wire is the bug this catches: it is not "no cursor", it is a
   * keyset cursor with the literal text `null`, which the API either rejects or reads as
   * a position — and either way the first page is not what comes back.
   */
  it("omits the cursor entirely on the first page", async () => {
    const fetchMock = stubOk();
    await fetchRuns(ALL_RUNS, null);
    expect(calledUrl(fetchMock)).not.toContain("cursor");
  });

  it("sends and encodes a cursor when there is one", async () => {
    const fetchMock = stubOk();
    await fetchRuns(5, "2026-08-20T09:00:00Z|r1");
    const url = calledUrl(fetchMock);
    expect(url).toContain("limit=5");
    // `|` and `:` must survive as an encoded cursor, not as raw URL punctuation.
    expect(url).toContain("cursor=2026-08-20T09%3A00%3A00Z%7Cr1");
  });
});

describe("resumeRun", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * The run id goes into the PATH, so an unencoded one does not fail loudly — it resolves
   * to a different, existing-looking route and resumes nothing.
   */
  it("encodes the run id into the path and POSTs", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ runId: "a/b", state: "running" }),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await resumeRun("a/b");

    const call = fetchMock.mock.calls[0];
    expect(call).toBeDefined();
    expect(String(call?.[0])).toContain("/api/v1/runs/a%2Fb/resume");
    expect(call?.[1]?.method).toBe("POST");
  });
});
