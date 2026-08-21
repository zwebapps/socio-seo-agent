/**
 * The owner's dashboard: the KPI row's honesty, and the onboarding panels it must not
 * have pushed off the screen.
 *
 * Every test here is aimed at a failure that looks fine on screen, which is the only kind
 * worth writing for a page made of numbers:
 *
 * - **A `null` metric rendered as `0`.** This is the one that matters. Six tidy zeroes
 *   look like a working dashboard for a business that has never been measured, and an
 *   owner who reads "0 SEO problems" as "my site is clean" has been told something false
 *   by us. So the null tests assert the explanation is present AND that no `0` reached the
 *   document at all — the assertion a snapshot or a "renders without crashing" test would
 *   both pass while the bug shipped.
 * - **An unreachable summary rendered as zeroes.** Same failure, different cause: a 404 or
 *   a network drop must not become a measurement.
 * - **The onboarding panels lost to the new layout.** They are the only path a business
 *   with no profile has, and the tile row was inserted above them. All three states are
 *   pinned so a future layout change cannot silently drop one.
 *
 * `fetch` is faked one level below the API clients, keyed on URL rather than on call
 * order: the page fires four independent reads (health, onboarding, runs, dashboard) whose
 * completion order is not defined, so a queue of replies would make these tests flaky for
 * a reason that has nothing to do with the code under test.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SessionProvider } from "@/app/components/session-context";
import { normalizeSummary, topChannel } from "@/app/lib/dashboard-api";
import Home from "@/app/dashboard/page";

/**
 * The page inside the provider the real root layout gives it.
 *
 * Without it `useSession` returns the default `loading` context forever, the dashboard
 * stays gated, and every assertion about a tile fails for a reason that has nothing to
 * do with what it is testing.
 */
function Providers() {
  return (
    <SessionProvider>
      <Home />
    </SessionProvider>
  );
}

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

type Reply = { ok?: boolean; status?: number; body: unknown };

function response(reply: Reply): Response {
  return {
    ok: reply.ok ?? true,
    status: reply.status ?? 200,
    statusText: "",
    json: () => Promise.resolve(reply.body),
  } as unknown as Response;
}

/** A complete summary, every metric measured. Overridden per test. */
function summary(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    clicksTotal: 1240,
    clicksByChannel: [
      { channel: "facebook", clicks: 340 },
      { channel: "linkedin", clicks: 900 },
    ],
    clicksFromBots: 17,
    runsTotal: 12,
    runsAwaitingApproval: 2,
    runsPartial: 1,
    leadsTotal: 5,
    spendUsd: "3.4218",
    seoProblems: 7,
    seoPagesAudited: 24,
    seoTruncated: false,
    shareOfVoice: 18.5,
    gaps: [],
    ...over,
  };
}

/** Nothing measured. Every nullable metric absent, which is how a fresh account reads. */
function nothingMeasured(): Record<string, unknown> {
  return {
    clicksTotal: null,
    clicksByChannel: [],
    clicksFromBots: null,
    runsTotal: null,
    runsAwaitingApproval: null,
    runsPartial: null,
    leadsTotal: null,
    spendUsd: null,
    seoProblems: null,
    seoPagesAudited: null,
    seoTruncated: false,
    shareOfVoice: null,
    gaps: [],
  };
}

let onboarding: Reply = { body: { hasBusiness: true, onboarded: true, name: "Ada", website: null } };
let dashboard: Reply = { body: summary() };

beforeEach(() => {
  onboarding = { body: { hasBusiness: true, onboarded: true, name: "Ada", website: null } };
  dashboard = { body: summary() };

  globalThis.fetch = vi.fn((input: unknown) => {
    const url = String(input);
    if (url.includes("/api/v1/dashboard")) return Promise.resolve(response(dashboard));
    if (url.includes("/api/v1/onboarding")) return Promise.resolve(response(onboarding));
    if (url.includes("/api/v1/runs")) {
      return Promise.resolve(response({ body: { runs: [], nextCursor: null } }));
    }
    if (url.includes("/api/v1/auth/me")) {
      // The dashboard is gated on being signed in — an anonymous visitor gets one quiet
      // notice instead of three red alerts — so every test here needs a session.
      return Promise.resolve(
        response({
          body: { id: "u1", email: "owner@example.test", role: "owner", businessId: "b1" },
        }),
      );
    }
    if (url.includes("/api/v1/health")) {
      return Promise.resolve(
        response({
          body: { status: "ok", service: "sma", version: "0.1.0", environment: "test" },
        }),
      );
    }
    throw new Error(`unexpected request: ${url}`);
  }) as unknown as typeof fetch;
});

