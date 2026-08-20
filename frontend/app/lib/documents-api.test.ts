/**
 * What a document's status MEANS on screen.
 *
 * The backend's own docstring for `DocumentOut` says it: "`status` and `chunkCount` are
 * both here because either alone can mislead". These tests hold the frontend to that —
 * every case below is one where the wrong sentence sends a customer to the wrong action
 * and nothing on the screen looks broken while it happens.
 */

import { describe, expect, it } from "vitest";

import { documentTone, statusExplanation, statusTone, type Document } from "./documents-api";

function doc(over: Partial<Document> = {}): Document {
  return {
    id: "d1",
    filename: "price-list.pdf",
    kind: "pdf",
    status: "indexed",
    chunkCount: 12,
    extractionNote: null,
    createdAt: "2026-08-20T09:00:00Z",
    ...over,
  };
}

describe("statusExplanation", () => {
  /**
   * The load-bearing one. `no_text` and `failed` are two different jobs for the customer:
   * one is "this is a scan, run OCR or export it differently", the other is "our side did
   * not finish, try again or tell us". Collapsing them into one "didn't work" is exactly
   * how a support ticket gets written for a file the customer could have fixed in a
   * minute — and a shared message looks perfectly fine on the screen.
   */
  it("does not collapse no_text and failed into the same message", () => {
    const noText = statusExplanation(doc({ status: "no_text", chunkCount: 0 }));
    const failed = statusExplanation(doc({ status: "failed", chunkCount: 0 }));

    expect(noText).not.toBe(failed);
    // The action, not just the wording, has to differ: one points at OCR.
    expect(noText).toMatch(/OCR/i);
    expect(failed).not.toMatch(/OCR/i);
  });

  it("tells a no_text customer it is their file and what to do about it", () => {
    expect(statusExplanation(doc({ status: "no_text", chunkCount: 0 }))).toMatch(
      /no readable text/i,
    );
  });

  /**
   * `indexed` with zero chunks is a state the API explicitly documents as reachable — the
   * backend calls it "a scan". Nothing about the row looks wrong, so if this sentence
   * claims the document is searchable the customer has been told the agent can quote a
   * file it cannot read a word of, and will never know why the answers are thin.
   */
  it("does not call an indexed document with no chunks searchable", () => {
    const said = statusExplanation(doc({ status: "indexed", chunkCount: 0 }));
    expect(said).not.toMatch(/searchable/i);
    // And it must not read as an empty success either.
    expect(said).not.toMatch(/^Searchable/);
    expect(said.length).toBeGreaterThan(0);
  });

  it("reports the passage count for a document that really is searchable", () => {
    expect(statusExplanation(doc({ status: "indexed", chunkCount: 12 }))).toBe(
      "Searchable — 12 passages the agent can quote.",
    );
  });

  it("says passage, not passages, when there is exactly one", () => {
    expect(statusExplanation(doc({ status: "indexed", chunkCount: 1 }))).toContain("1 passage the");
  });

  /**
   * The note is the API's own words about this specific file — "3 of 40 passages were
   * already indexed", or the name of the missing extractor package. A generic sentence
   * derived from the status is strictly less useful, so the note must win.
   */
  it("prefers the API's own note over anything derived from the status", () => {
    const said = statusExplanation(
      doc({
        status: "failed",
        chunkCount: 0,
        extractionNote: "This deployment cannot read pdf files: the 'pypdf' package is not installed.",
      }),
    );
    expect(said).toContain("pypdf");
  });

  it("says a pending document is still working rather than that it failed", () => {
    const said = statusExplanation(doc({ status: "pending", chunkCount: 0 }));
    expect(said).toBe("Still indexing.");
  });

  /** Same reason as `runStateTone`: the server owns this vocabulary and can grow it. */
  it("does not throw on a status it has never heard of", () => {
    expect(() => statusExplanation(doc({ status: "quarantined", chunkCount: 0 }))).not.toThrow();
  });
});

describe("statusTone", () => {
  it("distinguishes a fixable file from a broken one", () => {
    // `no_text` is the customer's to fix, so it is a warning, not our error.
    expect(statusTone("no_text")).toBe("warn");
    expect(statusTone("failed")).toBe("err");
    expect(statusTone("no_text")).not.toBe(statusTone("failed"));
  });

  it("only paints a document green when it is actually indexed", () => {
    expect(statusTone("indexed")).toBe("ok");
    for (const status of ["no_text", "failed", "pending", "quarantined"]) {
      expect(statusTone(status)).not.toBe("ok");
    }
  });

  it("falls through to muted for an unknown status instead of throwing", () => {
    expect(statusTone("quarantined")).toBe("muted");
    expect(statusTone("")).toBe("muted");
  });
});

describe("documentTone", () => {
  it("does not paint an indexed document green when it holds no passages", () => {
    // The sentence next to this pill says "nothing here the agent can quote". A green
    // pill contradicts it, and the half a customer reads is the colour.
    expect(documentTone(doc({ status: "indexed", chunkCount: 0 }))).toBe("warn");
  });

  it("agrees with no_text, because it is the same fact by a different route", () => {
    // Both mean "we read the file and there is nothing quotable in it", which is the
    // customer's to fix. Neither is a failure, so neither is `err`.
    expect(documentTone(doc({ status: "indexed", chunkCount: 0 }))).toBe(
      documentTone(doc({ status: "no_text", chunkCount: 0 })),
    );
  });

  it("is green for a document that really is searchable", () => {
    expect(documentTone(doc({ status: "indexed", chunkCount: 7 }))).toBe("ok");
  });

  it("still reports a failure as a failure", () => {
    expect(documentTone(doc({ status: "failed", chunkCount: 0 }))).toBe("err");
  });
});
