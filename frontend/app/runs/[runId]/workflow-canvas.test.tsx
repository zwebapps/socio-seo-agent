/**
 * Folding the event log into per-node state.
 *
 * `derive` is the only part of the canvas with logic in it, and every case below is a
 * failure that would render as a plausible-looking graph. A per-node cost taken as the
 * raw cumulative total shows the LAST node costing the whole run. A resumed run
 * re-emits `started`, so keeping the first one measures a duration that spans the gap
 * where the process was dead. And events arriving out of order — which polling after a
 * dropped stream genuinely does — would let a late `started` overwrite the `done` that
 * followed it, so a finished node renders as still running forever.
 */

import { describe, expect, it } from "vitest";

import { derive, PIPELINE } from "@/app/runs/[runId]/workflow-canvas";

type Event = Parameters<typeof derive>[0][number];

let seq = 0;
function event(node: string, status: Event["status"], over: Partial<Event> = {}): Event {
  seq += 1;
  return {
    seq,
    node,
    status,
    payload: {},
    at: "2026-08-21T10:00:00.000Z",
    ...over,
  };
}

function pick(nodes: ReturnType<typeof derive>, node: string) {
  const found = nodes.find((n) => n.node === node);
  if (!found) throw new Error(`${node} is not in PIPELINE`);
  return found;
}

describe("derive", () => {
  it("covers every node the backend runs", () => {
    // The timeline's own hand-maintained list had drifted to eight nodes, silently
    // dropping CONVERT, EXPORT and MEASURE — so the screen stopped showing the two
    // nodes that only run after a human approves. This is that guard.
    const names = PIPELINE.map((p) => p.node);
    for (const required of ["INTAKE", "CONVERT", "REVIEW", "EXPORT", "MEASURE"]) {
      expect(names).toContain(required);
    }
    expect(names).toHaveLength(11);
  });

  it("reports a node's OWN cost, not the running total", () => {
    seq = 0;
    const nodes = derive(
      [
        event("INTAKE", "started"),
        event("INTAKE", "done", { payload: { cost_usd: "0.0100" } }),
        event("HARVEST", "started"),
        event("HARVEST", "done", { payload: { cost_usd: "0.0250" } }),
      ],
      null,
    );

    expect(pick(nodes, "INTAKE").usd).toBeCloseTo(0.01, 6);
    // 0.0250 cumulative minus 0.0100 already spent. Taken raw, HARVEST would appear to
    // have cost the whole run.
    expect(pick(nodes, "HARVEST").usd).toBeCloseTo(0.015, 6);
  });

  it("shows no cost at all for a node that made no model call", () => {
    seq = 0;
    // "$0.0000" reads as "this node was free", which is a claim. INTAKE and HARVEST are
    // engines-only by design, so "not measured" is the honest rendering.
    const nodes = derive([event("HARVEST", "started"), event("HARVEST", "done")], null);

    expect(pick(nodes, "HARVEST").usd).toBeNull();
    expect(pick(nodes, "HARVEST").status).toBe("done");
  });

  it("never reports a negative cost when a resumed run reports a lower total", () => {
    seq = 0;
    const nodes = derive(
      [
        event("GENERATE", "started"),
        event("GENERATE", "done", { payload: { cost_usd: "0.0500" } }),
        // A resume can restart the accumulator, so the next total is LOWER.
        event("VALIDATE", "started"),
        event("VALIDATE", "done", { payload: { cost_usd: "0.0100" } }),
      ],
      null,
    );

    expect(pick(nodes, "VALIDATE").usd).toBe(0);
  });

  it("measures duration from the LATEST start, not the abandoned one", () => {
    seq = 0;
    const nodes = derive(
      [
        event("GENERATE", "started", { at: "2026-08-21T10:00:00.000Z" }),
        // The process died here. The run resumed twenty minutes later.
        event("GENERATE", "started", { at: "2026-08-21T10:20:00.000Z" }),
        event("GENERATE", "done", { at: "2026-08-21T10:20:03.000Z" }),
      ],
      null,
    );

    // Three seconds of work, not twenty minutes of being dead.
    expect(pick(nodes, "GENERATE").ms).toBe(3000);
  });

  it("orders by seq, so a late arrival cannot un-finish a node", () => {
    seq = 0;
    const started = event("REPACK", "started");
    const done = event("REPACK", "done");
    // Polling after a dropped stream delivers a batch, and nothing guarantees arrival
    // order. Sorted by `seq`, this is a finished node either way.
    const nodes = derive([done, started], null);

    expect(pick(nodes, "REPACK").status).toBe("done");
  });

  it("marks the node the run says it is on as running, even with no events yet", () => {
    seq = 0;
    const nodes = derive([], "HARVEST");

    expect(pick(nodes, "HARVEST").status).toBe("running");
    expect(pick(nodes, "INTAKE").status).toBe("pending");
  });

  it("carries a failure's message so the inspector can show it", () => {
    seq = 0;
    const nodes = derive(
      [
        event("HARVEST", "started"),
        event("HARVEST", "failed", { payload: { error: "serp provider timed out" } }),
      ],
      null,
    );

    expect(pick(nodes, "HARVEST").status).toBe("failed");
    expect(pick(nodes, "HARVEST").error).toBe("serp provider timed out");
  });

  it("distinguishes skipped from pending", () => {
    seq = 0;
    // A resumed run skips what the checkpoint says already ran. Rendering that as
    // `pending` would suggest work still to come that will never happen.
    const nodes = derive([event("INTAKE", "skipped")], null);

    expect(pick(nodes, "INTAKE").status).toBe("skipped");
  });

  it("leaves everything pending for a run that has not started", () => {
    seq = 0;
    const nodes = derive([], null);

    expect(nodes.every((n) => n.status === "pending")).toBe(true);
    expect(nodes.every((n) => n.usd === null && n.ms === null)).toBe(true);
  });
});
