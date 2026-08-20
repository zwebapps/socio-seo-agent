/**
 * The review screen's empty states.
 *
 * This screen has one rule its own docstring states three ways: **it never fills an empty
 * tab.** The whole claim of the product is that output is grounded in evidence, and this
 * is the one screen where the owner checks that claim — so a tab with nothing in it has to
 * say which node produces the thing and that the node has not run, and it must never show
 * a placeholder draft, a zero score, or a blank panel.
 *
 * A blank panel is the failure these tests exist for, because it is indistinguishable from
 * a rendering bug: the owner cannot tell "the agent produced nothing" from "the screen is
 * broken", and those need completely different responses from them.
 */

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RetrievalTrace, RunReview } from "@/app/lib/review-api";
import { ApproveGate, RunReviewTabs } from "@/app/runs/[runId]/review";

/** The notes the API actually sends, each naming the node responsible. */
const NOTES = {
  draft: "GENERATE has not completed for this run.",
  seo: "VALIDATE has not scored a draft for this run.",
  social: "REPACK has not written posts for this run.",
  ai: "GENERATE has not produced answer blocks for this run.",
  published:
    "Nothing has been published yet. EXPORT runs only after a human approves the run at " +
    "the review gate — approving it is what lets it publish.",
  measurement:
    "Nothing has been measured yet. MEASURE runs after EXPORT, so there is nothing to " +
    "measure until something has been published.",
  // Verbatim from `review_service._NO_RETRIEVAL`. The wording is the point of the test
  // that uses it: it names HARVEST, it says the run was normal, and it contains no word
  // for failure — because a business that uploaded nothing had nothing to retrieve.
  retrieval:
    "No document retrieval ran for this run. Retrieval reads the business's own uploaded " +
    "documents, and this business has none on record -- so the work was written from its " +
    "confirmed profile and the live research instead, which HARVEST records above under " +
    "what it was written without. That is a normal run: there was nothing to retrieve, so " +
    "nothing was retrieved.",
} as const;

function review(over: Partial<RunReview> = {}): RunReview {
  return {
    hasOutput: false,
    draft: null,
    draftNote: NOTES.draft,
    seo: null,
    seoNote: NOTES.seo,
    social: [],
    socialNote: NOTES.social,
    aiBlocks: null,
    aiBlocksNote: NOTES.ai,
    retrieval: [],
    retrievalNote: NOTES.retrieval,
    published: null,
    publishedNote: NOTES.published,
    measurement: null,
    measurementNote: NOTES.measurement,
    opportunity: null,
    factGaps: [],
    errors: [],
    ...over,
  };
}

let body: RunReview = review();

