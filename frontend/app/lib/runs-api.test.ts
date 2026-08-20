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
  approveRun,
  canApprove,
  canReject,
  canResume,
  cleanRejectReason,
  fetchRuns,
  isLive,
  isTerminalState,
  REJECT_REASON_MAX,
  REJECT_REASON_MIN,
  rejectRun,
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
   * `partial`'s rule pointed the other way, and the reason `rejected` is a branch rather
   * than a fall-through.
   *
   * A person deciding "no" is neither a fault nor a shortfall. `err` would tell the owner
   * the machine broke; `warn` would tell them it fell short. Both blame the agent for a
   * call a human made — which is the exact misattribution `partial`'s colour exists to
   * avoid, and it is the whole reason `rejected` is its own state rather than `partial`.
   *
   * Written as four assertions on purpose. The `muted` one alone would keep passing if the
   * branch were deleted, because the default is also `muted` — so it would be a test that
   * cannot fail, and the negatives are what make it intent rather than accident.
   */
  it("paints a rejected run muted, and never in a fault or shortfall colour", () => {
    expect(runStateTone("rejected")).toBe("muted");
    expect(runStateTone("rejected")).not.toBe("err");
    expect(runStateTone("rejected")).not.toBe("warn");
    // Nor the review gate's colour: it is not waiting for anybody any more.
    expect(runStateTone("rejected")).not.toBe("accent");
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
   * `rejected` is terminal in the plainest sense: no route moves a rejected run, and the
   * recovery from a rejection is a NEW run. Left out, a list would poll it forever and the
   * run timeline would hold an event stream open for the full MAX_STREAM_SECONDS waiting
   * for events that cannot come.
   */
  it("treats a rejected run as finished, so nothing polls or streams it", () => {
    expect(isLive(run({ state: "rejected" }))).toBe(false);
    expect(isTerminalState("rejected")).toBe(true);
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

describe("canApprove", () => {
  /**
   * The approve endpoint accepts exactly one state and answers every other with a 409, so
   * this predicate is what stops the screen offering a control that cannot ever work.
   */
  it("accepts awaiting_approval and nothing else", () => {
    expect(canApprove("awaiting_approval")).toBe(true);
    for (const state of ["queued", "running", "done", "failed", "partial"]) {
      expect(canApprove(state)).toBe(false);
    }
  });

  it("refuses a state it has never heard of rather than assuming it is approvable", () => {
    // The server owns this vocabulary. Guessing "unknown means fine" would offer the
    // decision on a run whose state we cannot reason about at all.
    expect(canApprove("rejected")).toBe(false);
  });
});

describe("canReject", () => {
  /**
   * The same one state as `canApprove` today, and a separate function on purpose: the two
   * endpoints already differ (reject has no `no_checkpoint` refusal), so the seam is where
   * it needs to be before it is needed.
   */
  it("accepts awaiting_approval and nothing else", () => {
    expect(canReject("awaiting_approval")).toBe(true);
    for (const state of ["queued", "running", "done", "failed", "partial"]) {
      expect(canReject(state)).toBe(false);
    }
  });

  /**
   * A second reject is a 409, not a silent no-op — so a rejected run must not be offered
   * the control again. This is the assertion that stops the new state being the one case
   * the predicate quietly says yes to.
   */
  it("refuses a run that has already been rejected", () => {
    expect(canReject("rejected")).toBe(false);
  });
});

describe("the rejection reason bounds", () => {
  /**
   * Mirrored from `REJECT_REASON_MIN`/`REJECT_REASON_MAX` in `backend/app/api/runs.py`. If
   * these drift the client promises a person something the API then refuses with a 422 —
   * the exact round trip mirroring them exists to prevent.
   */
  it("matches the API's own numbers", () => {
    expect(REJECT_REASON_MIN).toBe(10);
    // 240 and not the column's 255, because `clamp_reason` truncates: silently shortening
    // a person's stated reason is not a cosmetic loss.
    expect(REJECT_REASON_MAX).toBe(240);
  });

  /**
   * The API collapses whitespace BEFORE applying the bounds. A client that measured the raw
   * string would accept a field full of spaces as a ten-character reason.
   */
  it("collapses whitespace the way the API does", () => {
    expect(cleanRejectReason("  the   draft \n\n claims  too much  ")).toBe(
      "the draft claims too much",
    );
    expect(cleanRejectReason("   ")).toBe("");
    expect(cleanRejectReason("\n\t ")).toBe("");
    // Ten spaces are not a ten-character reason.
    expect(cleanRejectReason("          ".repeat(3)).length).toBe(0);
  });
});

describe("rejectRun", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubStored(finishedReason: string | null) {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({ runId: "r1", state: "rejected", finishedReason }),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  /**
   * The mirror of `approveRun`'s "no approver" assertion. A rejection authorises nothing
   * and sends nothing, so no rejecter is recorded — and a body carrying one would be the
   * client inventing an actor the API has deliberately no column for.
   */
  it("sends a reason and nothing else", async () => {
    const fetchMock = stubStored("The tone is wrong for our customers.");

    await rejectRun("r1", "The  tone is   wrong for our customers");

    const call = fetchMock.mock.calls[0];
    expect(call).toBeDefined();
    expect(call?.[1]?.method).toBe("POST");
    // Whitespace collapsed, so the string that was measured is the string that is sent.
    expect(call?.[1]?.body).toBe(
      JSON.stringify({ reason: "The tone is wrong for our customers" }),
    );
    const sent = JSON.stringify(call?.[1] ?? {});
    expect(sent).not.toContain("rejecter");
    expect(sent).not.toContain("rejectedBy");
    expect(sent).not.toContain("approver");
  });

  /** Same trap as approve and resume: an unencoded id addresses a different route. */
  it("encodes the run id into the path", async () => {
    const fetchMock = stubStored("A reason long enough to be accepted.");

    await rejectRun("a/b", "A reason long enough to be accepted");

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/api/v1/runs/a%2Fb/reject");
  });

  /**
   * 200, state `rejected`, and the STORED reason. The third field is the point of the
   * response having its own model: the screen renders what was persisted rather than what
   * it typed, and those differ.
   */
  it("passes back the stored reason rather than the one it sent", async () => {
    stubStored("Collapsed and stored by the API.");

    expect(await rejectRun("r1", "Collapsed   and stored by the API")).toEqual({
      runId: "r1",
      state: "rejected",
      finishedReason: "Collapsed and stored by the API.",
    });
  });
});

describe("approveRun", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * The assertion this whole route's contract turns on: **the client sends NO approver.**
   *
   * The approver is the authenticated user, resolved server-side from the session, and it
   * is persisted on every `actions` row as the answer to "who authorised this post". A
   * body carrying an approver would be the client making an authorisation decision, and
   * the failure would be invisible — the request would succeed and the audit ledger would
   * record whatever the browser claimed.
   */
  it("POSTs with no body at all, so no approver can be client-supplied", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 202,
        json: () => Promise.resolve({ runId: "r1", state: "running" }),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await approveRun("r1");

    const call = fetchMock.mock.calls[0];
    expect(call).toBeDefined();
    expect(call?.[1]?.method).toBe("POST");
    // Not merely "no `approver` key": no body whatsoever, which is the only version of
    // this that cannot drift back into sending one.
    expect(call?.[1]?.body).toBeUndefined();
    expect(JSON.stringify(call?.[1] ?? {})).not.toContain("approver");
  });

  it("encodes the run id into the path", async () => {
    // Same trap as resume: an unencoded id does not fail loudly, it addresses a different
    // route and approves nothing.
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 202,
        json: () => Promise.resolve({ runId: "a/b", state: "running" }),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await approveRun("a/b");

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/api/v1/runs/a%2Fb/approve");
  });

  /** 202, and the state it answers with is `running` — never `done`. */
  it("passes back the state the API reports rather than assuming success means finished", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 202,
          json: () => Promise.resolve({ runId: "r1", state: "running" }),
        } as unknown as Response),
      ),
    );

    expect(await approveRun("r1")).toEqual({ runId: "r1", state: "running" });
  });
});
