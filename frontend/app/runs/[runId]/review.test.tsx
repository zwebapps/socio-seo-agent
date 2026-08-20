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

import type { RunReview } from "@/app/lib/review-api";
import { RunReviewTabs } from "@/app/runs/[runId]/review";

/** The notes the API actually sends, each naming the node responsible. */
const NOTES = {
  draft: "GENERATE has not completed for this run.",
  seo: "VALIDATE has not scored a draft for this run.",
  social: "REPACK has not written posts for this run.",
  ai: "GENERATE has not produced answer blocks for this run.",
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