beforeEach(() => {
  body = review();
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      } as unknown as Response),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function settle() {
  await act(async () => {
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

async function mount() {
  render(<RunReviewTabs runId="r1" runState="partial" />);
  await settle();
}

/** The panel for one tab, which is the only place its emptiness can be judged. */
function panelFor(tabName: string | RegExp): HTMLElement {
  const tab = screen.getByRole("tab", { name: tabName });
  const id = tab.getAttribute("aria-controls");
  expect(id).toBeTruthy();
  const panel = document.getElementById(id!);
  expect(panel).not.toBeNull();
  return panel!;
}

/* ------------------------------------------------------------------------- */

describe("a run that produced nothing", () => {
  /**
   * The one an owner sees most often on this deployment: the run stopped at OPPORTUNITY,
   * so GENERATE never ran. The panel must name the node, because "nothing here" leaves the
   * owner unable to tell a stalled agent from a broken page.
   */
  it("names the node responsible in the draft tab instead of leaving it blank", async () => {
    await mount();

    const panel = panelFor("Draft");
    expect(panel).toHaveTextContent(NOTES.draft);
    // Which also proves the panel is not empty — a blank panel reads as a rendering bug.
    expect(panel.textContent?.trim().length ?? 0).toBeGreaterThan(0);
  });

  /**
   * The tabs are not interchangeable: each names a different node, so an owner can tell
   * "no draft yet" from "the draft exists but was never scored". One shared note would
   * lose that, and every panel would still look perfectly finished.
   */
  it("names a different node in every empty tab", async () => {
    const user = userEvent.setup();
    await mount();

    expect(panelFor("Draft")).toHaveTextContent(NOTES.draft);

    await user.click(screen.getByRole("tab", { name: /SEO findings/ }));
    expect(panelFor(/SEO findings/)).toHaveTextContent(NOTES.seo);

    await user.click(screen.getByRole("tab", { name: "Social" }));
    expect(panelFor("Social")).toHaveTextContent(NOTES.social);

    await user.click(screen.getByRole("tab", { name: /AI blocks/ }));
    expect(panelFor(/AI blocks/)).toHaveTextContent(NOTES.ai);
  });

  /**
   * The invented-content guard, written as an explicit negative because the failure it
   * catches is a helpful-looking one: a placeholder title, a sample post, a 0/100 score
   * card. Any of those would make a run that produced nothing look like a run that
   * produced something bad, which is a different and much more expensive conversation.
   */
  it("invents no draft, no score and no posts to fill the tabs", async () => {
    const user = userEvent.setup();
    await mount();

    const draft = panelFor("Draft");
    expect(draft).not.toHaveTextContent("Page title");
    expect(draft).not.toHaveTextContent("Meta description");
    expect(draft).not.toHaveTextContent("The page");

    await user.click(screen.getByRole("tab", { name: /SEO findings/ }));
    const seo = panelFor(/SEO findings/);
    expect(seo).not.toHaveTextContent("Score");
    expect(seo).not.toHaveTextContent("/100");
    expect(seo).not.toHaveTextContent("passed");
  });

  /** Said once at the top, so it is visible without opening a single tab. */
  it("says up front that nothing was produced", async () => {
    await mount();
    expect(screen.getByText("nothing produced yet")).toBeInTheDocument();
  });

  /**
   * The API is allowed to send no note — an older run, a node that never recorded one.
   * The panel still must not be blank, because blank is the state that reads as broken.
   */
  it("still says something when the API sends no note at all", async () => {
    body = review({ draftNote: null });
    await mount();

    expect(panelFor("Draft")).toHaveTextContent("There is nothing here yet.");
  });

  /**
   * An AI-blocks payload that exists but is empty is the subtle case: the object is
   * non-null, so a naive `if (!blocks)` check passes and the panel falls through to
   * rendering an empty list — a heading, a keyword pill, and nothing to read.
   */
  it("treats a present-but-empty AI blocks payload as nothing produced", async () => {
    body = review({
      hasOutput: true,
      aiBlocks: { targetKeyword: "emergency plumber kaunas", blocks: [], headings: [], cta: null },
    });
    const user = userEvent.setup();
    await mount();

    await user.click(screen.getByRole("tab", { name: /AI blocks/ }));
    expect(panelFor(/AI blocks/)).toHaveTextContent(NOTES.ai);
  });

  /**
   * Same shape one level down: VALIDATE ran and recorded no individual rule results. The
   * score card is real and stays; the findings area must say so rather than show two empty
   * sections that read as "nothing wrong here".
   */
  it("says so when the scorer recorded no rule results", async () => {
    body = review({
      hasOutput: true,
      seo: { score: 0, passed: false, findings: [], note: null },
    });
    const user = userEvent.setup();
    await mount();

    await user.click(screen.getByRole("tab", { name: /SEO findings/ }));
    expect(panelFor(/SEO findings/)).toHaveTextContent(
      "The scorer recorded no individual rule results for this run.",
    );
  });
});

describe("what the run was missing", () => {
  /**
   * Claims discipline applied to UI copy: the gaps and the node errors are shown ABOVE the
   * tabs, so "written without live research" is stated rather than implied. Hiding them
   * behind a click, or dropping them, is how a draft written blind gets read as a
   * researched one.
   */
  it("states the gaps and the node errors above the tabs", async () => {
    body = review({
      factGaps: ["No live search results: the research credential is not configured."],
      errors: [
        { node: "HARVEST", code: "provider_unavailable", message: "The search provider refused." },
      ],
    });
    await mount();

    expect(screen.getByText("What this was written without")).toBeInTheDocument();
    expect(
      screen.getByText("No live search results: the research credential is not configured."),
    ).toBeInTheDocument();
    expect(screen.getByText("The search provider refused.")).toBeInTheDocument();
    expect(screen.getByText("HARVEST")).toBeInTheDocument();
  });

  it("renders no honesty panel when there was nothing missing", async () => {
    await mount();
    expect(screen.queryByText("What this was written without")).not.toBeInTheDocument();
  });
});

describe("when the review itself cannot be loaded", () => {
  /**
   * A failed fetch must not look like a run that produced nothing — those are opposite
   * facts, and a silent empty screen would have the owner blaming the agent for a
   * transport failure.
   */
  it("shows the API's message rather than an empty set of tabs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 404,
          json: () =>
            Promise.resolve({ detail: { code: "no_run", message: "No such run for this business." } }),
        } as unknown as Response),
      ),
    );

    await mount();

    expect(screen.getByText("No such run for this business.")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Draft" })).not.toBeInTheDocument();
  });
});

