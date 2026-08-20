/**
 * The connections screen: four failures that type-checking cannot catch.
 *
 * **Recomputed usability.** The server derives `usable`/`unusableReason` from the same
 * function the publish actuator asks. A client that decided for itself — by comparing
 * `expiresAt` to the clock, say, or by reading `status` — would eventually disagree, and
 * the disagreement runs the wrong way: an owner reads a healthy account while their posts
 * go nowhere. So the suite hands the screen a payload whose fields CONTRADICT each other
 * (`status: "connected"`, an expiry a year away, `usable: false`) and insists the server's
 * verdict is what appears.
 *
 * **A simulated connection rendered as a real one.** Every provider behind this is
 * `FakeOAuthProvider` today, so `fake` is true almost everywhere, and a screen that drops
 * that word has an owner believing their Instagram is live. Asserted on the row, on the
 * capability card and on the panel that ends a connect attempt.
 *
 * **App Review implied away.** Connecting is not the last step for Facebook, Instagram,
 * LinkedIn or TikTok, and a screen that only says "Connect" invites the opposite
 * conclusion. The test insists the queue is named as somebody else's and measured in
 * weeks.
 *
 * **A connect button offered before it can work.** With no credential key the API answers
 * 503 — after the customer has been sent to a consent screen. The refusal has to be on the
 * page BEFORE the button, and the button has to be inert.
 *
 * No test here touches the network: `fetch` is stubbed per case.
 */

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConnectionsPage from "@/app/connections/page";
import type {
  ConnectStart,
  Connection,
  ConnectionList,
  CredentialStorage,
  OAuthStatus,
} from "@/app/lib/connections-api";

/** `platform_oauth.oauth_status()`'s real sentence, which the screen renders verbatim. */
const OAUTH_MESSAGE =
  "No real platform OAuth adapter is implemented, so every connection is made against " +
  "FakeOAuthProvider (no network) and nothing can be published outside this process. " +
  "Publishing to facebook, instagram, linkedin, tiktok is gated on per-platform App " +
  "Review -- see docs/CHANNELS.md sections 2-3.";

/** `NotConfiguredCipher`'s reason, which names the variable and what to set it to. */
const NO_KEY_MESSAGE =
  "PLATFORM_CREDENTIAL_KEY is not set, so a platform credential cannot be encrypted and " +
  "will not be stored. Set it to a 32-byte base64 or hex key in staging and production, " +
  "or to 'ephemeral' for local development (in-process, not durable).";

const EPHEMERAL_MESSAGE =
  "Platform credentials are held in this process and the database stores only a handle. " +
  "Nothing survives a restart -- development only.";

const PLATFORMS = [
  "facebook",
  "instagram",
  "linkedin",
  "tiktok",
  "youtube",
  "google_business",
];

function oauth(over: Partial<OAuthStatus> = {}): OAuthStatus {
  return {
    platforms: PLATFORMS,
    realProviders: [],
    usingFakeProviders: true,
    blockedOnAppReview: ["facebook", "instagram", "linkedin", "tiktok"],
    message: OAUTH_MESSAGE,
    ...over,
  };
}

function storage(over: Partial<CredentialStorage> = {}): CredentialStorage {
  return {
    scheme: "v1.ephemeral",
    protectsAtRest: true,
    canStoreCredentials: true,
    message: EPHEMERAL_MESSAGE,
    ...over,
  };
}

function connection(over: Partial<Connection> = {}): Connection {
  return {
    platform: "linkedin",
    externalAccountId: "fake-account",
    externalAccountName: "Fake Account",
    scopes: ["w_member_social"],
    status: "connected",
    expiresAt: "2027-01-01T10:00:00+00:00",
    credentialHint: "fake…ln-1",
    credentialScheme: "v1.ephemeral",
    hasCredential: true,
    fake: true,
    usable: true,
    unusableReason: null,
    needsRenewal: false,
    ...over,
  };
}

function list(over: Partial<ConnectionList> = {}): ConnectionList {
  return {
    connections: [],
    oauth: oauth(),
    credentialStorage: storage(),
    ...over,
  };
}

