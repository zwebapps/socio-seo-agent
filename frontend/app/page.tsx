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
 * Two columns at `lg:`, inside the shared `Shell` (fills the viewport to 1800px). It was
 * `max-w-5xl` — a 1024px column on a 1440px screen, which read as unfinished rather than
 * as restraint, the same complaint the earlier `max-w-2xl` earned. The split is at `lg:`
 * and not `md:` because the right column is a list of runs whose rows carry their own
 * wrapped text; breaking the page at tablet width puts a list inside a grid inside about
 * 350px. The extra room at `xl:` goes to that list rather than to the goal input: a text
 * field does not read better at 800px, and a run row with a wrapped stop-reason does —
 * but it needs SOME of it: at 22rem the goal input and its button shared 352px and the
 * placeholder truncated mid-word, so the left column grows to 26/30rem and the rest of
 * the gain still goes right.
 */

import Link from "next/link";
import { useCallback, useEffect, useId, useState } from "react";

import { Shell } from "@/app/components/page-shell";
import { RunRows, useRuns } from "@/app/components/run-rows";
import { Pill, SoftButton, SoftCard, SoftInput } from "@/app/components/soft";
import { StartRunForm } from "@/app/components/start-run";
import {
  ApiError,
  createBusiness,
  fetchOnboardingState,
  type OnboardingState,
} from "@/app/lib/api";
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
  // `null` while unknown, which is NOT the same as a loaded answer. Rendering
  // "onboard first" during the round trip would flash a setup prompt at an owner who
  // onboarded months ago, so the prompt waits and the page shows its normal self.
  const [setup, setSetup] = useState<OnboardingState | null>(null);

  useEffect(() => {
    let live = true;
    void fetchOnboardingState()
      .then((state) => {
        if (live) setSetup(state);
      })
      // Swallowed deliberately: this read only decides which panel leads. If it fails
      // the page keeps working exactly as it did before the read existed, and the
      // BackendStatus element at the bottom is what reports an unreachable API.
      .catch(() => {
        if (live) setSetup(null);
      });
    return () => {
      live = false;
    };
  }, []);

  const needsOnboarding = setup !== null && setup.hasBusiness && !setup.onboarded;
  const hasNoBusiness = setup !== null && !setup.hasBusiness;

  return (
    <Shell className="py-14">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Your growth agent
      </p>
      <h1 className="mt-2 text-[28px] font-semibold tracking-tight">
        Social Marketing Agent
      </h1>
      <p className="mt-3 max-w-[70ch] text-base" style={{ color: "var(--text-muted)" }}>
        Give it a goal. It gathers evidence about your business, picks something worth
        writing, writes it, scores it against the SEO rules, adapts it per channel — and
        hands it back to you to approve.
      </p>

      {/* Two columns from `lg`, and the right-hand list gets the extra room at `xl`
          rather than the form growing: a goal input does not read better at 800px, and
          a list of runs with wrapped goals and stop-reasons does. */}
      <div className="mt-10 grid items-start gap-10 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)] xl:grid-cols-[minmax(0,30rem)_minmax(0,1fr)] lg:gap-14 xl:gap-20">
        {/* Left: the thing to DO. */}
        <div>
          {needsOnboarding && <OnboardFirst />}
          {hasNoBusiness && (
            <NoBusiness
              onCreated={() => {
                // A full reload rather than refetching one value: creating the business
                // changes what almost every panel on this page may read, and several of
                // them fetched before it existed.
                window.location.reload();
              }}
            />
          )}

          <SoftCard className="p-6" size="lg">
            <h2 className="text-sm font-semibold">Start a run</h2>
            {needsOnboarding && (
              <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
                A run needs the profile above first — without one it stops at its first
                step and tells you the business profile is missing.
              </p>
            )}
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
                href="/connections"
                title="Platform accounts"
                blurb="Which accounts are connected, and whether a publish step would actually be accepted."
              />
              {/* Still listed when the business IS onboarded — re-running it is how you
                  correct a profile after the site changes. When it is NOT, `OnboardFirst`
                  above is the one that leads, and this row is the second mention rather
                  than the only one. */}
              <NavRow
                href="/onboard"
                title={needsOnboarding ? "Onboard a business" : "Business profile"}
                blurb={
                  needsOnboarding
                    ? "Paste a website URL and let it read the business for itself."
                    : "Re-read the website and correct what the agent believes about the business."
                }
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
    </Shell>
  );
}

/**
 * The setup step, promoted to the top of the page when it has not been done.
 *
 * It is not a banner and not a toast: it is the first card, because it is the first
 * thing to do. Everything downstream reads the profile it writes — HARVEST crawls
 * `website`, the regulated-claim gate enforces `banned_claims`, and INTAKE refuses to
 * proceed without a `name` rather than inventing one.
 */
function OnboardFirst() {
  return (
    <SoftCard className="mb-6 p-6" size="lg">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Start here
      </p>
      <h2 className="mt-2 text-sm font-semibold">Onboard your business</h2>
      <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
        Paste your website and the agent reads it into a profile you confirm or correct.
        Everything after this depends on it: which site gets audited, which claims are
        forbidden, and what the posts are allowed to say.
      </p>
      <Link
        href="/onboard"
        className="soft-edge mt-4 inline-flex items-center px-4 py-2 text-xs font-semibold"
        style={{
          borderRadius: "var(--r-pill)",
          background: "var(--primary)",
          color: "var(--primary-ink)",
        }}
      >
        Onboard your business
      </Link>
    </SoftCard>
  );
}

/**
 * An account with no business at all.
 *
 * It gets its own panel rather than the onboarding one, because onboarding cannot
 * help: `POST /onboarding/confirm` writes to a specific business and there is none to
 * write to, so the button would 409. This is reachable for a platform admin granted
 * the role by `scripts/grant_platform_admin.py` — signup creates a user and a business
 * in one transaction, so an ordinary owner always has one.
 */
function NoBusiness({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameId = useId();

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Give the business a name.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await createBusiness(trimmed);
      onCreated();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "The business could not be created.");
      setBusy(false);
    }
  }

  return (
    <SoftCard className="mb-6 p-6" size="lg">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Start here
      </p>
      <h2 className="mt-2 text-sm font-semibold">Name your business</h2>
      <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
        Runs, documents and your website profile all belong to a business. Signing up no
        longer asks for this, so name it now — you can change it later.
      </p>
      <form onSubmit={submit} className="mt-4 flex flex-wrap items-start gap-3">
        <SoftInput
          controlId={nameId}
          label="Business name"
          value={name}
          onChange={(next) => {
            setName(next);
            if (error) setError(null);
          }}
          placeholder="Müller Sanitär GmbH"
          className="min-w-0 flex-1"
        />
        <SoftButton type="submit" variant="primary" disabled={busy}>
          {busy ? "Creating…" : "Create business"}
        </SoftButton>
      </form>
      {error && (
        <p role="alert" className="mt-2 text-sm font-medium" style={{ color: "var(--err)" }}>
          {error}
        </p>
      )}
    </SoftCard>
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