describe("Delivery panel", () => {
  const simulatedLanding = {
    actionType: "publish.page",
    target: "landing_page",
    status: "succeeded",
    externalRef: "fake://publish.page/landing_page#8fe88144",
    error: null,
    simulated: true,
    summary: "publish.page → landing_page: done (SIMULATED — no credential configured)",
  };
  const refusedLinkedin = {
    actionType: "social.post",
    target: "linkedin",
    status: "refused",
    externalRef: null,
    error: "this business has no linkedin connection. Connect the account first.",
    simulated: true,
    summary: "social.post → linkedin: refused (this business has no linkedin connection)",
  };

  function delivered(over = {}) {
    return review({
      published: {
        note: "Published 1 of 3; nothing was published to facebook, linkedin -- SIMULATED",
        attempted: 3,
        succeeded: 1,
        simulated: true,
        notified: false,
        notifyNote: "Nobody was told: this business profile has no email address on record.",
        targets: [simulatedLanding, refusedLinkedin],
        ...over,
      },
    });
  }

  it("says the GATE is why nothing was published, not that something failed", async () => {
    // The normal state of every unapproved run. "EXPORT has not run" must not read as
    // "publishing is broken", because the reader's next action is completely different.
    body = review();
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    expect(screen.getByText(/approves the run at the review gate/i)).toBeInTheDocument();
  });

  it("never labels a simulated send as published", async () => {
    // The single most important assertion on this screen. A simulated send carries
    // `status: "succeeded"` — it DID succeed, at simulating — so anything colouring or
    // labelling by status alone paints a dry run as a delivery.
    body = delivered();
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    const landing = screen.getByText(/landing page/i).closest("div");
    expect(landing).not.toBeNull();
    expect(landing!.textContent?.toLowerCase()).toContain("simulated");
    expect(landing!.textContent?.toLowerCase()).not.toMatch(/\bpublished\b/);
  });

  it("warns in words, not only in colour, that nothing left the process", async () => {
    body = delivered();
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    expect(screen.getByText(/nothing left this process/i)).toBeInTheDocument();
    expect(screen.getByText(/dry run, not a delivery/i)).toBeInTheDocument();
  });

  it("keeps a refusal's reason, because that is the actionable half", async () => {
    body = delivered();
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    expect(screen.getByText(/Connect the account first/i)).toBeInTheDocument();
  });

  it("shows the server's headline rather than recomputing one", async () => {
    // Two headlines is two chances to disagree, and they would disagree on exactly the
    // runs where it matters — the partly-simulated ones.
    body = delivered();
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    expect(screen.getByText(/Published 1 of 3/)).toBeInTheDocument();
  });

  it("says why the owner was not told rather than leaving it blank", async () => {
    body = delivered();
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    expect(screen.getByText(/no email address on record/i)).toBeInTheDocument();
  });

  it("reports leads as not-yet-attributable instead of zero", async () => {
    const withMeasurement = review({
      published: delivered().published,
      measurement: {
        publishedRefs: 1,
        channels: ["landing_page"],
        simulated: true,
        gaps: ["Google Search Console / GA4 (cut from this build)"],
        leadsMeasured: false,
        attributionNote: "No leads are attributable yet: the tracked links were published moments ago.",
      },
      measurementNote: null,
    });
    body = withMeasurement;
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    expect(screen.getByText(/attributable yet/i)).toBeInTheDocument();
    // And what was NOT measured is named, or the rest reads as zero.
    expect(screen.getByText(/Google Search Console/)).toBeInTheDocument();
    // A bare "0 leads" must never appear.
    expect(screen.queryByText(/^0 leads/)).toBeNull();
  });

  it("does label a genuinely delivered destination as published", async () => {
    // The companion to the simulated test: the rule is "do not overstate", not "never
    // say published", and a test that only checked the absence would pass on a panel
    // that could never report a real delivery.
    const real = delivered({
      simulated: false,
      targets: [{ ...simulatedLanding, simulated: false, externalRef: "https://example.com/lp" }],
    });
    body = real;
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /delivery/i }));

    expect(screen.getByText("published")).toBeInTheDocument();
    expect(screen.queryByText(/dry run, not a delivery/i)).toBeNull();
  });
});

