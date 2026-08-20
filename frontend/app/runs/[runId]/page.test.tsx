/**
 * The run timeline for a run that a person ENDED.
 *
 * This screen had no test file at all, and a rejection is the case that most needs one:
 * `state` is a `string` everywhere, so a new terminal value does not break anything
 * loudly — it falls through every branch that was written for the states that existed, and
 * the result is a screen that quietly describes a decision as a fault, or holds a
 * connection open for a run nothing will ever move.
 *
 * Three claims are asserted here, each of which fails silently in production:
 *
 * - the `finishedReason` well names the HUMAN decision instead of heading a person's own
 *   sentence "Why it stopped" in the warning colour;
 * - the REVIEW step stops saying "Waiting for you" once the waiting is over;
 * - nothing polls and no event stream is opened, because a rejected run is terminal.
 *
 * The review tabs are asserted to survive, because the reject route deliberately leaves
 * `runs.checkpoint` intact so the refused draft is still readable.
 */

import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RunPage from "@/app/runs/[runId]/page";

const REASON = "Two claims in the draft are ones we are not allowed to make.";

/** The run as the API reports it after a rejection: terminal, with the stored reason. */
function rejectedRun() {
  return {
    runId: "r1",
    goal: "Win more plumbing leads in Kaunas",
    state: "rejected",
    currentNode: "REVIEW",
    resumedCount: 0,
    finishedReason: REASON,
    events: [
      { seq: 1, node: "INTAKE", status: "done", payload: {}, at: "2026-08-20T09:00:00Z" },
      { seq: 2, node: "REVIEW", status: "started", payload: {}, at: "2026-08-20T09:04:00Z" },
    ],
  };
}

/** An empty review payload; this file is about the timeline, not the tabs' contents. */
const REVIEW_BODY = {
  hasOutput: false,
  draft: null,
  draftNote: "GENERATE has not completed for this run.",
  seo: null,
  seoNote: "VALIDATE has not scored a draft for this run.",
  social: [],
  socialNote: "REPACK has not written posts for this run.",
  aiBlocks: null,
  aiBlocksNote: "GENERATE has not produced answer blocks for this run.",
  retrieval: [],
  retrievalNote: "No document retrieval ran for this run.",
  published: null,
  publishedNote: "Nothing has been published yet.",
  measurement: null,
  measurementNote: "Nothing has been measured yet.",
  opportunity: null,
  factGaps: [],
  errors: [],
};

let fetchMock: ReturnType<typeof vi.fn>;
/** Constructed EventSource urls. A rejected run must produce none. */
let streams: string[] = [];
let intervals = 0;

beforeEach(() => {
  streams = [];
  intervals = 0;

  fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/review") ? REVIEW_BODY : rejectedRun();
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(body),
    } as unknown as Response);
  });
  vi.stubGlobal("fetch", fetchMock);

  // jsdom has no EventSource. Stubbing it rather than letting the `try` throw is what
  // makes "no stream was opened" an assertion instead of an accident of the environment.
  vi.stubGlobal(
    "EventSource",
    class {
      constructor(url: string) {
        streams.push(url);
      }
      addEventListener() {}
      close() {}
      onerror: (() => void) | null = null;
    },
  );

  const realSetInterval = window.setInterval;
  vi.stubGlobal("setInterval", ((...args: Parameters<typeof window.setInterval>) => {
    intervals += 1;
    return realSetInterval(...args);
  }) as typeof window.setInterval);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function mount() {
  render(<RunPage params={Promise.resolve({ runId: "r1" })} />);
  await act(async () => {
    for (let i = 0; i < 12; i += 1) await Promise.resolve();
  });
}

describe("a rejected run's timeline", () => {
  /**
   * The heading is the whole difference between "the agent fell short" and "you decided".
   * `finishedReason` is one column carrying both, and the machine's version of it earns
   * `warn` — a person's does not.
   */
  it("heads the reason as the reviewer's decision, not as a stoppage", async () => {
    await mount();

    const heading = screen.getByText(/why you rejected it/i);
    expect(heading).toBeInTheDocument();
    expect(screen.queryByText(/why it stopped/i)).toBeNull();
    // And it is not painted in the register a failed or partial run's reason is.
    expect(heading.getAttribute("style") ?? "").not.toContain("--warn");
    expect(heading.getAttribute("style") ?? "").not.toContain("--err");
    // The reason itself is shown: it is the entire record of the decision.
    expect(screen.getByText(REASON)).toBeInTheDocument();
  });

  /**
   * The fourth place a new terminal state leaks. "Waiting for you" on a run the reader has
   * already decided says the decision is still theirs to make.
   */
  it("stops describing REVIEW as waiting once the decision is made", async () => {
    await mount();

    expect(screen.getByText(/you rejected the output/i)).toBeInTheDocument();
    expect(screen.queryByText("Waiting for you")).toBeNull();
    // Other nodes keep their own labels.
    expect(screen.getByText("Understanding the request")).toBeInTheDocument();
  });

  /**
   * Nothing will ever move a rejected run, so a stream here would hold a connection open
   * for the full MAX_STREAM_SECONDS and a poller would re-read a finished run forever.
   * Neither failure is visible on screen, which is why it is asserted rather than assumed.
   */
  it("opens no event stream and starts no poller", async () => {
    await mount();

    expect(streams).toEqual([]);
    expect(intervals).toBe(0);
  });

  /**
   * The checkpoint survives a rejection on purpose. A refused draft is still evidence of
   * work the owner paid for, and hiding it would withhold it at exactly the moment somebody
   * wants to check what they turned down.
   */
  it("still mounts the review tabs, so the refused output stays readable", async () => {
    await mount();

    expect(screen.getByRole("tab", { name: /Draft/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Delivery/ })).toBeInTheDocument();
  });

  /** The decision is made, so the card that offers it must be gone entirely. */
  it("offers neither decision control any more", async () => {
    await mount();

    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject/i })).toBeNull();
  });
});