/** The `POST /connect` body, which is fake for every platform today. */
function started(over: Partial<ConnectStart> = {}): ConnectStart {
  return {
    platform: "linkedin",
    authorizationUrl:
      "https://fake-oauth.invalid/authorize?client_id=fake-linkedin-app&state=abc",
    scopes: ["w_member_social"],
    fake: true,
    ...over,
  };
}

/**
 * The wire, as a queue of responses.
 *
 * A queue rather than one canned body because the screen makes several calls in one
 * scenario — list, then connect, then list again — and a single stub would answer the
 * `POST` with the list payload and pass for the wrong reason.
 */
type Reply = { ok: boolean; status: number; body: unknown };

let replies: Reply[] = [];
let requests: { url: string; method: string }[] = [];

function reply(body: unknown, over: Partial<Reply> = {}): Reply {
  return { ok: true, status: 200, body, ...over };
}

beforeEach(() => {
  replies = [];
  requests = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      requests.push({ url: String(url), method: init?.method ?? "GET" });
      // The LAST reply repeats, so a scenario only has to enumerate the calls it cares
      // about — a poll or a reload past the end of the queue answers the same way rather
      // than throwing somewhere unrelated to the assertion.
      const next = (replies.length > 1 ? replies.shift() : replies[0]) as Reply;
      return Promise.resolve({
        ok: next.ok,
        status: next.status,
        json: () => Promise.resolve(next.body),
      } as unknown as Response);
    }),
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

async function mount(...queue: Reply[]) {
  replies = queue;
  render(<ConnectionsPage />);
  await settle();
}

/* ------------------------------------------------------------------------- */

describe("the server owns the verdict", () => {
  /**
   * The contradiction case, and the reason this suite exists. `status: "connected"` with
   * an expiry a year out is exactly what a client-side rule would call healthy; the
   * server says otherwise, because the row holds no credential. The server wins, and its
   * own sentence is what appears.
   */
  it("renders the API's refusal even when the row's other fields look healthy", async () => {
    await mount(
      reply(
        list({
          connections: [
            connection({
              status: "connected",
              expiresAt: "2027-01-01T10:00:00+00:00",
              hasCredential: false,
              usable: false,
              unusableReason: "the linkedin connection holds no credential",
            }),
          ],
        }),
      ),
    );

    expect(
      screen.getByText(/Nothing can be published on this: the linkedin connection holds no credential/),
    ).toBeInTheDocument();
    expect(screen.getByText("not usable")).toBeInTheDocument();
    // And no invented verdict of its own.
    expect(screen.queryByText("ready to publish")).not.toBeInTheDocument();
  });

  /**
   * The mirror image: a row the server calls usable is rendered as usable even though its
   * stored status is the one a client-side rule would treat as broken. Without this, the
   * previous test could pass on a screen that simply always says "not usable".
   */
  it("renders a usable connection as usable, whatever the stored status says", async () => {
    await mount(
      reply(
        list({
          connections: [connection({ status: "expired", usable: true, unusableReason: null })],
        }),
      ),
    );

    expect(screen.getByText("ready to publish")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing can be published on this/)).not.toBeInTheDocument();
    // The stored status is still reported — it is a fact about the row, and hiding it
    // would make the screen and a SQL-level report disagree.
    expect(screen.getByText(/stored status expired/)).toBeInTheDocument();
  });

  /**
   * `needsRenewal` is the server's "publishing on this is a race" flag, and it is a third
   * state rather than a shade of the other two: the credential still works, and reconnecting
   * now is cheap where discovering it mid-publish is not.
   */
  it("distinguishes an expiring credential from an unusable one", async () => {
    await mount(reply(list({ connections: [connection({ needsRenewal: true })] })));

    expect(screen.getByText("expiring")).toBeInTheDocument();
    expect(screen.getByText(/publishing on it is a race/)).toBeInTheDocument();
    expect(screen.queryByText("not usable")).not.toBeInTheDocument();
  });
});