/**
 * The retrieval trace panel — `BUILD_ORDER.md` Phase 3 calls this panel "the Hard #1
 * evidence", and until it existed the rewritten queries, the per-chunk grades and the
 * fallback decision were computed inside the process and discarded there.
 *
 * Two rules are load-bearing and both are asserted below.
 *
 * **The decision must be in WORDS.** `fallback_to_web` is a decision the agent made, and
 * anybody who has not read the code reads the raw token as an error. A panel that printed
 * the enum value and nothing else would be reporting a correct degradation as a fault.
 *
 * **The empty state must read as normal.** A business that uploaded no documents has
 * nothing to retrieve, so nothing was retrieved — that is a complete run, and copy that
 * hinted at failure would send the owner hunting a bug instead of uploading a PDF.
 */
describe("retrieval trace panel", () => {
  function trace(over: Partial<RetrievalTrace> = {}): RetrievalTrace {
    return {
      seq: 1,
      node: "GENERATE",
      question: "notdienst koblenz preise",
      needed: true,
      needReason: "The page states a price, so it needs the business's own figures.",
      outcome: "fallback_to_web",
      outcomeReason:
        "No passage graded relevant after two attempts, so the run continues on live research.",
      promptVersion: "kb_retrieve.v1",
      attempts: [
        {
          attempt: 1,
          query: "notdienst anfahrt kosten",
          queryRationale: "Nouns the price list would use, not the node's own question.",
          decision: "retry",
          decisionReason: "Nothing graded relevant, so the query was widened.",
          relevant: 0,
          partial: 0,
          irrelevant: 1,
          grades: [
            {
              chunkId: "chunk-a",
              documentId: "doc-1",
              ordinal: 1,
              grade: "irrelevant",
              reason: "Describes opening hours, not a call-out charge.",
              distance: 0.41,
            },
          ],
          gradesTotal: 1,
          notes: [],
        },
        {
          attempt: 2,
          query: "sanitaer notdienst preisliste koblenz",
          queryRationale: "Adds the city and the document's own word for a price list.",
          decision: "exhausted",
          decisionReason: "Two attempts is the ceiling; one partial is not grounding.",
          relevant: 0,
          partial: 1,
          irrelevant: 0,
          grades: [
            {
              chunkId: "chunk-b",
              documentId: "doc-1",
              ordinal: 2,
              grade: "partial",
              reason: "Mentions a fee, but not the call-out one.",
              distance: 0.23,
            },
          ],
          gradesTotal: 1,
          notes: [],
        },
      ],
      attemptsTotal: 2,
      groundingChunkIds: [],
      chunkCount: 1,
      modelCalls: 3,
      costUsd: "0.0042",
      notes: [],
      ...over,
    };
  }

  async function openRetrieval() {
    await mount();
    await userEvent.click(screen.getByRole("tab", { name: /Retrieval/ }));
    return panelFor(/Retrieval/);
  }

  it("states the fallback decision in words rather than printing the raw value", async () => {
    body = review({ hasOutput: true, retrieval: [trace()], retrievalNote: null });

    const panel = await openRetrieval();

    // The sentence, not the token. This is the assertion the panel exists for.
    expect(panel).toHaveTextContent(
      "Fell back to live web research: the business's own documents did not answer this.",
    );
    // The server's own reason travels with it, so the words are backed by the run's.
    expect(panel).toHaveTextContent(/continues on live research/);
    // And each turn's decision is also words: "retry" alone says nothing about what
    // happened next, which is the whole content of a retrieval loop.
    expect(panel).toHaveTextContent(/rewrote the query and searched again/);
    expect(panel).toHaveTextContent(/attempt ceiling was reached/);
  });

  it("shows the rewritten query for every attempt, and the node that asked", async () => {
    // A system that embeds the node's own question is doing vector search. The rewrite is
    // the difference, so it is the one thing this panel may not summarise away.
    body = review({ hasOutput: true, retrieval: [trace()], retrievalNote: null });

    const panel = await openRetrieval();

    expect(panel).toHaveTextContent("notdienst anfahrt kosten");
    expect(panel).toHaveTextContent("sanitaer notdienst preisliste koblenz");
    expect(panel).toHaveTextContent("GENERATE");
    expect(panel).toHaveTextContent("Attempt 1 of 2");
  });

  it("shows a grade and a reason against every graded passage", async () => {
    body = review({ hasOutput: true, retrieval: [trace()], retrievalNote: null });

    const panel = await openRetrieval();

    expect(panel).toHaveTextContent("irrelevant");
    expect(panel).toHaveTextContent("partial");
    expect(panel).toHaveTextContent("Describes opening hours, not a call-out charge.");
    expect(panel).toHaveTextContent("doc-1#2");
  });

  it("names the node in the no-documents state and does not imply retrieval failed", async () => {
    // The normal state for most businesses on day one. `retrieval: []` with the server's
    // own note, which is what a business that has uploaded nothing gets.
    body = review();

    const panel = await openRetrieval();

    // It names the node that recorded the absence, so the reader can connect the panel to
    // the "written without: uploaded documents" line above the tabs.
    expect(panel).toHaveTextContent("HARVEST");
    expect(panel).toHaveTextContent(/normal run/);
    expect(panel).toHaveTextContent(/nothing to retrieve, so nothing was retrieved/);

    // And nothing in it reads as a fault. Asserted as an explicit negative because the
    // failure mode here is a helpful-sounding word, not a blank panel.
    const text = (panel.textContent ?? "").toLowerCase();
    for (const word of ["fail", "error", "unavailable", "broke", "could not", "problem"]) {
      expect(text).not.toContain(word);
    }
  });

  it("renders no fabricated trace when the API sends none", async () => {
    // The invented-content rule, applied here. A sample query with a plausible grade would
    // be the worst possible lie on this screen: it is the evidence panel.
    body = review();

    const panel = await openRetrieval();

    expect(panel).not.toHaveTextContent(/searched for/);
    expect(panel).not.toHaveTextContent(/Attempt 1/);
  });

  it("says so when the stored trace count was capped instead of quietly starting at 4", async () => {
    // `seq` survives the cap, so a first entry numbered above 1 means earlier calls were
    // dropped. A panel that showed the tail as if it were the whole retrieval would be a
    // quieter version of exactly the bug this key was added to fix.
    body = review({ hasOutput: true, retrieval: [trace({ seq: 4 })], retrievalNote: null });

    const panel = await openRetrieval();

    expect(panel).toHaveTextContent(/the earliest 3 are not shown/);
  });

  it("falls through to the server's value for an outcome it does not recognise", async () => {
    // The server owns this vocabulary and can add to it, and an app in a browser tab is
    // older than the API it is talking to. Showing the raw value beats showing the wrong
    // sentence, and beats showing nothing.
    body = review({
      hasOutput: true,
      retrieval: [
        trace({
          outcome: "deferred_to_operator",
          outcomeReason: "A future outcome this build has never heard of.",
        }),
      ],
      retrievalNote: null,
    });

    const panel = await openRetrieval();

    expect(panel).toHaveTextContent("deferred to operator");
    expect(panel).toHaveTextContent("A future outcome this build has never heard of.");
  });
});

