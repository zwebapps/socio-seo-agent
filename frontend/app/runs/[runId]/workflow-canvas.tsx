"use client";

/**
 * The run, drawn as the graph it actually is.
 *
 * The timeline below this is a vertical list of events, and a list is the wrong shape
 * for the thing it describes: the run is a state machine with branches, a revision
 * loop, and a human gate that two of its nodes sit BEHIND. None of that is visible in
 * a list, and the gate is the single most important property of the machine — EXPORT
 * and MEASURE are unreachable without an approval, which is what makes "nothing
 * publishes without a person" structural rather than a promise.
 *
 * Three rules this canvas follows, and each is a decision:
 *
 * **It is READ-ONLY, and that is not a missing feature.** The reference UI this is
 * modelled on is a workflow *builder* — drag a node, connect an edge, create a
 * workflow. Ours is a workflow *runner*: the nodes and edges are fixed in
 * `agents/graph.py::ORDER` and `agents/state_graph.py`, deliberately, so that there is
 * no path that loops forever and no path that fails silently. Drawing drag handles
 * would promise users a pipeline they can author, which they cannot.
 *
 * **Every number on it is measured, not decorated.** Per-node cost is a delta of the
 * cumulative `cost_usd` the graph emits on each `done` event; duration is the gap
 * between that node's `started` and `done`. Where a node produced neither, the chip is
 * absent rather than zero — "0.0000 USD" reads as "this node was free", and "no
 * measurement" is a different fact.
 *
 * **The node list mirrors the backend's `ORDER` and says so.** The timeline's own list
 * had drifted to eight nodes, silently omitting CONVERT, EXPORT and MEASURE — so the
 * screen stopped showing the two nodes that only run after a human approves. A second
 * hand-maintained copy of the pipeline is how that happens, so this one is a single
 * exported constant with the backend file named next to it.
 *
 * The list view below is kept rather than replaced, and not out of caution: a canvas is
 * a poor experience with a screen reader, and the list is the accessible rendering of
 * the same events. This is `aria-hidden` for exactly that reason.
 */

import { useMemo, useState } from "react";

import { Pill, SoftCard } from "@/app/components/soft";

/**
 * The pipeline, in the order the graph runs it.
 *
 * Mirrors `ORDER` in `backend/app/agents/graph.py`. If a node is added there and not
 * here, this canvas silently stops showing it — which is exactly what happened to the
 * timeline list, so keep the two in step.
 */
export const PIPELINE = [
  { node: "INTAKE", title: "Intake", blurb: "Normalising the request" },
  { node: "HARVEST", title: "Harvest", blurb: "Gathering evidence · engines only" },
  { node: "OPPORTUNITY", title: "Opportunity", blurb: "Ranking what is worth writing" },
  { node: "PLAN", title: "Plan", blurb: "Outlining against a keyword" },
  { node: "GENERATE", title: "Generate", blurb: "Writing the article" },
  { node: "CONVERT", title: "Convert", blurb: "The ask, per channel" },
  { node: "VALIDATE", title: "Validate", blurb: "SEO score + claim gate" },
  { node: "REPACK", title: "Repack", blurb: "Adapting per channel" },
  { node: "REVIEW", title: "Review", blurb: "Waiting for a person" },
  { node: "EXPORT", title: "Export", blurb: "Publishing what was approved" },
  { node: "MEASURE", title: "Measure", blurb: "Clicks, share of voice" },
] as const;

/** Nodes that can only run after a human approves. Drawn behind the gate. */
const AFTER_GATE = new Set(["EXPORT", "MEASURE"]);

type Event = {
  seq: number;
  node: string;
  status: "started" | "done" | "failed" | "skipped";
  payload: Record<string, unknown>;
  at: string;
};

type NodeState = {
  node: string;
  title: string;
  blurb: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  /** This node's own cost, as a delta of the cumulative total. `null` when unmeasured. */
  usd: number | null;
  /** Milliseconds between `started` and `done`. `null` when it did not finish. */
  ms: number | null;
  error: string | null;
  at: string | null;
};

