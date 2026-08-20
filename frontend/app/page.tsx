"use client";

/**
 * The owner's home.
 *
 * This screen used to be a "Phase 0 Foundations" placeholder whose main event was a card
 * reporting whether the API was reachable. That is a developer's question. An owner arriving
 * here could not start a run, could not find a run they had already started, and could not
 * see a single captured lead — every one of those endpoints existed and worked, and nothing
 * on any screen called them.
 *
 * So the page now answers the three questions an owner actually has, in the order they have
 * them: **what should the agent work on** (start a run), **what has it been doing** (recent
 * runs, with their real state), and **what did it earn me** (leads).
 *
 * A client component, and it has to be. Every call it makes carries the session cookie, and
 * the API's Origin-CSRF guard refuses a cookie-bearing request that arrives with no `Origin`
 * header — which is exactly what `fetch` from a server component sends. It also means "the
 * API is unreachable" is a designed state rather than a build-time failure, which is why the
 * backend status element survives at the bottom of the page.
 *
 * Two columns at `lg:`, inside `max-w-5xl`. At `max-w-2xl` this was a 672px column on a
 * 1440px screen and read as unfinished rather than as restraint. The split is at `lg:` and
 * not `md:` because the right column is a list of runs whose rows carry their own wrapped
 * text; breaking the page at tablet width puts a list inside a grid inside about 350px.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { RunRows, useRuns } from "@/app/components/run-rows";
import { Pill, SoftButton, SoftCard } from "@/app/components/soft";
import { StartRunForm } from "@/app/components/start-run";
import { RECENT_RUNS } from "@/app/lib/runs-api";

/** Shape returned by GET /api/v1/health. */
type Health = {
  status: string;
  service: string;
  version: string;
  environment: string;
};

type HealthState =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

export default function Home() {
  const { state: runs, live, reload } = useRuns(RECENT_RUNS);

  return (
    <main className="mx-auto max-w-5xl px-6 py-14">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Your growth agent
      </p>
      <h1 className="mt-2 text-[28px] font-semibold tracking-tight">
        Social Marketing Agent
      </h1>
      <p className="mt-3 max-w-2xl text-base" style={{ color: "var(--text-muted)" }}>
        Give it a goal. It gathers evidence about your business, picks something worth
        writing, writes it, scores it against the SEO rules, adapts it per channel — and
        hands it back to you to approve.
      </p>

      <div className="mt-10 grid items-start gap-10 lg:grid-cols-2 lg:gap-14">
        {/* Left: the thing to DO. */}
        <div>
          <SoftCard className="p-6" size="lg">
            <h2 className="text-sm font-semibold">Start a run</h2>
            <StartRunForm />
          </SoftCard>

          <nav className="mt-8" aria-label="Screens">
            <h2
              className="text-[11px] font-semibold uppercase tracking-wider"
              style={{ color: "var(--text-muted)" }}
            >
              Elsewhere
            </h2>
            <ul className="mt-3 space-y-2.5">
              <NavRow
                href="/leads"
                title="Leads"
                blurb="Who got in touch, and which piece of content earned them."
              />
              <NavRow
                href="/documents"
                title="Your documents"
                blurb="Upload a price list or service sheet — the agent quotes your own material instead of guessing."
              />
              <NavRow
                href="/memory"
                title="What I remember"
                blurb="The preferences carried into every run — and the exact lines the next one receives."
              />
              <NavRow
                href="/onboard"
                title="Onboard a business"
                blurb="Paste a website URL and let it read the business for itself."
              />
            </ul>
          </nav>
        </div>

        {/* Right: what has already happened. */}
        <section aria-labelledby="recent-runs-heading">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="recent-runs-heading" className="text-sm font-semibold">
              Recent runs
            </h2>
            <div className="flex items-center gap-2">
              {/* Only while something is actually in flight, so the badge means something
                  rather than being permanent furniture. */}
              {live && <Pill tone="accent">live</Pill>}
              <SoftButton
                onClick={() => void reload()}
                variant="quiet"
                ariaLabel="Refresh the list of recent runs"
              >
                Refresh
              </SoftButton>
            </div>
          </div>

          {/* `aria-live` so a state change arriving from the poll is announced instead of
              silently replacing the row a screen-reader user was reading. */}
          <div className="mt-4" aria-live="polite">
            <RunRows
              state={runs}
              emptyNote="No runs yet. Describe a goal on the left and the agent will start one."
            />
          </div>

          {runs.kind === "ready" && runs.runs.length > 0 && (
            <p className="mt-4">
              <Link
                href="/runs"
                className="text-sm font-medium underline"
                style={{ color: "var(--primary)" }}
              >
                See all runs
              </Link>
            </p>
          )}
        </section>
      </div>

      <BackendStatus />
    </main>
  );
}

function NavRow({ href, title, blurb }: { href: string; title: string; blurb: string }) {
  return (
    <li>
      {/* `soft-edge` because this is an interactive control and a neumorphic shadow measures
          about 1.2:1 — the hairline is what carries the 3:1 boundary SC 1.4.11 asks for. */}
      <Link
        href={href}
        className="soft-flat soft-edge soft-press block px-4 py-3"
        style={{ borderRadius: "var(--r-sm)", color: "var(--text)" }}
      >
        <span className="block text-sm font-medium">{title}</span>
        <span className="mt-0.5 block text-xs" style={{ color: "var(--text-muted)" }}>
          {blurb}
        </span>
      </Link>
    </li>
  );
}

/**
 * Whether the API is reachable — kept, but demoted.
 *
 * It earns its place because every other thing on this page is a call to that API, so when
 * the page looks empty this line is the difference between "you have no runs" and "nothing
 * could be asked". What it does not earn is the top of the screen: an owner does not open
 * their dashboard to read a version string, and a green tick on a service is not a product.
 *
 * So it is one line in a footer, and it only expands into detail when something is wrong.
 */
function BackendStatus() {
  const [state, setState] = useState<HealthState>({ kind: "loading" });

  const check = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const response = await fetch(`${API_URL}/api/v1/health`, { cache: "no-store" });
      if (!response.ok) {
        setState({
          kind: "error",
          message: `API responded ${response.status} ${response.statusText}`,
        });
        return;
      }
      setState({ kind: "ok", health: (await response.json()) as Health });
    } catch {
      setState({ kind: "error", message: `Cannot reach the API at ${API_URL}.` });
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  return (
    <footer
      className="mt-14 flex flex-wrap items-center justify-between gap-3 border-t pt-5"
      // `--edge`, not `--border`: there is no `--border` token in globals.css. The card this
      // footer replaces asked for one, so its border fell back to `currentColor` and was
      // never the hairline it looked like in the source. `--edge` is the measured one
      // (3.37:1 on `--bg`).
      style={{ borderColor: "var(--edge)" }}
    >
      <div className="min-h-5 text-xs" aria-live="polite" style={{ color: "var(--text-muted)" }}>
        {state.kind === "loading" && <span>Checking the backend…</span>}

        {state.kind === "ok" && (
          <span>
            Backend {state.health.status} · {state.health.service} {state.health.version} ·{" "}
            {state.health.environment}
          </span>
        )}

        {state.kind === "error" && (
          <span style={{ color: "var(--err)" }}>
            {state.message} Nothing on this page can load until it is up — start it with{" "}
            <code className="rounded px-1" style={{ background: "var(--surface-sunken)" }}>
              make api
            </code>
            .
          </span>
        )}
      </div>

      <SoftButton
        onClick={() => void check()}
        variant="quiet"
        ariaLabel="Re-check whether the backend is reachable"
      >
        Re-check
      </SoftButton>
    </footer>
  );
}