/** The tile whose heading is `label`, so an assertion cannot match a sibling tile. */
async function tile(label: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: label });
  const item = heading.closest("li");
  if (item === null) throw new Error(`the "${label}" tile is not a list item`);
  return item;
}

/* --- the rule: absent is not zero -------------------------------------------- */

describe("an unmeasured metric", () => {
  it("explains itself in words and never renders a zero", async () => {
    dashboard = { body: nothingMeasured() };
    render(<Providers />);

    // Each tile says what the missing number means, rather than showing a figure.
    expect(await tile("Tracked clicks")).toHaveTextContent(
      /A click is only counted when a published post carries a tracked link/,
    );
    expect(await tile("AI share of voice")).toHaveTextContent(/a sample, never a census/);
    expect(await tile("SEO problems on your site")).toHaveTextContent(
      /has not been audited yet/,
    );
    expect(await tile("Model spend")).toHaveTextContent(/No model spend reported/);
    expect(await tile("Leads")).toHaveTextContent(/A lead is counted when/);

    // The failure this test exists for: six tidy zeroes on a business nobody has
    // measured. `0` must not appear anywhere on the page, in any tile, as any part of a
    // number — not "0", not "0%", not "$0.00".
    expect(screen.queryByText("0")).toBeNull();
    expect(screen.queryByText(/\b0(\.\d+)?%?$/)).toBeNull();
    expect(screen.queryByText(/^\$0/)).toBeNull();

    // And an em-dash with nothing behind it is the other tempting placeholder.
    expect(screen.queryByText("—")).toBeNull();
    expect(screen.queryByText("–")).toBeNull();
  });

  it("marks the tile as unmeasured as well as explaining it", async () => {
    dashboard = { body: nothingMeasured() };
    render(<Providers />);

    // The sentence is the explanation; the pill is what a skim-reader sees. Colour is
    // not carrying it — the words "not measured" are in the DOM.
    const clicks = await tile("Tracked clicks");
    expect(within(clicks).getByText("not measured")).toBeInTheDocument();
  });

  it("does not turn an unreachable summary into zeroes", async () => {
    // A 404 is the live case while the endpoint is still being built, and `ApiError`
    // renders it as "Request failed (404)" — which sends an owner to check their network
    // for a route that was never deployed.
    dashboard = { ok: false, status: 404, body: null };
    render(<Providers />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /no dashboard summary endpoint/,
    );
    expect(screen.queryByText("0")).toBeNull();
    // No tiles at all, rather than tiles full of nothing.
    expect(screen.queryByRole("heading", { name: "Tracked clicks" })).toBeNull();
  });

  it("re-reads on demand after a failure", async () => {
    dashboard = { ok: false, status: 503, body: null };
    render(<Providers />);
    await screen.findByRole("alert");

    dashboard = { body: summary() };
    await userEvent.click(screen.getByRole("button", { name: /Try loading your numbers again/ }));

    expect(await tile("Tracked clicks")).toHaveTextContent("1,240");
  });
});

/* --- a measured metric ------------------------------------------------------- */

describe("a measured metric", () => {
  it("names the top channel beside the click count", async () => {
    render(<Providers />);
    const clicks = await tile("Tracked clicks");
    expect(clicks).toHaveTextContent("1,240");
    // LinkedIn has 900 to Facebook's 340, so ordering by array position rather than by
    // clicks would name the wrong channel while looking entirely plausible.
    expect(clicks).toHaveTextContent("Most from LinkedIn · 900");
    expect(clicks).toHaveTextContent("17 bot hits excluded");
  });

  it("says how many runs await approval", async () => {
    render(<Providers />);
    const runs = await tile("Runs");
    expect(runs).toHaveTextContent("12");
    expect(runs).toHaveTextContent("2 await approval");
    // A run that stopped short is the fact this product is least allowed to round off.
    expect(runs).toHaveTextContent("1 stopped short");
  });

  it("still calls share of voice a sample when there is a number", async () => {
    // docs/ARCHITECTURE.md §15.2: model answers are non-deterministic and shift with
    // model updates. A bare "18.5%" reads as a measured market share, which it is not —
    // and the caveat is easiest to lose exactly when a real figure arrives.
    render(<Providers />);
    const sov = await tile("AI share of voice");
    expect(sov).toHaveTextContent("18.5%");
    expect(sov).toHaveTextContent(/sample of model answers, not a census/);
  });

  it("renders spend as the string it arrived as", async () => {
    // Money is Decimal server-side. Parsing "3.4218" into a JS number here is how the
    // one figure a customer checks against an invoice acquires a rounding error.
    render(<Providers />);
    expect(await tile("Model spend")).toHaveTextContent("$3.4218");
  });

  it("says the audit was partial when the crawl was cut short", async () => {
    dashboard = { body: summary({ seoTruncated: true }) };
    render(<Providers />);
    expect(await tile("SEO problems on your site")).toHaveTextContent(
      /covers part of the site, not all of it/,
    );
  });

  it("distinguishes a measured zero from an unmeasured metric", async () => {
    // The other half of the rule, and the half that is easy to over-correct into: zero
    // leads IS a measurement and must render as a figure, not as "not measured".
    dashboard = { body: summary({ leadsTotal: 0 }) };
    render(<Providers />);
    const leads = await tile("Leads");
    expect(within(leads).getByText("0")).toBeInTheDocument();
    expect(within(leads).queryByText("not measured")).toBeNull();
  });
});