export function WorkflowCanvas({
  events,
  currentNode,
  runState,
}: {
  events: Event[];
  currentNode: string | null;
  runState: string;
}) {
  const nodes = useMemo(() => derive(events, currentNode), [events, currentNode]);
  const [selected, setSelected] = useState<string | null>(null);

  const chosen = nodes.find((n) => n.node === selected) ?? null;
  // The node worth showing when nobody has clicked: whatever is happening now, or the
  // last thing that did. An empty inspector on a finished run is a wasted panel.
  const shown =
    chosen ??
    nodes.find((n) => n.status === "running") ??
    [...nodes].reverse().find((n) => n.status !== "pending") ??
    null;

  return (
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,20rem)]">
      {/*
        `aria-hidden` because the timeline list below renders the same events as text.
        A screen-reader user gets the list; duplicating it here as a graph of divs would
        read as the run happening twice.
      */}
      <div aria-hidden className="soft-sunken soft-edge p-5" style={{ borderRadius: "var(--r-md)" }}>
        <ol className="space-y-0">
          {nodes.map((node, index) => (
            <li key={node.node}>
              {AFTER_GATE.has(node.node) && index > 0 && nodes[index - 1]?.node === "REVIEW" && (
                <GateMarker />
              )}
              {index > 0 && !AFTER_GATE.has(node.node) && <Connector />}
              <NodeCard
                node={node}
                selected={shown?.node === node.node}
                onSelect={() => setSelected(node.node)}
              />
            </li>
          ))}
        </ol>
      </div>

      <Inspector node={shown} runState={runState} nodes={nodes} />
    </div>
  );
}

function Connector() {
  return (
    <div className="flex h-6 justify-start pl-6">
      <span className="block w-px" style={{ background: "var(--edge)" }} />
    </div>
  );
}

/**
 * The human gate, drawn as a break in the flow rather than another node.
 *
 * It is not a step the machine performs — it is the point at which the machine stops
 * and cannot continue by itself. Drawing it like the other nodes would make it look
 * like something the agent does.
 */
function GateMarker() {
  return (
    <div className="flex items-center gap-3 py-3 pl-1">
      <span className="block h-px flex-1" style={{ background: "var(--accent)" }} />
      <span
        className="soft-edge px-3 py-1 text-[10px] font-semibold uppercase tracking-wider"
        style={{ borderRadius: "var(--r-pill)", color: "var(--accent)" }}
      >
        A person approves here
      </span>
      <span className="block h-px flex-1" style={{ background: "var(--accent)" }} />
    </div>
  );
}

function NodeCard({
  node,
  selected,
  onSelect,
}: {
  node: NodeState;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className="soft-edge flex w-full items-center gap-3 px-4 py-3 text-left"
      style={{
        borderRadius: "var(--r-sm)",
        background: selected ? "var(--surface-raised)" : "transparent",
        opacity: node.status === "pending" ? 0.5 : 1,
      }}
    >
      <StatusDot status={node.status} />
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-semibold">{node.title}</span>
        <span className="block text-xs" style={{ color: "var(--text-muted)" }}>
          {node.blurb}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-2">
        {/* Absent rather than zero when unmeasured: "$0.0000" reads as "this node was
            free", which is a claim, while showing nothing is the truth. */}
        {node.usd !== null && node.usd > 0 && (
          <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>
            ${node.usd.toFixed(4)}
          </span>
        )}
        {node.ms !== null && (
          <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>
            {formatMs(node.ms)}
          </span>
        )}
        <StatusPill status={node.status} />
      </span>
    </button>
  );
}

function StatusDot({ status }: { status: NodeState["status"] }) {
  const colour =
    status === "done"
      ? "var(--primary)"
      : status === "running"
        ? "var(--accent)"
        : status === "failed"
          ? "var(--err)"
          : "var(--text-faint)";
  return (
    <span
      className="block h-2.5 w-2.5 shrink-0"
      style={{ borderRadius: "var(--r-pill)", background: colour }}
    />
  );
}

function StatusPill({ status }: { status: NodeState["status"] }) {
  if (status === "pending") return null;
  if (status === "done") return <Pill tone="ok">done</Pill>;
  if (status === "running") return <Pill tone="accent">running</Pill>;
  if (status === "failed") return <Pill tone="err">failed</Pill>;
  return <Pill>skipped</Pill>;
}

/**
 * The inspector, and what it is allowed to say.
 *
 * The reference panel this mirrors carries "142 req/min throughput" and a "1.2% error
 * rate". Neither has a source here — we record model calls, not requests per minute —
 * so they are replaced with what IS measured: this node's cost and duration, the run's
 * total, and the error text when there is one. An invented throughput figure on a
 * screen whose whole purpose is showing what the agent really did would poison the rest
 * of the panel.
 */