describe("a simulated connection says so", () => {
  /** On the row, which is where somebody scanning the list will be. */
  it("marks a connection made against the fake provider as simulated", async () => {
    await mount(reply(list({ connections: [connection()] })));

    expect(screen.getByText("simulated")).toBeInTheDocument();
    expect(
      screen.getByText(/this grant came from the built-in fake provider, not from LinkedIn/),
    ).toBeInTheDocument();
    expect(screen.getByText(/reaches no platform and no network/)).toBeInTheDocument();
  });

  /** And not on a real one, so the word keeps meaning something. */
  it("does not call a real connection simulated", async () => {
    await mount(
      reply(
        list({
          connections: [connection({ fake: false })],
          oauth: oauth({ usingFakeProviders: false, realProviders: ["linkedin"] }),
        }),
      ),
    );

    expect(screen.queryByText("simulated")).not.toBeInTheDocument();
    expect(screen.queryByText("simulated providers")).not.toBeInTheDocument();
    expect(screen.getByText("live providers")).toBeInTheDocument();
  });

  /**
   * The end of a connect attempt. A fake authorisation URL points at a domain RFC 2606
   * reserves so it can never resolve, so offering it as a link would read as a broken
   * connection rather than an absent one — and would be the one place on this screen where
   * a simulation is dressed as the real thing.
   */
  it("offers no link for a simulated authorisation, and says nothing was connected", async () => {
    const user = userEvent.setup();
    await mount(reply(list()), reply(started()));

    await user.click(screen.getByRole("button", { name: "Start connecting a LinkedIn account" }));
    await settle();

    expect(screen.getByText(/Nothing was sent anywhere and no account was connected/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Continue to LinkedIn/ })).not.toBeInTheDocument();
    // The address is still shown — an operator needs to see what would have been sent —
    // but as text, not as a destination.
    expect(screen.getByText(/fake-oauth\.invalid/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /fake-oauth/ })).not.toBeInTheDocument();
    // What was asked for travels with it, because a token granted a subset of these is
    // the usual reason a publish fails long after the connection looked fine.
    expect(screen.getByText(/Permissions requested: w_member_social/)).toBeInTheDocument();
  });

  /** A real authorisation DOES become a link, or the branch above would be untested. */
  it("links to a real authorisation URL", async () => {
    const user = userEvent.setup();
    await mount(
      reply(list({ oauth: oauth({ usingFakeProviders: false }) })),
      reply(
        started({
          fake: false,
          authorizationUrl: "https://www.linkedin.com/oauth/v2/authorization?state=abc",
        }),
      ),
    );

    await user.click(screen.getByRole("button", { name: "Start connecting a LinkedIn account" }));
    await settle();

    const link = screen.getByRole("link", { name: "Continue to LinkedIn" });
    expect(link).toHaveAttribute("href", "https://www.linkedin.com/oauth/v2/authorization?state=abc");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });
});

describe("App Review is somebody else's queue", () => {
  /**
   * The honest statement, asserted on the parts that carry the meaning: whose queue it is,
   * how long it takes, and that connecting is not a substitute for it. A screen that only
   * renders the word "App Review" has said nothing an owner can plan around.
   */
  it("names the platforms, the weeks, and that connecting does not stand in for it", async () => {
    await mount(reply(list()));

    expect(screen.getByText(/Facebook, Instagram, LinkedIn and TikTok/)).toBeInTheDocument();
    expect(screen.getByText(/two to six weeks/)).toBeInTheDocument();
    expect(screen.getByText(/does not enter it, shorten it or stand in for it/)).toBeInTheDocument();
    expect(screen.getByText(/That queue belongs to them, not to us/)).toBeInTheDocument();
  });

  /** The server's own sentence, rendered rather than paraphrased. */
  it("renders the API's oauth message verbatim", async () => {
    await mount(reply(list()));
    expect(screen.getByText(OAUTH_MESSAGE)).toBeInTheDocument();
  });

  /**
   * And it disappears when nothing is gated, rather than being permanent furniture.
   *
   * Asserted on OUR section and not on the words "App Review": the server's own message
   * names App Review whatever `blockedOnAppReview` holds, and a test that keyed on the
   * phrase would be asserting that the verbatim message had gone.
   */
  it("drops the App Review section when no platform is gated on it", async () => {
    await mount(reply(list({ oauth: oauth({ blockedOnAppReview: [] }) })));

    expect(screen.queryByText("Waiting on App Review")).not.toBeInTheDocument();
    expect(screen.queryByText(/two to six weeks/)).not.toBeInTheDocument();
    // The server's sentence is still there — it is the one thing on this card that is
    // never conditional.
    expect(screen.getByText(OAUTH_MESSAGE)).toBeInTheDocument();
  });
});

