/**
 * The `/developer` nav's "you are here".
 *
 * Written because it was missing: all four pills rendered identically on every screen,
 * so the nav said where you could go and never where you were.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const pathname = vi.hoisted(() => ({ current: "/developer/tools" }));
vi.mock("next/navigation", () => ({ usePathname: () => pathname.current }));

import { DeveloperNav } from "./nav";

describe("DeveloperNav", () => {
  it("marks the current section for a screen reader, not only visually", () => {
    pathname.current = "/developer/tools";
    render(<DeveloperNav />);

    // `aria-current` is the only signal a screen-reader user gets. Colour and weight
    // are the other two, and neither reaches them.
    expect(screen.getByRole("link", { name: /tool access/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("marks exactly one section, so the nav cannot claim two", () => {
    pathname.current = "/developer/cost";
    render(<DeveloperNav />);

    const marked = screen
      .getAllByRole("link")
      .filter((link) => link.getAttribute("aria-current") === "page");
    expect(marked).toHaveLength(1);
    expect(marked[0]).toHaveAccessibleName(/cost/i);
  });

  it("keeps the parent highlighted on a nested route", () => {
    // `startsWith`, not equality: a future `/developer/tools/harvest` must not
    // un-highlight the whole nav and leave the reader with no location at all.
    pathname.current = "/developer/tools/harvest";
    render(<DeveloperNav />);

    expect(screen.getByRole("link", { name: /tool access/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("marks nothing when the path is not one of its sections", () => {
    // Better than guessing: a highlighted pill on a page you are not on is worse than
    // no highlight, because it is confidently wrong.
    pathname.current = "/dashboard";
    render(<DeveloperNav />);

    expect(
      screen.getAllByRole("link").filter((l) => l.getAttribute("aria-current") === "page"),
    ).toHaveLength(0);
  });
});