function Inspector({
  node,
  runState,
  nodes,
}: {
  node: NodeState | null;
  runState: string;
  nodes: NodeState[];
}) {
  const total = nodes.reduce((sum, n) => sum + (n.usd ?? 0), 0);
  const measured = nodes.filter((n) => n.ms !== null).length;

  return (
    <SoftCard className="p-5" size="lg">
      <h3 className="text-sm font-semibold">{node ? node.title : "Run detail"}</h3>
      <p className="mt-1 text-xs" style={{ color: "var(--text-muted)" }}>
        {node ? node.blurb : "Nothing has run yet."}
      </p>

      <dl className="mt-4 space-y-2.5 text-sm">
        <Field label="Run state" value={runState} />
        {node && <Field label="Node status" value={node.status} />}
        {node?.ms !== null && node !== null && (
          <Field label="Took" value={formatMs(node.ms as number)} />
        )}
        {node?.usd !== null && node !== null && (node.usd as number) > 0 && (
          <Field label="This node" value={`$${(node.usd as number).toFixed(4)}`} />
        )}
        <Field
          label="Run total"
          value={total > 0 ? `$${total.toFixed(4)}` : "no model spend recorded"}
        />
        <Field label="Nodes measured" value={`${measured} of ${nodes.length}`} />
      </dl>

      {node?.error && (
        <div className="mt-4">
          <h4
            className="text-[11px] font-semibold uppercase tracking-wider"
            style={{ color: "var(--err)" }}
          >
            Error
          </h4>
          <p className="mt-1 whitespace-pre-wrap text-xs" style={{ color: "var(--text-muted)" }}>
            {node.error}
          </p>
        </div>
      )}

      <p className="mt-5 text-[11px]" style={{ color: "var(--text-faint)" }}>
        Costs are the deltas of the running total the graph reports after each node, so a
        node that made no model call shows none rather than zero.
      </p>
    </SoftCard>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-wrap gap-x-3">
      <dt
        className="min-w-[8rem] text-xs font-semibold uppercase tracking-wider"
        style={{ color: "var(--text-muted)" }}
      >
        {label}
      </dt>
      <dd>{value}</dd>
    </div>
  );
}

/**
 * Fold the event log into one row per pipeline node.
 *
 * Exported for its own test: this is the only part of the canvas with logic in it, and
 * the interesting cases are all in the data — a node that started and never finished, a
 * resumed run whose events repeat, and the cost delta arithmetic.
 */
export function derive(events: Event[], currentNode: string | null): NodeState[] {
  // Ordered by `seq`, not by arrival. The stream resumes from the last sequence seen and
  // polling can deliver a batch out of order, so sorting here is what stops a late
  // `started` from overwriting the `done` that followed it.
  const ordered = [...events].sort((a, b) => a.seq - b.seq);

  const byNode = new Map<string, { started?: Event; last?: Event }>();
  // Cumulative totals in `seq` order, so a delta is against the PREVIOUS node's total
  // rather than against whatever happened to be processed last.
  let previousTotal = 0;
  const ownCost = new Map<string, number>();

  for (const event of ordered) {
    const entry = byNode.get(event.node) ?? {};
    if (event.status === "started") {
      // A resumed run re-emits `started` for the node it picks up at. Keeping the FIRST
      // one would measure the duration from the original attempt, across the gap where
      // the process was dead.
      entry.started = event;
    } else {
      entry.last = event;
    }
    byNode.set(event.node, entry);

    if (event.status === "done") {
      const raw = event.payload["cost_usd"];
      const total = typeof raw === "string" || typeof raw === "number" ? Number(raw) : NaN;
      if (Number.isFinite(total)) {
        // Clamped at zero: a resumed run's first `done` can report a total LOWER than
        // the one before it, and a negative cost is not a thing.
        ownCost.set(event.node, Math.max(0, total - previousTotal));
        previousTotal = total;
      }
    }
  }

  return PIPELINE.map(({ node, title, blurb }) => {
    const entry = byNode.get(node);
    const last = entry?.last;
    const started = entry?.started;

    let status: NodeState["status"] = "pending";
    if (last?.status === "done") status = "done";
    else if (last?.status === "failed") status = "failed";
    else if (last?.status === "skipped") status = "skipped";
    else if (started || currentNode === node) status = "running";

    const ms =
      started && last && last.status === "done"
        ? new Date(last.at).getTime() - new Date(started.at).getTime()
        : null;

    return {
      node,
      title,
      blurb,
      status,
      usd: ownCost.has(node) ? (ownCost.get(node) as number) : null,
      ms: ms !== null && Number.isFinite(ms) && ms >= 0 ? ms : null,
      error: typeof last?.payload?.["error"] === "string" ? String(last.payload["error"]) : null,
      at: last?.at ?? started?.at ?? null,
    };
  });
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}
