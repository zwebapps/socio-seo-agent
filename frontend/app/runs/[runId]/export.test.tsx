/**
 * The export pack surface: honest labels, and the per-channel cost of posting by hand.
 *
 * Two failures are worth a test suite of their own here, and neither is caught by
 * type-checking.
 *
 * **A control that implies we post for you.** Nothing in this product reaches a platform.
 * A button called "Publish" or "Post to LinkedIn" on this screen would have the owner
 * believe their content was live when it is sitting in a browser tab — the most expensive
 * misunderstanding this app could create, and one that a well-meaning rename introduces in
 * four characters. So the first test refuses the WORDS, not the behaviour: no control on
 * this screen may be named with a verb that implies posting.
 *
 * **A channel warning that goes missing.** docs/CHANNELS.md §1 calls it the correction
 * that matters most: an Instagram caption does not render a clickable link, so a URL there
 * is not a broken link, it is NO link. A poster who is not told loses the click and the
 * attribution with it, and nothing on the screen looks wrong.
 */

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ExportChannel, ExportPack, ExportTrackedLink } from "@/app/lib/export-api";
import { ExportPanel } from "@/app/runs/[runId]/export";

/** The notes the API actually sends, each naming the node responsible. */
const NOTES = {
  channels:
    "No social posts were rendered. Per-channel copy is produced by the REPACK node, " +
    "which has not completed for this run.",
  landing:
    "No landing page was written, so there is nothing for a click to land on. The page, " +
    "its offer and its per-channel asks come from the CONVERT node, which has not " +
    "completed for this run.",
  aiBlocks: "No answer blocks were produced. They come from the PLAN node's outline.",
  trackedLink:
    "No tracked short link is in this pack. A short link is minted when a landing page " +
    "is published to a public address, and that has not happened for this run.",
  hub: "This is the business's own bio-link page and it is the entire conversion path for Instagram and TikTok.",
  notice:
    "This pack sends nothing to any platform. Every block below is text for a person to " +
    "paste in themselves.",
} as const;

const LINKEDIN: ExportChannel = {
  channel: "linkedin",
  body: "Kurz erklärt: was ein Notar beurkundet. #Notar",
  pasteText: "Kurz erklärt: was ein Notar beurkundet. #Notar",
  hashtags: ["#Notar"],
  appendedHashtags: [],
  bodyCharacters: 45,
  pasteCharacters: 45,
  characterTarget: 1700,
  characterLimit: 3000,
  hashtagCount: 1,
  hashtagMinimum: 0,
  hashtagLimit: 3,
  hashtagsRemoved: 2,
  hashtagsShortfall: 0,
  overTarget: false,
  overLimit: false,
  linkInBody: true,
  linkMechanism: "inline",
  notes: ["2 hashtags were removed in code to stay inside this channel's cap."],
};

const INSTAGRAM: ExportChannel = {
  channel: "instagram",
  body: "Wer beurkundet einen Grundstückskauf?",
  pasteText: "Wer beurkundet einen Grundstückskauf?\n\n#Notar #Koblenz #Immobilien",
  hashtags: ["#Notar", "#Koblenz", "#Immobilien"],
  appendedHashtags: ["#Notar", "#Koblenz", "#Immobilien"],
  bodyCharacters: 36,
  pasteCharacters: 65,
  characterTarget: 2200,
  characterLimit: 2200,
  hashtagCount: 3,
  hashtagMinimum: 3,
  hashtagLimit: 5,
  hashtagsRemoved: 0,
  hashtagsShortfall: 0,
  overTarget: false,
  overLimit: false,
  linkInBody: false,
  linkMechanism: "bio_hub",
  notes: [
    "A link in the body does not work on this channel — on instagram a URL in the body " +
      "is plain text, not a clickable link. Use the bio hub instead.",
  ],
};

/**
 * What a run that actually published looks like on the wire.
 *
 * Shaped after the addresses `_published_addresses` reads back out of the `publish.page`
 * outcome: an absolute page URL, and one minted short link per channel CTA. The default
 * `pack()` below carries neither, because most runs have not published and the absence is
 * the case the older tests here were written to protect.
 */
const PAGE_URL = "http://localhost:8100/p/6f1c9b02-0000-4000-8000-000000000001";

