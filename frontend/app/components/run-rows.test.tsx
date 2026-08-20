/**
 * The runs list: what a row must say, when the list is allowed to poll, and what happens
 * when the resume endpoint refuses.
 *
 * Every test here is aimed at a failure that looks fine on screen. A dropped
 * `finishedReason` renders a tidy "partial" pill. A poll that never stops renders a
 * perfectly correct list while sending a request every five seconds forever. A duplicated
 * run renders as the list inventing runs. A swallowed refusal renders as a generic error
 * for a safeguard that worked.
 *
 * The API is faked at `fetch`, one level below `runs-api.ts`, so `request`'s own unpacking
 * of the API's `{detail: {code, message}}` shape is exercised too — that unpacking is
 * where "[object Object]" comes from when it is wrong.
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RunRows, useRuns } from "@/app/components/run-rows";
import type { RunSummary } from "@/app/lib/runs-api";

/** The list's own poll interval, from the module under test. */
const POLL_MS = 5000;

type Reply = { ok?: boolean; status?: number; body: unknown };

function response(reply: Reply): Response {
  return {
    ok: reply.ok ?? true,
    status: reply.status ?? 200,
    json: () => Promise.resolve(reply.body),
  } as unknown as Response;
}

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

function page(runs: RunSummary[], nextCursor: string | null = null): Reply {
  return { body: { runs, nextCursor } };
}

/* ------------------------------------------------------------------------- */

/** Replies for `GET /runs`, consumed in order; the last one repeats for every poll. */
let listReplies: Reply[] = [];
let listCalls = 0;
/** The reply for `POST /runs/{id}/resume`. */
let resumeReply: Reply = { body: { runId: "r1", state: "running" } };
let resumeCalls = 0;