/* ------------------------------------------------------------------------- */
/* The approve gate                                                           */
/* ------------------------------------------------------------------------- */

/**
 * The human decision, which for most of this project's life had no button.
 *
 * `POST /api/v1/runs/{id}/approve` existed and was tested, and nothing in the UI called
 * it: a reviewer could read every tab on this screen and the only control offered was
 * Resume, which deliberately refuses a parked run. So these tests are less about pixels
 * than about four claims the screen must not get wrong — that the control exists only
 * where it can work, that its copy does not promise a post the machine will refuse, that
 * the two refusals stay two different sentences, and that success reads as work STARTED.
 */
describe("the approve gate", () => {
  const APPROVABLE = "awaiting_approval";

  /** One fetch answer, shaped the way the API shapes it. */
  function stubApprove(outcome: { ok: boolean; status: number; body: unknown }) {
    const mock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve({
        ok: outcome.ok,
        status: outcome.status,
        json: () => Promise.resolve(outcome.body),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", mock);
    return mock;
  }

  const accepted = { ok: true, status: 202, body: { runId: "r1", state: "running" } };

  function refused(code: string, message: string) {
    return { ok: false, status: 409, body: { detail: { code, message } } };
  }

  function gate(state: string, onApproved?: () => void) {
    render(<ApproveGate runId="r1" runState={state} onApproved={onApproved} />);
  }

  function approveButton(): HTMLElement {
    return screen.getByRole("button", { name: /approve/i });
  }

  /**
   * The rule from the task, and the one worth an exhaustive loop: a control that can never
   * work is worse than no control. Every one of these states is a 409 at the API, so a
   * button here would be a button whose only outcome is a refusal.
   */
  it("renders no approve control in any state other than awaiting_approval", () => {
    for (const state of ["queued", "running", "done", "failed", "partial"]) {
      const { unmount } = render(<ApproveGate runId="r1" runState={state} />);
      expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
      unmount();
    }
  });

  /**
   * Asserted separately from the line above because "absent" and "present but disabled"
   * pass and fail the same query only if you write the query carelessly. A greyed-out
   * Approve announces a decision that is not available, which is the failure the export
   * tab's own docstring already refuses for Publish.
   */
  it("does not offer a disabled approve control instead of hiding it", () => {
    const { container } = render(<ApproveGate runId="r1" runState="done" />);

    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(container.textContent?.toLowerCase() ?? "").not.toContain("approve");
  });

  it("offers the control when the run is parked at the gate", () => {
    gate(APPROVABLE);

    expect(approveButton()).toBeEnabled();
  });

  /**
   * Requirement 2, and the one this project cares about most: the copy has to say what
   * approving DOES without promising what it cannot deliver. The landing page publishes
   * for real; social refuses with no connected account; email needs a key. A gate that
   * said "your posts go live" would be making a claim the actuators then refuse, on the
   * one screen where a person is deciding whether to let it happen.
   */
  it("says what approving does per destination, and does not promise a post that will refuse", () => {
    const { container } = render(<ApproveGate runId="r1" runState={APPROVABLE} />);
    const text = container.textContent ?? "";

    // It names the thing approval unlocks, in the machine's own terms.
    expect(text).toContain("EXPORT");
    expect(text).toContain("MEASURE");
    expect(text).toMatch(/lets this run publish/i);

    // The one real destination is named as real.
    expect(text).toMatch(/Landing page/);
    expect(text).toMatch(/for real/i);

    // And the two that are not are named as not.
    expect(text).toMatch(/Social posts/);
    expect(text).toMatch(/refuse unless that platform.s account is connected/i);
    expect(text).toMatch(/Email/);
    expect(text).toMatch(/only where an email key is configured/i);

    // The overstatement, asserted as an explicit negative: the failure mode here is a
    // confident word, not a missing one.
    expect(text).not.toMatch(/go live/i);
    expect(text).not.toMatch(/posts to every channel/i);
  });

  /** The approver is the session's, so the screen must not imply it is choosing one. */
  it("states that the approver comes from the session rather than from this screen", () => {
    const { container } = render(<ApproveGate runId="r1" runState={APPROVABLE} />);

    expect(container.textContent ?? "").toMatch(/taken from your session/i);
  });

  it("approves, sends no approver, and reports work STARTED rather than finished", async () => {
    const user = userEvent.setup();
    const fetchMock = stubApprove(accepted);
    const onApproved = vi.fn();
    gate(APPROVABLE, onApproved);

    await user.click(approveButton());
    await settle();

    // The wire shape, asserted here too and not only in `runs-api.test.ts`: this is the
    // path a person actually takes, and the authorisation decision is the server's.
    const call = fetchMock.mock.calls[0];
    expect(String(call?.[0])).toContain("/api/v1/runs/r1/approve");
    expect(call?.[1]?.method).toBe("POST");
    expect(call?.[1]?.body).toBeUndefined();
    expect(JSON.stringify(call?.[1] ?? {})).not.toContain("approver");

    // 202 means accepted, not done. EXPORT and MEASURE take minutes, and a confirmation
    // reading "published" would be a finished word for unfinished work.
    expect(screen.getByText(/publishing has started/i)).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/published successfully/i);
    expect(text).not.toMatch(/all done/i);

    // And the screen around the card is told, so the state pill and the timeline stop
    // showing a gate that has been passed.
    expect(onApproved).toHaveBeenCalled();

    // The control is gone: there is nothing left to approve.
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
  });

  /**
   * Refusal one. The API's own sentence names the ACTUAL state, which this screen cannot
   * know better — so it is rendered rather than paraphrased, and the guidance added
   * beside it is about a stale screen.
   */
  it("renders run_not_awaiting_approval's own sentence and re-reads the run", async () => {
    const user = userEvent.setup();
    stubApprove(
      refused(
        "run_not_awaiting_approval",
        "This run is running, not waiting for approval. Only a run parked at the review gate can be approved.",
      ),
    );
    const onApproved = vi.fn();
    gate(APPROVABLE, onApproved);

    await user.click(approveButton());
    await settle();

    expect(screen.getByRole("alert")).toHaveTextContent(
      /This run is running, not waiting for approval/,
    );
    // The refusal happened because the screen was out of date, so it re-reads rather than
    // leaving the reader looking at the state that was already wrong.
    expect(onApproved).toHaveBeenCalled();

    // Not the other refusal's guidance, and not a generic failure.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/nothing to publish/i);
    expect(text).not.toMatch(/something went wrong/i);
  });

  /** Refusal two: parked before it produced anything, so there is nothing to approve. */
  it("renders no_checkpoint's own sentence, and a different one", async () => {
    const user = userEvent.setup();
    stubApprove(
      refused(
        "no_checkpoint",
        "This run has no checkpoint, so there is nothing to approve. It was parked before it produced anything.",
      ),
    );
    gate(APPROVABLE);

    await user.click(approveButton());
    await settle();

    expect(screen.getByRole("alert")).toHaveTextContent(/no checkpoint/);
    expect(screen.getByText(/Start a new run rather than approving this one/)).toBeInTheDocument();

    // The whole point of two codes: the other refusal's guidance must not appear here.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/was out of date/i);
    expect(text).not.toMatch(/something went wrong/i);
  });

  /**
   * The two 409s must not be one message. Asserted by rendering both and comparing, so a
   * future collapse into a shared "could not approve" string fails here rather than
   * passing every individual test above.
   */
  it("keeps the two refusals as two different sentences", async () => {
    async function refusalText(code: string, message: string): Promise<string> {
      const user = userEvent.setup();
      stubApprove(refused(code, message));
      const { container, unmount } = render(
        <ApproveGate runId="r1" runState={APPROVABLE} />,
      );
      await user.click(screen.getByRole("button", { name: /approve/i }));
      await settle();
      const text = container.textContent ?? "";
      unmount();
      vi.unstubAllGlobals();
      return text;
    }

    const notAwaiting = await refusalText(
      "run_not_awaiting_approval",
      "This run is running, not waiting for approval.",
    );
    const noCheckpoint = await refusalText(
      "no_checkpoint",
      "This run has no checkpoint, so there is nothing to approve.",
    );

    expect(notAwaiting).not.toEqual(noCheckpoint);
  });

  /**
   * Reject is deliberately absent — it needs a terminal state `runs.state`'s CHECK
   * constraint does not have, so it is a schema change and an open decision, not a control
   * somebody forgot. A disabled one would announce it as nearly here.
   */
  it("offers no reject control, disabled or otherwise", () => {
    const { container } = render(<ApproveGate runId="r1" runState={APPROVABLE} />);

    expect(screen.queryByRole("button", { name: /reject|decline|deny/i })).toBeNull();
    expect(container.textContent?.toLowerCase() ?? "").not.toContain("reject");
  });
});
