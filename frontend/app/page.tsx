"use client";

import { useCallback, useEffect, useState } from "react";

/** Shape returned by GET /api/v1/health. */
type Health = {
  status: string;
  service: string;
  version: string;
  environment: string;
};

type State =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

/* A client component on purpose. A server component fetching at render time
   would make `next build` depend on the API being up, which would couple the
   web build to backend availability in CI. It also means "API unreachable" is a
   real, designed UI state rather than a crash -- the same principle the run
   timeline will need in Phase 6. */
export default function Home() {
  const [state, setState] = useState<State>({ kind: "loading" });

  const check = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const response = await fetch(`${API_URL}/api/v1/health`, {
        cache: "no-store",
      });
      if (!response.ok) {
        setState({
          kind: "error",
          message: `API responded ${response.status} ${response.statusText}`,
        });
        return;
      }
      setState({ kind: "ok", health: (await response.json()) as Health });
    } catch {
      setState({
        kind: "error",
        message: `Cannot reach the API at ${API_URL}. Is it running?`,
      });
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <p
        className="text-xs font-semibold uppercase tracking-widest"
        style={{ color: "var(--accent)" }}
      >
        Phase 0 · Foundations
      </p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight">
        Social Marketing Agent
      </h1>
      <p className="mt-3 text-base" style={{ color: "var(--text-muted)" }}>
        SEO content, AI-answer visibility, social content and lead capture for
        small businesses.
      </p>

      <section
        className="mt-10 rounded-xl border p-6"
        style={{
          background: "var(--surface)",
          borderColor: "var(--border)",
        }}
        aria-labelledby="api-status-heading"
      >
        <div className="flex items-center justify-between gap-4">
          <h2 id="api-status-heading" className="text-sm font-semibold">
            Backend status
          </h2>
          <button
            type="button"
            onClick={() => void check()}
            className="rounded-md border px-3 py-1.5 text-xs font-medium transition-opacity hover:opacity-80"
            style={{ borderColor: "var(--border)", color: "var(--text)" }}
          >
            Re-check
          </button>
        </div>

        {/* aria-live so a screen reader hears the result arrive, and a reserved
            min-height so the late answer does not shift the layout (CLS). */}
        <div className="mt-4 min-h-24" aria-live="polite">
          {state.kind === "loading" && (
            <p className="text-sm" style={{ color: "var(--text-muted)" }}>
              Checking {API_URL}…
            </p>
          )}

          {state.kind === "error" && (
            <div>
              <p className="text-sm font-medium" style={{ color: "var(--err)" }}>
                Unreachable
              </p>
              <p className="mt-1 text-sm" style={{ color: "var(--text-muted)" }}>
                {state.message}
              </p>
              <p className="mt-3 text-xs" style={{ color: "var(--text-muted)" }}>
                Start it with{" "}
                <code className="rounded px-1" style={{ background: "var(--bg)" }}>
                  make api
                </code>
              </p>
            </div>
          )}

          {state.kind === "ok" && (
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
              <Row label="Status" value={state.health.status} accent />
              <Row label="Service" value={state.health.service} />
              <Row label="Version" value={state.health.version} />
              <Row label="Environment" value={state.health.environment} />
            </dl>
          )}
        </div>
      </section>

      {/* There is no global navigation in the layout yet, so the screens that exist are
          linked from here. Without this the memory panel would be reachable only by
          typing its address, which is not shipped. */}
      <nav className="mt-8 flex flex-wrap gap-3" aria-label="Screens">
        <a
          href="/onboard"
          className="soft-raised soft-edge soft-press px-4 py-2 text-sm font-medium"
          style={{ borderRadius: "var(--r-pill)", color: "var(--text)" }}
        >
          Onboard a business
        </a>
        <a
          href="/memory"
          className="soft-raised soft-edge soft-press px-4 py-2 text-sm font-medium"
          style={{ borderRadius: "var(--r-pill)", color: "var(--text)" }}
        >
          What I remember about your business
        </a>
      </nav>

      <p className="mt-8 text-xs" style={{ color: "var(--text-muted)" }}>
        Next: Phase 1 — paste a website URL, crawl it, and generate the first
        article end to end.
      </p>
    </main>
  );
}

function Row({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <>
      <dt style={{ color: "var(--text-muted)" }}>{label}</dt>
      <dd
        className={accent ? "font-semibold" : ""}
        style={{ color: accent ? "var(--ok)" : "var(--text)" }}
      >
        {value}
      </dd>
    </>
  );
}
