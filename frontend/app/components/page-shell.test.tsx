/**
 * The layout/measure split, which is the whole reason there are two containers.
 *
 * Written because the failure mode is invisible in a screenshot at one width: a page
 * that uses `Shell` and forgets `Prose` looks fine at 1024px and has 1400px-wide
 * paragraphs at 1512px, which is the exact problem the shell was introduced to fix.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Prose, Shell } from "./page-shell";

describe("Shell", () => {
  it("fills the viewport rather than centring in a narrow column", () => {
    render(<Shell>content</Shell>);

    const shell = screen.getByRole("main");
    // `w-full` is the part that matters: without it the max-width is a ceiling on a
    // shrink-wrapped box and the page stays narrow however wide the screen is.
    expect(shell.className).toContain("w-full");
    expect(shell.className).toContain("max-w-[1800px]");
  });

  it("keeps padding at every breakpoint, so content never touches the edge", () => {
    render(<Shell>content</Shell>);

    const className = screen.getByRole("main").className;
    expect(className).toContain("px-6");
    expect(className).toContain("lg:px-10");
    expect(className).toContain("xl:px-14");
  });

  it("renders as a section when it is not the page", () => {
    // A page has exactly one `main`. A nested shell that emitted a second one would be
    // an a11y defect that only a landmark audit would catch.
    render(<Shell as="section">inner</Shell>);

    expect(screen.queryByRole("main")).toBeNull();
  });
});

describe("Prose", () => {
  it("constrains the reading measure in characters, not pixels", () => {
    render(<Prose>a paragraph</Prose>);

    // `ch` rather than `px`, because it is a statement about the TEXT: a pixel width
    // silently stops being ~70 characters the moment anyone touches the type scale.
    expect(screen.getByText("a paragraph").className).toContain("max-w-[70ch]");
  });
});
