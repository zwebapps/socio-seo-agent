"use client";

/**
 * The run timeline.
 *
 * This is the screen that makes the agent visible: which node is working, what it decided,
 * what each step cost. Three things it does that a naive live view would get wrong:
 *
 * - it RESUMES from the last sequence number it saw, so a dropped connection does not
 *   replay the whole run and re-animate the timeline from the beginning;
 * - it falls back to polling when EventSource is unavailable or errors, because a
 *   timeline that silently stops updating looks like an agent that silently stopped;
 * - it renders the terminal state explicitly. "Nothing more is coming, and here is why"
 *   is information; a spinner that never resolves is not.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Pill, SoftCard, SoftWell } from "../../components/soft";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

type Event = {
  seq: number;
  node: string;
  status: "started" | "done" | "failed" | "skipped";
  payload: Record<string, unknown>;
  at: string;
};

type Run = {
  runId: string;
  goal: string;
  state: string;
  currentNode: string | null;
  resumedCount: number;
  finishedReason: string | null;
  events: Event[];
};

const NODE_ORDER = [
  "INTAKE",
  "HARVEST",
  "OPPORTUNITY",
  "PLAN",
  "GENERATE",
  "VALIDATE",
  "REPACK",
  "REVIEW",
];

const NODE_LABEL: Record<string, string> = {
  INTAKE: "Understanding the request",
  HARVEST: "Gathering evidence",
  OPPORTUNITY: "Choosing what to write",
  PLAN: "Outlining the page",
  GENERATE: "Writing",
  VALIDATE: "Scoring against the SEO rules",
  REPACK: "Adapting per channel",
  REVIEW: "Waiting for you",
};

const TERMINAL = new Set(["awaiting_approval", "done", "failed", "partial"]);

export default function RunPage({ params }: { params: Promise<{ runId: string }> }) {
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const lastSeq = useRef(0);

  useEffect(() => {
    void params.then((p) => setRunId(p.runId));
  }, [params]);

  const merge = useCallback((incoming: Event[]) => {
    if (incoming.length === 0) return;
    setEvents((prev) => {
      const bySeq = new Map(prev.map((e) => [e.seq, e]));
      for (const e of incoming) bySeq.set(e.seq, e);
      const merged = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
      lastSeq.current = Math.max(lastSeq.current, ...merged.map((e) => e.seq));
      return merged;
    });
  }, []);

  const refresh = useCallback(async () => {
    if (!runId) return null;
    try {
      const response = await fetch(`${API_URL}/api/v1/runs/${runId}`, {
        credentials: "include",
        cache: "no-store",
      });
      if (response.status === 404) {
        setError("That run does not exist, or belongs to another account.");
        return null;
      }
      if (!response.ok) {
        setError(`Could not load the run (${response.status}).`);
        return null;
      }
      const body = (await response.json()) as Run;
      setRun(body);
      merge(body.events);
      setError(null);
      return body;
    } catch {
      setError(`Cannot reach the API at ${API_URL}.`);
      return null;
    }
  }, [runId, merge]);

  useEffect(() => {
    if (!runId) return;
    let source: EventSource | null = null;
    let poller: number | null = null;
    let cancelled = false;

    async function begin() {
      const first = await refresh();
      if (cancelled || !first) return;
      if (TERMINAL.has(first.state)) return;

      // Resume from where we already are, so a reconnect costs nothing.
      const url = `${API_URL}/api/v1/runs/${runId}/events?after=${lastSeq.current}`;
      try {
        source = new EventSource(url, { withCredentials: true });
        source.addEventListener("node", (raw) => {
          merge([JSON.parse((raw as MessageEvent<string>).data) as Event]);
          setLive(true);
        });
        source.addEventListener("end", () => {
          source?.close();
          setLive(false);
          void refresh();
        });
        source.onerror = () => {
          // A stream that stops silently looks like an agent that stopped. Fall back to
          // polling rather than leaving a stale screen.
          source?.close();
          setLive(false);
          poller = window.setInterval(() => void refresh(), 2000);
        };
      } catch {
        poller = window.setInterval(() => void refresh(), 2000);
      }
    }

    void begin();
    return () => {
      cancelled = true;
      source?.close();
      if (poller !== null) window.clearInterval(poller);
    };
  }, [runId, refresh, merge]);

  const totalCost = events.reduce((sum, e) => {
    const raw = e.payload["cost_usd"];
    return sum + (typeof raw === "string" ? Number.parseFloat(raw) || 0 : 0);
  }, 0);

  const statusByNode = new Map<string, Event["status"]>();
  for (const e of events) statusByNode.set(e.node, e.status);

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Run
      </p>
      <h1 className="mt-2 text-[26px] font-semibold tracking-tight">
        {run?.goal ?? "Loading…"}
      </h1>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {run && <Pill tone={toneFor(run.state)}>{run.state.replace(/_/g, " ")}</Pill>}
        {live && <Pill tone="accent">live</Pill>}
        {events.length > 0 && (
          <Pill tone="muted">
            {events.length} events · ${totalCost.toFixed(4)}
          </Pill>
        )}
        {run && run.resumedCount > 0 && (
          <Pill tone="warn">resumed {run.resumedCount}×</Pill>
        )}
      </div>

      {error && (
        <SoftCard className="mt-6 p-5" size="md">
          <p className="text-sm font-semibold" style={{ color: "var(--err)" }}>
            {error}
          </p>
        </SoftCard>
      )}

      <SoftCard className="mt-7 p-6" size="lg">
        <ol className="space-y-1">
          {NODE_ORDER.map((node) => {
            const status = statusByNode.get(node);
            const nodeEvents = events.filter((e) => e.node === node);
            const cost = nodeEvents.reduce((sum, e) => {
              const raw = e.payload["cost_usd"];
              return sum + (typeof raw === "string" ? Number.parseFloat(raw) || 0 : 0);
            }, 0);
            return (
              <Step
                key={node}
                node={node}
                label={NODE_LABEL[node] ?? node}
                status={status}
                cost={cost}
                active={run?.currentNode === node}
              />
            );
          })}
        </ol>

        {run?.finishedReason && (
          <SoftWell className="mt-5 p-4">
            <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--warn)" }}>
              Why it stopped
            </p>
            <p className="mt-1.5 text-sm">{run.finishedReason}</p>
          </SoftWell>
        )}
      </SoftCard>

      {events.some((e) => e.status === "failed") && (
        <SoftCard className="mt-5 p-5" size="md">
          <p className="text-sm font-semibold" style={{ color: "var(--warn)" }}>
            Some evidence could not be gathered
          </p>
          <p className="mt-1 text-sm" style={{ color: "var(--text-muted)" }}>
            The run continued with less to go on. What is missing is listed on the draft, so
            nothing here implies research that did not happen.
          </p>
        </SoftCard>
      )}
    </main>
  );
}

function toneFor(state: string): "ok" | "warn" | "err" | "accent" | "muted" {
  if (state === "done") return "ok";
  if (state === "awaiting_approval") return "accent";
  if (state === "failed") return "err";
  if (state === "partial") return "warn";
  return "muted";
}

function Step({
  node,
  label,
  status,
  cost,
  active,
}: {
  node: string;
  label: string;
  status?: Event["status"];
  cost: number;
  active: boolean;
}) {
  const done = status === "done";
  const failed = status === "failed";
  const running = status === "started" || active;

  const mark = done ? "✓" : failed ? "!" : running ? "•" : "";
  const colour = done
    ? "var(--ok)"
    : failed
      ? "var(--warn)"
      : running
        ? "var(--accent)"
        : "var(--text-faint)";

  return (
    <li className="flex items-center gap-3 py-2">
      <span
        aria-hidden
        className="flex h-7 w-7 shrink-0 items-center justify-center text-xs font-bold"
        style={{
          borderRadius: "50%",
          color: colour,
          border: `1px solid ${status || active ? colour : "var(--edge)"}`,
        }}
      >
        {mark}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium" style={{ color: status ? "var(--text)" : "var(--text-faint)" }}>
          {label}
        </span>
        <span className="block text-[11px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
          {node}
          {status ? ` · ${status}` : " · waiting"}
        </span>
      </span>
      {cost > 0 && (
        <span className="tabular text-xs" style={{ color: "var(--text-muted)" }}>
          ${cost.toFixed(4)}
        </span>
      )}
    </li>
  );
}