describe("credential storage decides whether connecting is offered at all", () => {
  /**
   * The whole point of checking first: the reason is on the page and every connect button
   * is inert. Otherwise a customer authorises an account, the 503 arrives, and a live grant
   * we hold no record of is left behind on their side.
   */
  it("states the refusal and disables connecting when no key is configured", async () => {
    await mount(
      reply(
        list({
          credentialStorage: storage({
            scheme: "unconfigured",
            protectsAtRest: false,
            canStoreCredentials: false,
            message: NO_KEY_MESSAGE,
          }),
        }),
      ),
    );

    expect(screen.getByText(NO_KEY_MESSAGE)).toBeInTheDocument();
    expect(
      screen.getByText(/refused here rather than after the round trip/),
    ).toBeInTheDocument();

    for (const button of screen.getAllByRole("button", { name: /Start connecting/ })) {
      expect(button).toBeDisabled();
    }
    expect(
      screen.getAllByText(/unavailable until credential storage is configured/).length,
    ).toBeGreaterThan(0);
  });

  /** And offers connecting when it IS configured, or the assertion above proves nothing. */
  it("enables connecting when a credential can be stored", async () => {
    await mount(reply(list()));

    for (const button of screen.getAllByRole("button", { name: /Start connecting/ })) {
      expect(button).toBeEnabled();
    }
    expect(screen.getByText(EPHEMERAL_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText(/unavailable until credential storage/)).not.toBeInTheDocument();
  });
});

describe("the credential itself", () => {
  /**
   * Write-only, and asserted as an absence: the hint is four-and-four and nothing longer
   * reaches the screen. `hasCredential` is rendered as a sentence about existence — a
   * "true" printed where a value belongs is how a boolean starts looking like a secret.
   */
  it("shows the mask and never a token, and states an absent credential in words", async () => {
    await mount(
      reply(
        list({
          connections: [
            connection({ credentialHint: "fake…ln-1" }),
            connection({
              platform: "facebook",
              hasCredential: false,
              usable: false,
              status: "revoked",
              unusableReason:
                "the facebook connection was revoked; the business has to authorise it again",
            }),
          ],
        }),
      ),
    );

    expect(screen.getByText("fake…ln-1")).toBeInTheDocument();
    expect(screen.getByText(/No credential is stored for this account/)).toBeInTheDocument();
    // Nothing that looks like a whole fake token, and no raw boolean.
    expect(document.body.textContent).not.toMatch(/fake-access-/);
    expect(document.body.textContent).not.toMatch(/hasCredential/);
    expect(document.body.textContent).not.toMatch(/\btrue\b|\bfalse\b/);
  });
});

describe("the list itself", () => {
  /**
   * Every connectable platform, connected or not. A screen that iterated `connections`
   * alone would show an owner with nothing connected an empty page, which is the state
   * this screen exists to get them out of.
   */
  it("lists every connectable platform even when nothing is connected", async () => {
    await mount(reply(list()));

    for (const label of [
      "Facebook",
      "Instagram",
      "LinkedIn",
      "TikTok",
      "YouTube",
      "Google Business Profile",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByText("not connected")).toHaveLength(PLATFORMS.length);
  });

  /**
   * A held connection for a platform that has left the connectable list is still shown.
   * It is precisely the row an owner needs, because it holds a credential and a live grant
   * on their account and only this screen can disconnect it.
   */
  it("still shows a connection whose platform is no longer connectable", async () => {
    await mount(
      reply(
        list({
          connections: [connection({ platform: "x", externalAccountName: "Legacy account" })],
          oauth: oauth({ platforms: ["linkedin"] }),
        }),
      ),
    );

    expect(screen.getByText(/Legacy account/)).toBeInTheDocument();
    expect(screen.getByText("fake-account")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect the x account" })).toBeInTheDocument();
  });
});

describe("disconnecting", () => {
  /**
   * Confirmed inline, because this revokes a credential at the provider and a stray click
   * costs a re-authorisation the owner has to perform at the platform. And the DELETE is
   * only sent after the confirmation, which is the half a "does the button exist" test
   * would miss.
   */
  it("asks first, then sends the DELETE for that platform", async () => {
    const user = userEvent.setup();
    await mount(
      reply(list({ connections: [connection()] })),
      reply(null, { status: 204 }),
      reply(list()),
    );

    await user.click(screen.getByRole("button", { name: "Disconnect the LinkedIn account" }));
    expect(screen.getByText(/Reconnecting means authorising the account again/)).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);

    await user.click(
      screen.getByRole("button", { name: "Confirm disconnecting the LinkedIn account" }),
    );
    await settle();

    const deletes = requests.filter((r) => r.method === "DELETE");
    expect(deletes).toHaveLength(1);
    expect(deletes[0]?.url).toContain("/api/v1/connections/linkedin");
    expect(screen.getByText(/LinkedIn is disconnected/)).toBeInTheDocument();
  });

  /** And sends nothing when the confirmation is declined. */
  it("sends nothing when the confirmation is declined", async () => {
    const user = userEvent.setup();
    await mount(reply(list({ connections: [connection()] })));

    await user.click(screen.getByRole("button", { name: "Disconnect the LinkedIn account" }));
    await user.click(screen.getByRole("button", { name: "Keep it" }));

    expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
    expect(screen.queryByText(/is disconnected/)).not.toBeInTheDocument();
  });
});

describe("when the API refuses", () => {
  /**
   * A transport or authorisation failure must not read as "you have no accounts": those are
   * opposite facts, and an empty list would have an owner reconnecting an account that is
   * already connected.
   */
  it("shows the API's message rather than an empty list", async () => {
    await mount(
      reply(
        { detail: { code: "no_business", message: "Finish onboarding first." } },
        { ok: false, status: 409 },
      ),
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Finish onboarding first.");
    expect(screen.queryByText("not connected")).not.toBeInTheDocument();
  });

  /**
   * The 503 from a connect attempt is the credential-storage refusal, and its message names
   * the variable and the value. Shown verbatim, in the row that asked for it.
   */
  it("shows a refused connect verbatim, in the row it belongs to", async () => {
    const user = userEvent.setup();
    await mount(
      reply(list()),
      reply(
        { detail: { code: "credential_storage_unavailable", message: NO_KEY_MESSAGE } },
        { ok: false, status: 503 },
      ),
    );

    await user.click(screen.getByRole("button", { name: "Start connecting a TikTok account" }));
    await settle();

    expect(screen.getByRole("alert")).toHaveTextContent(NO_KEY_MESSAGE);
  });
});

describe("accessibility of the signal", () => {
  /**
   * A `Pill` is colour plus text, and on this screen the text is the whole point: "we will
   * not publish on this" has to survive being read in greyscale. So every pill on a
   * populated screen carries words, and the verdict words are among them.
   */
  it("carries the verdict in text and not only in colour", async () => {
    await mount(
      reply(
        list({
          connections: [
            connection(),
            connection({
              platform: "facebook",
              usable: false,
              unusableReason: "the facebook credential expired and has not been renewed",
            }),
          ],
        }),
      ),
    );

    expect(screen.getByText("ready to publish")).toBeInTheDocument();
    expect(screen.getByText("not usable")).toBeInTheDocument();
    expect(screen.getAllByText("simulated").length).toBeGreaterThan(0);
  });

  /**
   * Six identical "Connect" buttons are six identically named controls to a screen-reader
   * user, so each says which account it connects.
   */
  it("names every connect and disconnect control by its platform", async () => {
    await mount(reply(list({ connections: [connection()] })));

    expect(
      screen.getByRole("button", { name: "Start connecting a Facebook account" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reconnect the LinkedIn account" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect the LinkedIn account" })).toBeInTheDocument();
  });
});
