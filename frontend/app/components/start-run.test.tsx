/**
 * The channel picker on the start-run form.
 *
 * Both tests here are aimed at a failure that looks fine on screen. Unticking every
 * channel and submitting would produce posts for all three, because the API reads an
 * empty set as "nobody chose" — a run that did the opposite of what the form appears to
 * ask for, and nothing on screen would say so. And a channel list assembled in click
 * order renders the review tabs in a different order for two people who chose the same
 * channels, which reads as a bug in the review screen rather than here.
 *
 * `fetch` is faked one level below `runs-api.ts`, the same as the runs-list tests, so
 * the request body this form actually sends is what gets asserted.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { StartRunForm } from "@/app/components/start-run";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

function accepted(): Response {
  return {
    ok: true,
    status: 202,
    json: () => Promise.resolve({ runId: "r1", state: "queued" }),
  } as unknown as Response;
}

/** The body of the one POST the form sent. */
function sentBody(): Record<string, unknown> {
  const call = vi.mocked(globalThis.fetch).mock.calls[0];
  if (call === undefined) throw new Error("nothing was sent");
  return JSON.parse(String(call[1]?.body));
}

beforeEach(() => {
  push.mockClear();
  globalThis.fetch = vi.fn().mockResolvedValue(accepted()) as unknown as typeof fetch;
});

it("sends the chosen channels in the order the review screen lists them", async () => {
  const user = userEvent.setup();
  render(<StartRunForm />);

  // Every channel starts selected — the behaviour before the control existed. Untick
  // Facebook, so the surviving pair is deliberately NOT in click order.
  await user.click(screen.getByRole("checkbox", { name: "Facebook" }));
  await user.type(
    screen.getByLabelText("What should the agent work on?"),
    "more local leads",
  );
  await user.click(screen.getByRole("button", { name: "Start a run" }));

  await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledOnce());
  expect(sentBody()).toEqual({ goal: "more local leads", channels: ["linkedin", "instagram"] });
});

it("refuses a run with no channel rather than silently posting to all of them", async () => {
  const user = userEvent.setup();
  render(<StartRunForm />);

  for (const name of ["LinkedIn", "Facebook", "Instagram"]) {
    await user.click(screen.getByRole("checkbox", { name }));
  }
  await user.type(
    screen.getByLabelText("What should the agent work on?"),
    "more local leads",
  );
  await user.click(screen.getByRole("button", { name: "Start a run" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Choose at least one channel to post to.",
  );
  // The refusal is the point: nothing was sent, so nothing was started.
  expect(globalThis.fetch).not.toHaveBeenCalled();
  expect(push).not.toHaveBeenCalled();
});
