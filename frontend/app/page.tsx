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
    // Two columns on desktop, one on mobile. At `max-w-2xl` this was a 672px column
    // on a 1440px screen -- a lot of empty space, and it read as unfinished rather
    // than as deliberate restraint.
    //
    // Split at `lg:` rather than `md:`, because the status card carries a two-column
    // definition list of its own; breaking the page at tablet width would put a grid
    // inside a grid inside about 350px.
    <main className="mx-auto max-w-5xl px-6 py-16">
      <div className="grid items-start gap-10 lg:grid-cols-2 lg:gap-14">
        {/* Left: what this is, and what you can do. */}
        <div>
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

          {/* Filled in the two brand colours: deep green for the primary action, orange
              for the secondary one. Still `<a>` rather than `SoftButton`, because these
              navigate -- a button that changes the address is the wrong element and
              breaks middle-click, ctrl-click and "copy link".

              Note the ink on the orange is `--accent-ink`, NOT white. White on #ef7215
              measures 2.96:1 and fails the 4.5:1 AA needs for normal text; the dark ink
              measures 6.09:1 and keeps the brand orange exactly. */}
          <nav className="mt-8 flex flex-wrap gap-3" aria-label="Screens">
            <a
              href="/onboard"
              className="soft-press px-4 py-2 text-sm font-medium"
              style={{
                borderRadius: "var(--r-pill)",
                background: "var(--primary)",
                color: "var(--primary-ink)",
                boxShadow:
                  "-4px -4px 10px var(--shadow-light), 5px 5px 14px var(--shadow-dark)",
              }}
            >
              Onboard a business
            </a>
            <a
              href="/memory"
              className="soft-press px-4 py-2 text-sm font-medium"
              style={{
                borderRadius: "var(--r-pill)",
                background: "var(--accent)",
                color: "var(--accent-ink)",
                boxShadow:
                  "-4px -4px 10px var(--shadow-light), 5px 5px 14px var(--shadow-dark)",
              }}
            >
              What I remember
            </a>
          </nav>

          <p className="mt-8 text-xs" style={{ color: "var(--text-muted)" }}>
            Next: Phase 1 — paste a website URL, crawl it, and generate the first
            article end to end.
          </p>
        </div>

        {/* Right column on desktop. `lg:mt-0` because the left column's own top
            margin already spaces it, and a second one would misalign the two. */}
        <section
          className="mt-10 rounded-xl border p-6 lg:mt-0"
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

      </div>
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