beforeEach(() => {
  listReplies = [page([])];
  listCalls = 0;
  resumeCalls = 0;
  resumeReply = { body: { runId: "r1", state: "running" } };

  vi.stubGlobal(
    "fetch",
    vi.fn((input: string) => {
      if (String(input).includes("/resume")) {
        resumeCalls += 1;
        return Promise.resolve(response(resumeReply));
      }
      const index = Math.min(listCalls, listReplies.length - 1);
      listCalls += 1;
      const reply = listReplies[index];
      // A missing reply means the test set up fewer pages than the component asked for,
      // which is a broken test rather than a silent empty list.
      if (!reply) throw new Error("no list reply configured");
      return Promise.resolve(response(reply));
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/**
 * Let the in-flight fetch promises settle without advancing any timer.
 *
 * Microtask flushing rather than `waitFor`, because half these tests run on fake timers
 * and `waitFor` wants to advance them — which would fire a poll in the middle of an
 * assertion about how many polls have happened. The loop is generous on purpose: a resume
 * is click → state → fetch → state, and a flush that is one tick short makes a real test
 * look flaky.
 */
async function settle() {
  await act(async () => {
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

function Harness({ limit = 5 }: { limit?: number }) {
  const { state, reload, loadMore, canLoadMore, loadingMore } = useRuns(limit);
  return (
    <RunRows
      state={state}
      emptyNote="No runs yet."
      onLoadMore={() => void loadMore()}
      canLoadMore={canLoadMore}
      loadingMore={loadingMore}
      onResumed={() => void reload()}
    />
  );
}

/* ------------------------------------------------------------------------- */
/* A row has to account for a run that stopped short                          */
/* ------------------------------------------------------------------------- */

describe("a run that stopped short", () => {
  /**
   * The reason this component's docstring exists. "partial" is a word an owner cannot act
   * on; the sentence beside it is the difference between "this product is broken" and
   * "this deployment's credential cannot reach the mid tier". Dropping the field leaves a
   * row that looks completely healthy.
   */
  it("states why it stopped, not only that it did", async () => {
    listReplies = [
      page([
        run({
          state: "partial",
          currentNode: "OPPORTUNITY",
          finishedReason: "The configured credential cannot reach the mid tier.",
        }),
      ]),
    ];

    render(<Harness />);
    await settle();

    expect(screen.getByText("partial")).toBeInTheDocument();
    expect(
      screen.getByText("The configured credential cannot reach the mid tier."),
    ).toBeInTheDocument();
  });

  /**
   * `currentNode` carries three different facts depending on the state, and printing it
   * bare lets the reader take the wrong one — "OPPORTUNITY" on a partial run has to read
   * as where it GOT TO, and on an awaiting_approval run as where it is WAITING. Calling
   * the review gate "stopped" reports the product's one deliberate pause as a fault.
   */
  it("labels the node by what it means for that state", async () => {
    listReplies = [
      page([
        run({ runId: "a", state: "partial", currentNode: "OPPORTUNITY" }),
        run({ runId: "b", state: "awaiting_approval", currentNode: "REVIEW" }),
        run({ runId: "c", state: "running", currentNode: "HARVEST" }),
      ]),
    ];

    render(<Harness />);
    await settle();

    expect(screen.getByText("stopped at OPPORTUNITY")).toBeInTheDocument();
    expect(screen.getByText("waiting at REVIEW")).toBeInTheDocument();
    expect(screen.getByText("HARVEST")).toBeInTheDocument();
  });

  /**
   * A rejected run needs a THIRD verb, because both existing ones lie about it: "waiting
   * at REVIEW" says a decision is still pending when one has been made, and "stopped at
   * REVIEW" says the machine gave up on a run that in fact finished its work and had its
   * output refused by a person. Both are `string`-typed fall-throughs, so getting this
   * wrong is silent.
   */
  it("gives a rejected run its own verb rather than borrowing either existing one", async () => {
    listReplies = [
      page([
        run({ runId: "a", state: "rejected", currentNode: "REVIEW" }),
        run({ runId: "b", state: "partial", currentNode: "OPPORTUNITY" }),
      ]),
    ];

    render(<Harness />);
    await settle();

    expect(screen.getByText("rejected at REVIEW")).toBeInTheDocument();
    // Not the review gate's pending verb, and not the machine's giving-up verb.
    expect(screen.queryByText("waiting at REVIEW")).toBeNull();
    expect(screen.queryByText("stopped at REVIEW")).toBeNull();
    // And the caption for a genuine shortfall is untouched.
    expect(screen.getByText("stopped at OPPORTUNITY")).toBeInTheDocument();
  });

  /**
   * The register, which is the half of this that is easy to ship wrong.
   *
   * `finishedReason` on a `partial` run is the machine reporting a shortfall, and `warn` is
   * right for it. On a `rejected` run the SAME field holds a person's own sentence about
   * work they refused — and painting that in a warning colour reports their decision as a
   * fault of the agent. Asserted as a comparison between the two rows rather than as an
   * absolute, so it stays a statement about the DIFFERENCE and cannot pass by both rows
   * happening to be muted.
   */
  it("does not present a rejected run's reason in the same register as a shortfall", async () => {
    listReplies = [
      page([
        run({
          runId: "a",
          state: "rejected",
          currentNode: "REVIEW",
          finishedReason: "Two claims in the draft are ones we are not allowed to make.",
        }),
        run({
          runId: "b",
          state: "partial",
          currentNode: "OPPORTUNITY",
          finishedReason: "The configured credential cannot reach the mid tier.",
        }),
      ]),
    ];

    render(<Harness />);
    await settle();

    const refused = screen.getByText(
      "Two claims in the draft are ones we are not allowed to make.",
    );
    const fellShort = screen.getByText("The configured credential cannot reach the mid tier.");

    // The machine falling short keeps the warning colour...
    expect(fellShort.getAttribute("style") ?? "").toContain("--warn");
    // ...and a person's decision does not get it.
    expect(refused.getAttribute("style") ?? "").not.toContain("--warn");
    expect(refused.getAttribute("style") ?? "").not.toContain("--err");
    // The reason itself is still shown: a rejection whose reason is hidden is a decision
    // with no record on the one screen that lists runs.
    expect(refused).toBeInTheDocument();
  });

  /**
   * The mirror of the test above, and it has to be written as "invents nothing" rather
   * than "the fixture's string is absent" — a fixture string that was never supplied is
   * absent no matter what the component does, which is a test that could not fail. What
   * this catches is a `finishedReason ?? "Stopped for an unknown reason"` fallback: a
   * sentence the API never sent, on a run that finished perfectly well.
   */
  it("invents no reason for a run that did not stop short", async () => {
    listReplies = [page([run({ state: "done", goal: "Rank for boiler service" })])];

    render(<Harness />);
    await settle();

    const row = screen.getByRole("link", { name: /Rank for boiler service/ });
    expect(row).toHaveTextContent("done");
    expect(row.textContent ?? "").not.toMatch(/reason|unknown|stopped|failed/i);
  });
});

/* ------------------------------------------------------------------------- */
/* Polling: armed by the data, and disarmed by it                             */
/* ------------------------------------------------------------------------- */

describe("polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  /**
   * A dashboard left open on an account whose runs have all finished must not hold a timer
   * and a request every five seconds forever. Nothing on screen would ever look wrong;
   * the cost shows up as API load nobody can attribute.
   */
  it("never starts when every run is already terminal", async () => {
    // `rejected` belongs in this list for the plainest reason: nothing will ever move a
    // rejected run, so a list containing one must not arm a timer on its behalf.
    listReplies = [
      page([
        run({ state: "done" }),
        run({ runId: "r2", state: "partial" }),
        run({ runId: "r3", state: "rejected" }),
      ]),
    ];

    render(<Harness />);
    await settle();
    expect(listCalls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 4);
    });

    expect(listCalls).toBe(1);
  });

  /**
   * The other half, and the one that has to keep working: a list with a live run must not
   * go stale, and the poll that sees the last run reach a terminal state must be the last
   * poll. Asserting the count STOPS GROWING is the whole test — a version that only
   * checked "it polled at least once" would pass against a timer that runs forever.
   */
  it("polls while a run is live and stops once it finishes", async () => {
    listReplies = [
      page([run({ state: "running" })]),
      page([run({ state: "running" })]),
      // From here on the run is done, so this reply repeats for any further poll.
      page([run({ state: "done" })]),
    ];

    render(<Harness />);
    await settle();
    expect(listCalls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(listCalls).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(listCalls).toBe(3);
    expect(screen.getByText("done")).toBeInTheDocument();

    // The run is terminal now. Nothing may schedule another read.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 5);
    });
    expect(listCalls).toBe(3);
  });
});

/* ------------------------------------------------------------------------- */
/* Paging                                                                     */
/* ------------------------------------------------------------------------- */

describe("older runs", () => {
  it("offers no control when the API says there is no next page", async () => {
    listReplies = [page([run()], null)];

    render(<Harness />);
    await settle();

    // A control that fetches nothing reads as the list being broken.
    expect(screen.queryByRole("button", { name: "Load older runs" })).not.toBeInTheDocument();
  });

  it("offers the control only when the API says there is another page", async () => {
    listReplies = [page([run()], "cursor-1")];

    render(<Harness />);
    await settle();

    expect(screen.getByRole("button", { name: "Load older runs" })).toBeInTheDocument();
  });

  /**
   * The dedupe. A run can legitimately be on both pages: it was page two when it was
   * fetched, then a newer run appeared and pushed the boundary, so the refreshed first
   * page now contains it too. Rendering it twice reads as the list inventing runs — and
   * an owner who sees two of yesterday's run has no way to tell which is real.
   *
   * The sequence below is the real one: first page, append page two, then a poll refreshes
   * the first page with the older run now included.
   */
  it("does not show a run twice when a refreshed first page overlaps an appended page", async () => {
    const live = run({ runId: "a", state: "running", goal: "Rank for emergency plumber" });
    const older = run({ runId: "c", state: "done", goal: "Rank for boiler service" });

    listReplies = [
      page([live, run({ runId: "b", goal: "Rank for drain cleaning" })], "cursor-1"),
      // The appended second page.
      page([older], null),
      // The poll: the older run has migrated onto the first page.
      page([live, run({ runId: "b", goal: "Rank for drain cleaning" }), older], null),
    ];

    // `fireEvent`, not `userEvent`, for this one test only. This is the single case that
    // needs a click AND a fake-timer poll in the same test, and user-event's own internal
    // delay runs on the timers it is being asked to fake — so the click never resolves.
    // The click is a plain click with no pointer sequence worth simulating.
    vi.useFakeTimers();

    render(<Harness />);
    await settle();

    fireEvent.click(screen.getByRole("button", { name: "Load older runs" }));
    await settle();
    expect(screen.getAllByText("Rank for boiler service")).toHaveLength(1);

    // Now let the poll bring the overlapping first page back.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });

    expect(listCalls).toBe(3);
    expect(screen.getAllByText("Rank for boiler service")).toHaveLength(1);
    expect(screen.getAllByText("Rank for emergency plumber")).toHaveLength(1);
  });
});

/* ------------------------------------------------------------------------- */
/* Resume                                                                     */
/* ------------------------------------------------------------------------- */

describe("the resume control", () => {
  function resumeButton(goal: string) {
    return screen.queryByRole("button", {
      name: `Resume the run for "${goal}" from its checkpoint`,
    });
  }

  it("appears only for a run the endpoint would accept", async () => {
    listReplies = [
      page([
        run({ runId: "a", state: "running", goal: "live one" }),
        run({ runId: "b", state: "queued", goal: "queued one" }),
        run({ runId: "c", state: "done", goal: "finished one" }),
        run({ runId: "d", state: "partial", goal: "partial one" }),
        run({ runId: "e", state: "awaiting_approval", goal: "parked one" }),
      ]),
    ];

    render(<Harness />);
    await settle();

    expect(resumeButton("live one")).toBeInTheDocument();
    expect(resumeButton("queued one")).toBeInTheDocument();
    // Re-running a finished run would spend money to overwrite work somebody approved,
    // and resuming a parked one would step past the review gate.
    expect(resumeButton("finished one")).not.toBeInTheDocument();
    expect(resumeButton("partial one")).not.toBeInTheDocument();
    expect(resumeButton("parked one")).not.toBeInTheDocument();
  });

  /**
   * The refusal is the point of the feature, not an error to hide.
   *
   * A run genuinely executing right now and a run abandoned by a dead process both read
   * `running` from here — only the executor can tell them apart. So the button is offered,
   * the API refuses, and what it said is printed. "This run is already executing" is a
   * useful answer; replacing it with "Could not resume this run" turns a working safeguard
   * into a mystery, and the screen looks identical either way.
   */
  it("shows the API's own refusal verbatim rather than a generic error", async () => {
    listReplies = [page([run({ runId: "a", state: "running", goal: "live one" })])];
    resumeReply = {
      ok: false,
      status: 409,
      body: {
        detail: {
          code: "run_already_executing",
          message: "This run is already executing. Wait for it to finish or fail.",
        },
      },
    };

    const user = userEvent.setup();
    render(<Harness />);
    await settle();

    const button = resumeButton("live one");
    expect(button).not.toBeNull();
    await user.click(button!);
    await settle();

    expect(resumeCalls).toBe(1);
    expect(
      screen.getByText("This run is already executing. Wait for it to finish or fail."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Could not resume this run.")).not.toBeInTheDocument();
  });

  it("confirms a successful resume and refreshes the list", async () => {
    listReplies = [page([run({ runId: "a", state: "running", goal: "live one" })])];

    const user = userEvent.setup();
    render(<Harness />);
    await settle();
    const before = listCalls;

    const button = resumeButton("live one");
    expect(button).not.toBeNull();
    await user.click(button!);
    await settle();

    expect(screen.getByText("Picked back up from its checkpoint.")).toBeInTheDocument();
    // The list is re-read, or the row keeps showing the state from before the resume.
    expect(listCalls).toBeGreaterThan(before);
  });
});

/* ------------------------------------------------------------------------- */
/* Failure and emptiness                                                      */
/* ------------------------------------------------------------------------- */

describe("when there is nothing to show", () => {
  it("uses the caller's empty note rather than an invented one", async () => {
    listReplies = [page([])];

    render(<Harness />);
    await settle();

    expect(screen.getByText("No runs yet.")).toBeInTheDocument();
  });

  /**
   * A 409 `no_business` is not a broken screen — it is an account that has not finished
   * onboarding, and the API's message says exactly that. Swapping it for "Could not load
   * your runs" sends the owner looking for a bug instead of to the onboarding form.
   */
  it("passes an API refusal through instead of replacing it", async () => {
    listReplies = [
      {
        ok: false,
        status: 409,
        body: {
          detail: {
            code: "no_business",
            message: "Finish onboarding before starting a run.",
          },
        },
      },
    ];

    render(<Harness />);
    await settle();

    expect(screen.getByText("Finish onboarding before starting a run.")).toBeInTheDocument();
    expect(screen.queryByText("Could not load your runs.")).not.toBeInTheDocument();
  });
});
