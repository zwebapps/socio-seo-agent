/**
 * What the developer console says when it refuses an account.
 *
 * Written for a specific contradiction on `/developer/cost`: the header told an owner
 * these were their own business's numbers, read under row-level security, and the body
 * then told them "these are platform-wide settings — ask whoever operates this
 * installation if you need them changed". There is no setting on that page. The owner was
 * refused a NUMBER, and was answered with advice about changing something that does not
 * exist.
 *
 * So these tests are about one sentence, and they are worth having because the sentence is
 * the whole bug. Two of them assert copy is ABSENT, which is the direction that matters
 * here: a refusal is easy to make polite and very easy to make untrue, and nothing else in
 * the suite would notice the wrong noun coming back.
 *
 * The 403 gate itself is not under test and is not being loosened. `require_admin` stays
 * on the route; `docs/BUILD_ORDER.md` Phase 9 puts this console behind a server-side role
 * check, and making a sentence true by widening an authorisation check is the wrong
 * direction of fix.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/api";

/** Every admin call rejects with this, so a screen renders nothing but its refusal. */
const stub = vi.hoisted(() => ({
  error: new Error("no stub set") as Error,
}));

vi.mock("../lib/admin-api", () => {
  const fail = async (): Promise<never> => {
    throw stub.error;
  };
  return {
    adminApi: {
      cost: fail,
      routes: fail,
      providers: fail,
      catalogue: fail,
      tools: fail,
      sampling: fail,
      promptVersions: fail,
    },
  };
});

import CostPage, { COST_REFUSAL } from "./cost/page";
import ModelsAdminPage from "./models/page";
import { ErrorCard, SETTINGS_REFUSAL } from "./shell";

/**
 * The server's own 403 body. It is route-agnostic on purpose — the API cannot know which
 * screen asked — which is exactly why it must not be the sentence a reader gets on Cost.
 */
const SERVER_403 = new ApiError("forbidden", "Your account cannot change these settings.", 403);

describe("ErrorCard, 403", () => {
  it("defaults to the settings refusal, so a screen that IS settings needs no argument", () => {
    render(
      <ErrorCard
        error={{ code: "forbidden", message: SERVER_403.message }}
        returnTo="/developer/tools"
        onRetry={() => {}}
      />,
    );

    expect(screen.getByText("Not available on this account")).toBeInTheDocument();
    expect(screen.getByText(SETTINGS_REFUSAL)).toBeInTheDocument();
  });

  it("shows the screen's own sentence INSTEAD of the server's, not underneath it", () => {
    render(
      <ErrorCard
        error={{ code: "forbidden", message: SERVER_403.message }}
        returnTo="/developer/cost"
        forbidden={COST_REFUSAL}
        onRetry={() => {}}
      />,
    );

    expect(screen.getByText(COST_REFUSAL)).toBeInTheDocument();
    // Both of these would leave the wrong noun on screen next to the right one.
    expect(document.body.textContent).not.toMatch(/platform-wide settings/i);
    expect(document.body.textContent).not.toMatch(/change these settings/i);
  });

  it("still shows the server's message for a failure that is not a refusal", () => {
    // Suppressing the message is scoped to 403, where it is one fixed string. Everywhere
    // else it is the only account of what actually went wrong.
    render(
      <ErrorCard
        error={{ code: "network", message: "Cannot reach the API at http://localhost:8100." }}
        returnTo="/developer/cost"
        forbidden={COST_REFUSAL}
        onRetry={() => {}}
      />,
    );

    expect(screen.getByText(/cannot reach the api/i)).toBeInTheDocument();
    expect(screen.queryByText(COST_REFUSAL)).toBeNull();
  });
});

describe("the refusal each developer screen actually ships", () => {
  it("Cost refuses a figure, and never calls it a setting", async () => {
    stub.error = SERVER_403;
    render(<CostPage />);

    expect(await screen.findByText("Not available on this account")).toBeInTheDocument();
    expect(screen.getByText(COST_REFUSAL)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/platform-wide settings/i);
    expect(document.body.textContent).not.toMatch(/change these settings/i);
  });

  it("Model routing still says platform-wide settings, because it is one", async () => {
    // The other half of the fix: per-page copy must not quietly blank the three screens
    // where the original sentence was correct.
    stub.error = SERVER_403;
    render(<ModelsAdminPage />);

    expect(await screen.findByText("Not available on this account")).toBeInTheDocument();
    expect(screen.getByText(SETTINGS_REFUSAL)).toBeInTheDocument();
  });

  it("offers no retry and no sign-in link on either, signed in already", async () => {
    // A retry cannot change a role and the login page is a loop for someone who is
    // already authenticated. Asserted here because both screens now share one card.
    stub.error = SERVER_403;
    render(<CostPage />);

    // Waits on the heading rather than the sentence, so a wrong sentence fails the two
    // tests above and not this one.
    await screen.findByText("Not available on this account");
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /sign in/i })).toBeNull();
  });
});
