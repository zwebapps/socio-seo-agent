/**
 * Which sidebar item is "current".
 *
 * The dashboard is the case that has to be special-cased and the one a naive prefix
 * match gets wrong: `/` is a prefix of every path in the app, so `startsWith` would
 * highlight "Dashboard" on every screen alongside the screen you are actually on. And a
 * detail page whose section highlights nothing reads as a screen outside the app, so
 * `/runs/{id}` has to light up "Runs".
 */

import { describe, expect, it } from "vitest";

import { isCurrent } from "@/app/components/app-nav";

describe("isCurrent", () => {
  it("matches the marketing root only exactly", () => {
    // `/` is the public page now, but the rule still matters: as a PREFIX it matches
    // every path in the app, so any item pointing at it would light up everywhere.
    expect(isCurrent("/", "/")).toBe(true);
    expect(isCurrent("/content", "/")).toBe(false);
    expect(isCurrent("/runs/abc", "/")).toBe(false);
  });

  it("matches the dashboard at its own path", () => {
    expect(isCurrent("/dashboard", "/dashboard")).toBe(true);
    expect(isCurrent("/content", "/dashboard")).toBe(false);
  });

  it("matches a section exactly", () => {
    expect(isCurrent("/content", "/content")).toBe(true);
  });

  it("matches a detail page to its section", () => {
    // Otherwise the run page highlights nothing and looks like it left the app.
    expect(isCurrent("/runs/2f6c-1234", "/runs")).toBe(true);
    expect(isCurrent("/developer/models", "/developer/models")).toBe(true);
  });

  it("does not match a section that merely shares a prefix", () => {
    // `/business` must not light up when you are on `/businesses-elsewhere`. The
    // trailing slash in the prefix check is what prevents it.
    expect(isCurrent("/businessplan", "/business")).toBe(false);
  });

  it("matches nothing when there is no pathname", () => {
    expect(isCurrent(null, "/")).toBe(false);
  });
});