const TRACKED: ExportTrackedLink[] = [
  {
    channel: "instagram",
    text: "Link in der Bio",
    code: "aB3xK9mQ",
    url: "http://localhost:8100/l/aB3xK9mQ",
  },
  {
    channel: "linkedin",
    text: "Termin anfragen",
    code: "Zq7Lp2Wd",
    url: "http://localhost:8100/l/Zq7Lp2Wd",
  },
];

function pack(over: Partial<ExportPack> = {}): ExportPack {
  return {
    hasPack: true,
    notice: NOTES.notice,
    channels: [LINKEDIN, INSTAGRAM],
    channelsNote: null,
    landingPage: null,
    landingPageNote: NOTES.landing,
    aiBlocks: null,
    aiBlocksNote: NOTES.aiBlocks,
    hubUrl: "http://localhost:8100/go/11111111-1111-1111-1111-111111111111",
    hubNote: NOTES.hub,
    publishedPageUrl: null,
    trackedLinks: [],
    trackedLinkNote: NOTES.trackedLink,
    factGaps: [],
    errors: [],
    ...over,
  };
}

/** A run that published: a real page, real codes, and therefore no absence to explain. */
function published(over: Partial<ExportPack> = {}): ExportPack {
  return pack({
    publishedPageUrl: PAGE_URL,
    trackedLinks: TRACKED,
    trackedLinkNote: null,
    ...over,
  });
}

let body: ExportPack = pack();