/* --- the gaps ---------------------------------------------------------------- */

describe("gaps", () => {
  it("renders the API's own account of what it could not measure", async () => {
    dashboard = {
      body: summary({
        gaps: [
          "No tracked links have been published, so clicks cannot be attributed.",
          "Share of voice has never been sampled for this business.",
        ],
      }),
    };
    render(<Providers />);

    expect(
      await screen.findByText(
        "No tracked links have been published, so clicks cannot be attributed.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Share of voice has never been sampled for this business."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "What is not measured yet" }),
    ).toBeInTheDocument();
  });

  it("renders no gaps panel when the API reports none", async () => {
    render(<Providers />);
    await tile("Leads");
    // Otherwise an empty "no gaps" panel becomes permanent furniture on every dashboard.
    expect(screen.queryByRole("heading", { name: "What is not measured yet" })).toBeNull();
  });
});

/* --- the panels the tile row must not have displaced ------------------------- */

describe("the onboarding panels", () => {
  it("leads with 'Name your business' for an account that has none", async () => {
    onboarding = { body: { hasBusiness: false, onboarded: false, name: null, website: null } };
    render(<Providers />);

    expect(
      await screen.findByRole("heading", { name: "Name your business" }),
    ).toBeInTheDocument();
    // Onboarding cannot help here: the confirm route writes to a business that does not
    // exist, so offering it would be offering the one action that must 409.
    expect(screen.queryByRole("heading", { name: "Onboard your business" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Start a run" })).toBeInTheDocument();
  });

  it("leads with 'Onboard your business' for a business with no confirmed profile", async () => {
    onboarding = { body: { hasBusiness: true, onboarded: false, name: "Ada", website: null } };
    render(<Providers />);

    expect(
      await screen.findByRole("heading", { name: "Onboard your business" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Name your business" })).toBeNull();
    // And the run panel warns rather than pretending a run would work.
    expect(screen.getByRole("heading", { name: "Start a run" })).toBeInTheDocument();
    expect(screen.getByText(/A run needs the profile above first/)).toBeInTheDocument();
  });

  it("shows neither panel once the business is onboarded", async () => {
    render(<Providers />);
    await tile("Leads");

    expect(screen.queryByRole("heading", { name: "Onboard your business" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Name your business" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Start a run" })).toBeInTheDocument();
  });

  it("shows neither panel while the onboarding read is still in flight", async () => {
    // A flashed "onboard first" is a setup prompt shown to a business that onboarded
    // months ago, which is why the state starts as unknown rather than as false.
    onboarding = { ok: false, status: 500, body: null };
    render(<Providers />);
    await tile("Leads");

    expect(screen.queryByRole("heading", { name: "Onboard your business" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Name your business" })).toBeNull();
  });
});

/* --- the normaliser, on its own ---------------------------------------------- */

describe("normalizeSummary", () => {
  it("collapses a missing key to null rather than to zero", async () => {
    // The realistic drift: the endpoint ships without one field, or renames it. `?? 0`
    // is the fix someone reaches for when the tile renders blank, and it turns a gap
    // into a measurement.
    const normalized = normalizeSummary({ runsTotal: 4 });
    expect(normalized.runsTotal).toBe(4);
    expect(normalized.clicksTotal).toBeNull();
    expect(normalized.leadsTotal).toBeNull();
    expect(normalized.spendUsd).toBeNull();
    expect(normalized.clicksByChannel).toEqual([]);
    expect(normalized.gaps).toEqual([]);
    // Not a claim we may make from an absent field: "partial audit" is a statement
    // about the crawl.
    expect(normalized.seoTruncated).toBe(false);
  });

  it("rejects a non-finite number", async () => {
    // JSON cannot carry NaN, but a serialiser that emits `Infinity` or a numeric string
    // can, and both would render as "NaN" or "Infinity" beside real figures.
    expect(normalizeSummary({ clicksTotal: Number.NaN }).clicksTotal).toBeNull();
    expect(normalizeSummary({ clicksTotal: "1240" }).clicksTotal).toBeNull();
  });

  it("drops a channel row it cannot render but keeps an unknown channel name", async () => {
    const rows = normalizeSummary({
      clicksByChannel: [
        { channel: "linkedin", clicks: 3 },
        // Would have reached the screen as "Mastodon undefined".
        { channel: "mastodon" },
        // §11: enum consumers tolerate values they do not recognise.
        { channel: "mastodon", clicks: 9 },
      ],
    }).clicksByChannel;
    expect(rows).toEqual([
      { channel: "linkedin", clicks: 3 },
      { channel: "mastodon", clicks: 9 },
    ]);
  });

  it("keeps only real gap strings", async () => {
    expect(normalizeSummary({ gaps: ["real", "", "  ", 7, null] }).gaps).toEqual(["real"]);
  });
});

describe("topChannel", () => {
  it("picks the largest, and the first of a tie", async () => {
    expect(topChannel([])).toBeNull();
    expect(
      topChannel([
        { channel: "a", clicks: 1 },
        { channel: "b", clicks: 9 },
      ]),
    ).toEqual({ channel: "b", clicks: 9 });
    // Stable, so the headline does not flip between two equal channels on every reload.
    expect(
      topChannel([
        { channel: "a", clicks: 9 },
        { channel: "b", clicks: 9 },
      ]),
    ).toEqual({ channel: "a", clicks: 9 });
  });
});

/* --- the row does not force a horizontal scroll ----------------------------- */

it("wraps the tiles rather than laying them out in one fixed row", async () => {
  render(<Providers />);
  const list = (await tile("Leads")).parentElement;
  // jsdom computes no layout, so the assertion is on the mechanism: a wrapping grid
  // that starts at one column, plus `min-w-0` on the cell so a long channel name
  // shrinks instead of pushing the page sideways.
  expect(list).toHaveClass("grid", "grid-cols-1", "sm:grid-cols-2");
  expect(await tile("Leads")).toHaveClass("min-w-0");
});

/* --- the API is asked once, and for the right thing ------------------------- */

it("asks for the summary without a business id", async () => {
  render(<Providers />);
  await tile("Leads");

  const urls = vi
    .mocked(globalThis.fetch)
    .mock.calls.map((call) => String(call[0]))
    .filter((url) => url.includes("/api/v1/dashboard"));

  expect(urls).toHaveLength(1);
  // FastAPI ignores an unknown query parameter silently, so a `businessId` that appeared
  // to work would be a complete cross-tenant read no test would notice. The business is
  // resolved from the session.
  expect(urls[0]).not.toMatch(/business/i);
});

it("waits for the read rather than showing an empty dashboard", async () => {
  let release: (() => void) | undefined;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const base = globalThis.fetch;
  globalThis.fetch = vi.fn(async (input: unknown, init?: RequestInit) => {
    if (String(input).includes("/api/v1/dashboard")) await held;
    return base(input as RequestInfo, init);
  }) as unknown as typeof fetch;

  render(<Providers />);
  // Loading is its own state: the unmeasured copy here would tell a business with 1,240
  // clicks it has none, for as long as the request takes.
  expect(await screen.findByText("Reading your numbers…")).toBeInTheDocument();
  expect(screen.queryByText("not measured")).toBeNull();

  release?.();
  await waitFor(async () => expect(await tile("Tracked clicks")).toHaveTextContent("1,240"));
});

it("shows no KPI tiles, and no failure, to an account with no business", async () => {
  // The endpoint sits behind `current_business`, which 409s when the account owns no
  // business — so before this the first thing such an owner saw on their own dashboard
  // was a red, assertively-announced "This account has no business yet" above the very
  // panel that exists to fix it, beside a "Try again" button that could never succeed.
  onboarding = { body: { hasBusiness: false, onboarded: false, name: null, website: null } };
  // The state the suite could not previously express: no business AND the 409 the
  // endpoint really returns for one. The existing hasBusiness:false test paired it with
  // a fully-measured 200 body, which cannot happen in production — no business, no
  // metrics.
  dashboard = {
    ok: false,
    status: 409,
    body: {
      detail: {
        code: "no_business",
        message: "This account has no business yet. Complete onboarding first.",
      },
    },
  };

  render(<Providers />);

  // The panel that CAN help is there.
  expect(await screen.findByText("Name your business")).toBeInTheDocument();
  // The one that cannot is not, in any form.
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByText("Where you stand")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /try loading your numbers again/i }),
  ).not.toBeInTheDocument();
});
