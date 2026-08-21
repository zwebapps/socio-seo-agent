/**
 * Where a role lands, and the open redirect that used to live in `?next=`.
 *
 * The landing map is tested because the bug it replaces was invisible: an owner signing
 * in was sent to the platform operator's model-routing screen and shown "Not available
 * on this account", which reads as a broken account rather than a wrong redirect.
 *
 * `safeNext` is tested harder than it looks like it needs, because the moment right
 * after somebody authenticates is the most valuable moment to phish them, and every
 * case below is a real bypass of a naive `startsWith("/")` check.
 */

import { describe, expect, it } from "vitest";

import { landingFor, safeNext } from "@/app/lib/roles";

describe("landingFor", () => {
  it("sends a business owner to the business dashboard", () => {
    expect(landingFor("owner")).toBe("/");
  });

  it("sends a platform admin to the operator screens", () => {
    expect(landingFor("platform_admin")).toBe("/developer/models");
  });

  it("sends a member to the business dashboard, never the operator's", () => {
    // `member` exists in the enum and the DB constraint and nothing implements it. Until
    // it means something it must not resolve to the operator console by accident.
    expect(landingFor("member")).toBe("/");
  });

  it("falls back to the business dashboard for a role this build has not heard of", () => {
    // A server that has grown a new role must not strand the person on a blank screen.
    expect(landingFor("auditor")).toBe("/");
    expect(landingFor(null)).toBe("/");
    expect(landingFor(undefined)).toBe("/");
  });
});

describe("safeNext", () => {
  it("keeps a relative path on this site", () => {
    expect(safeNext("/runs")).toBe("/runs");
    expect(safeNext("/runs?tab=social")).toBe("/runs?tab=social");
  });

  it("refuses an absolute URL to another origin", () => {
    expect(safeNext("https://evil.example/harvest")).toBeNull();
  });

  it("refuses a protocol-relative URL", () => {
    // `//evil.example` passes `startsWith("/")` and is a DIFFERENT SITE. This is the
    // case the naive check lets through.
    expect(safeNext("//evil.example")).toBeNull();
  });

  it("refuses a backslash-rooted path some browsers normalise off-site", () => {
    expect(safeNext("/\\evil.example")).toBeNull();
  });

  it("treats an absent or empty next as absent", () => {
    expect(safeNext(null)).toBeNull();
    expect(safeNext("")).toBeNull();
  });
});