beforeEach(() => {
  body = pack();
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
  render(<ExportPanel runId="r1" runState="awaiting_approval" />);
  await settle();
}

/** Every control's accessible name, which is what a screen-reader user hears. */
function controlNames(): string[] {
  const controls = [...screen.queryAllByRole("button"), ...screen.queryAllByRole("link")];
  return controls.map((node) => node.getAttribute("aria-label") ?? node.textContent ?? "");
}

/* ------------------------------------------------------------------------- */

describe("honest labelling", () => {
  /**
   * The rule, asserted on the words rather than on the behaviour: this screen posts
   * nothing, so no control on it may be named as though it did. Written as an explicit
   * negative because the failure is a helpful-looking one — "Post to LinkedIn" is what
   * anyone would call this button if they had not read docs/CHANNELS.md §2.
   */
  it("labels no control with a word that implies posting to a platform", async () => {
    await mount();

    const names = controlNames();
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      expect(name).not.toMatch(/publish|post|schedule|send|share|go live/i);
    }
  });

  /** What the controls DO say, so the previous test cannot pass by rendering nothing. */
  it("says copy and download, which is what the controls actually do", async () => {
    await mount();

    expect(screen.getByRole("button", { name: "Copy the LinkedIn text" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy the Instagram text" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Download the pack/ })).toBeInTheDocument();
  });

  /**
   * The claim carried in the payload, rendered rather than paraphrased. If this screen
   * summarised it in its own words, the file and the screen could end up saying different
   * things about the same pack.
   */
  it("states that nothing has been sent to a platform", async () => {
    await mount();
    expect(screen.getByText(NOTES.notice)).toBeInTheDocument();
  });

  /**
   * The placeholder for a capability that does not exist. It is allowed to be on the
   * screen; it is not allowed to be a control, and it has to say it is not connected.
   */
  it("says direct publishing does not happen here, and offers no control pretending it does", async () => {
    await mount();

    expect(screen.getByText(/Not from this screen\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
  });
});

describe("what it costs to post this by hand", () => {
  /**
   * The Instagram/TikTok truth. Twice over on purpose: as the compact "link" measurement
   * and as the note, because the first is scannable and the second is the sentence that
   * explains what to do instead.
   */
  it("warns that a link in the body does not work on Instagram", async () => {
    await mount();

    expect(screen.getByText(/not clickable in the body — use the bio hub/)).toBeInTheDocument();
    expect(
      screen.getByText(/A link in the body does not work on this channel/),
    ).toBeInTheDocument();
  });

  /** And does not repeat the warning on the channel where a link works — noise gets skipped. */
  it("does not warn about links on a channel that carries them", async () => {
    body = pack({ channels: [LINKEDIN] });
    await mount();

    expect(screen.queryByText(/does not work on this channel/)).not.toBeInTheDocument();
    expect(screen.getByText(/clickable in the body/)).toBeInTheDocument();
  });

  /**
   * A bare character count answers nothing: 65 characters is fine on Instagram and
   * impossible on X. Every figure is rendered against the thing it has to fit inside.
   */
  it("shows each count against this channel's target and platform limit", async () => {
    await mount();

    expect(screen.getByText(/45 characters/)).toBeInTheDocument();
    expect(screen.getByText(/target 1,700/)).toBeInTheDocument();
    expect(screen.getByText(/platform limit 3,000/)).toBeInTheDocument();
    expect(screen.getByText(/at most 3/)).toBeInTheDocument();
  });

  /**
   * Over the editorial target and over the platform's own ceiling are different facts —
   * one is publishable and long, the other is refused — and they are distinguished in
   * WORDS, not only by two shades of orange.
   */
  it("distinguishes over-target from over-limit in words", async () => {
    body = pack({
      channels: [{ ...LINKEDIN, pasteCharacters: 1900, overTarget: true }],
    });
    await mount();
    expect(screen.getByText(/over target, still publishable/)).toBeInTheDocument();
    expect(screen.queryByText(/over the platform limit/)).not.toBeInTheDocument();

    body = pack({
      channels: [{ ...LINKEDIN, pasteCharacters: 3200, overTarget: true, overLimit: true }],
    });
    render(<ExportPanel runId="r2" runState="done" />);
    await settle();
    expect(screen.getByText(/over the platform limit/)).toBeInTheDocument();
  });

  /**
   * What code had to correct, said out loud. A tidy block shown without this credits the
   * model for the renderer's work.
   */
  it("says how many hashtags code had to remove", async () => {
    await mount();
    expect(screen.getByText(/2 hashtags were removed in code/)).toBeInTheDocument();
  });
});

describe("copying and downloading", () => {
  /**
   * The clipboard gets `pasteText`, which is the server-assembled string the counts were
   * measured against — not the body, which on Instagram is 29 characters shorter because
   * the declared hashtags are appended to it.
   */
  it("copies the exact string the counts describe", async () => {
    // `userEvent.setup()` installs its own clipboard stub in jsdom, so the clipboard is
    // read back through it rather than through a hand-rolled `writeText` spy — which
    // would be silently replaced and never called.
    const user = userEvent.setup();
    await mount();

    await user.click(screen.getByRole("button", { name: "Copy the Instagram text" }));

    const copied = await navigator.clipboard.readText();
    expect(copied).toBe(INSTAGRAM.pasteText);
    // Not the body: on Instagram the declared hashtags are appended, so the body alone is
    // 29 characters short of what the screen measured and what should be pasted.
    expect(copied).not.toBe(INSTAGRAM.body);
    expect(screen.getByText("copied")).toBeInTheDocument();
  });

  /**
   * A real link to the Markdown rendering, so the download works with JavaScript off and
   * the filename comes from the server's `Content-Disposition` rather than being
   * re-invented in the browser.
   */
  it("links to the markdown rendering of this run's pack", async () => {
    await mount();

    const link = screen.getByRole("link", { name: /Download the pack/ });
    expect(link).toHaveAttribute("href", expect.stringContaining("/api/v1/runs/r1/export"));
    expect(link).toHaveAttribute("href", expect.stringContaining("format=markdown"));
  });
});

describe("a run that published its page", () => {
  /**
   * The published page reaches the screen as a real destination, not as a string.
   *
   * It has to be an anchor: this is the address a reader clicks to check what they are
   * about to point a customer at, and it is the one thing on this tab that leaves the app.
   * `target="_blank"` with `rel="noopener"` because losing the run you are reviewing in
   * order to look at its output is a bad trade.
   */
  it("renders the published page as a link that goes to it", async () => {
    body = published();
    await mount();

    const link = screen.getByRole("link", { name: PAGE_URL });
    expect(link).toHaveAttribute("href", PAGE_URL);
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  /**
   * One link per channel, each with the code beside it.
   *
   * The code is not decoration: it is the `short_links` row's key and therefore what a
   * later report counts, so it is what lets an owner match a figure back to the paste that
   * earned it. A screen showing two indistinguishable URLs would make that match guesswork.
   */
  it("renders one tracked link per channel, with the code that attributes the click", async () => {
    body = published();
    await mount();

    for (const link of TRACKED) {
      expect(screen.getByRole("link", { name: link.url })).toHaveAttribute("href", link.url);
      expect(screen.getByText(`code ${link.code}`)).toBeInTheDocument();
    }
    // Named per channel, and by the label the rest of the app uses — the raw `instagram`
    // key on a customer surface is developer output leaking out.
    expect(screen.getByText(/Instagram:/)).toBeInTheDocument();
    expect(screen.getByText(/LinkedIn:/)).toBeInTheDocument();
    // The ask travels with the link, so what is pasted is the whole post.
    expect(screen.getByText("Link in der Bio")).toBeInTheDocument();
  });

  /**
   * The mirror image of the absence rule, and the reason `trackedLinkNote` became
   * nullable: a sentence saying no short link exists, printed above two working ones, is
   * the screen contradicting itself. The server sends `null` once links exist; this test
   * proves the client keys on that instead of rendering the note unconditionally.
   */
  it("does not explain an absence when the links are there", async () => {
    body = published();
    await mount();

    expect(screen.queryByText(NOTES.trackedLink)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/No tracked short link/);
  });

  /**
   * A page CAN be published with no tracked links behind it — a CTA missing its code is
   * dropped by the server, and a page with no channel asks mints nothing. Then both halves
   * are true at once, and each keys on its own field rather than on the other.
   */
  it("shows the page and still explains the missing links when only the page exists", async () => {
    body = published({ trackedLinks: [], trackedLinkNote: NOTES.trackedLink });
    await mount();

    expect(screen.getByRole("link", { name: PAGE_URL })).toBeInTheDocument();
    expect(screen.getByText(NOTES.trackedLink)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\/l\/[0-9a-zA-Z]{6,}/);
  });

  /**
   * The honest-labelling rule again, on the controls this feature added — and it is a real
   * risk rather than a formality: "published page" is the obvious thing to put inside the
   * anchor, and it would name a control with the very word the rule forbids. The label
   * stays outside; the accessible name is the destination.
   */
  it("adds no control named as though this screen published anything", async () => {
    body = published();
    await mount();

    const names = controlNames();
    expect(names).toContain(PAGE_URL);
    for (const name of names) {
      expect(name).not.toMatch(/publish|post|schedule|send|share|go live/i);
    }
  });
});

describe("a run with nothing in it yet", () => {
  /**
   * The same rule the review tabs keep: an empty section names the node that fills it. An
   * empty panel is indistinguishable from a rendering bug, and "REPACK has not completed"
   * is a different problem from "the download is broken".
   */
  it("names the node responsible instead of showing an empty section", async () => {
    body = pack({ hasPack: false, channels: [], channelsNote: NOTES.channels });
    await mount();

    expect(screen.getByText(NOTES.channels)).toBeInTheDocument();
    expect(screen.getByText(NOTES.landing)).toBeInTheDocument();
    expect(screen.getByText(NOTES.aiBlocks)).toBeInTheDocument();
  });

  /** And invents no copy to fill it — a sample post here would be a lie about the run. */
  it("invents no channel copy and no copy control for a channel that has none", async () => {
    body = pack({ hasPack: false, channels: [], channelsNote: NOTES.channels });
    await mount();

    expect(screen.queryByRole("button", { name: /Copy the/ })).not.toBeInTheDocument();
  });

  /**
   * A run that published nothing has no tracked short link, so the pack states the
   * absence. Rendering a plausible `/l/xxxxxxxx` instead would put a dead URL in somebody's
   * Instagram bio, where the failure is invisible until the leads do not arrive.
   */
  it("states that there is no tracked short link rather than showing one", async () => {
    await mount();

    expect(screen.getByText(NOTES.trackedLink)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\/l\/[0-9a-zA-Z]{6,}/);
  });

  /**
   * And no page address either — which is the case a SIMULATED publish produces. The
   * server refuses a `fake://` reference and sends `null`, and a simulated publish looks
   * successful in every respect except reaching the world, so this is precisely the run
   * where an invented-looking address would be believed. No placeholder stands in for it:
   * there is nothing to click, and the note above already says why.
   */
  it("shows no published page address when the run published none", async () => {
    await mount();

    expect(document.body.textContent).not.toMatch(/\/p\/[0-9a-f-]{8,}/);
    expect(screen.queryByRole("link", { name: /published/i })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/published page/i);
  });
});

describe("when the pack cannot be loaded", () => {
  /**
   * A transport failure must not read as a run that produced nothing: those are opposite
   * facts, and a silent empty panel has the owner blaming the agent for a network error.
   */
  it("shows the API's message rather than an empty pack", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 404,
          json: () =>
            Promise.resolve({ detail: { code: "run_not_found", message: "No such run." } }),
        } as unknown as Response),
      ),
    );

    await mount();

    expect(screen.getByText("No such run.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Download the pack/ })).not.toBeInTheDocument();
  });
});
