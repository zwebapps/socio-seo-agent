"use client";

/**
 * `/business` — everything the agent believes about this business, in one place.
 *
 * The gap this fills: the business's own information was scattacross four screens and
 * no screen showed the business. `/onboard` held the profile but only as a form you
 * re-submit, `/memory` held the brand rules, `/documents` held the material, and the
 * name and website appeared nowhere at all. An owner asking "what does this thing
 * actually know about me?" had no page to open.
 *
 * A client component, and it has to be. Every call carries the session cookie and the
 * API's Origin-CSRF guard refuses a cookie-bearing request with no `Origin` header —
 * which is exactly what `fetch` from a server component sends. Same reason the
 * dashboard is one.
 *
 * Reads only endpoints that already exist: `GET /api/v1/onboarding` for identity and
 * `GET /api/v1/memory` for the brand profile. The SEO audit of the customer's own site
 * is deliberately NOT here yet — it lives in the run checkpoint and needs its own
 * read; see the note by `AuditPlaceholder` below.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import { Shell } from "@/app/components/page-shell";
import { Pill, SoftCard } from "@/app/components/soft";
import { fetchOnboardingState, type OnboardingState } from "@/app/lib/api";
import { fetchMemory, type BusinessMemory } from "@/app/lib/memory-api";

type Loaded = {
  setup: OnboardingState;
  memory: BusinessMemory | null;
};

type State =
  | { kind: "loading" }
  | { kind: "ready"; data: Loaded }
  | { kind: "error"; message: string };

export default function BusinessPage() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const setup = await fetchOnboardingState();
        // The memory read is allowed to fail on its own: an account with no business
        // has no brand profile to fetch, and that is a state to RENDER rather than an
        // error to show. Failing the whole page on it would hide the identity panel
        // that explains why the rest is empty.
        let memory: BusinessMemory | null = null;
        try {
          memory = await fetchMemory();
        } catch {
          memory = null;
        }
        if (live) setState({ kind: "ready", data: { setup, memory } });
      } catch (exc) {
        if (live) {
          setState({
            kind: "error",
            message: exc instanceof Error ? exc.message : "Could not load the business.",
          });
        }
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  return (
    <Shell className="py-14">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Your business
      </p>
      <h1 className="mt-2 text-[28px] font-semibold tracking-tight">Business profile</h1>
      <p className="mt-3 max-w-[70ch] text-base" style={{ color: "var(--text-muted)" }}>
        What the agent knows about you, and where it got it. Everything a run produces is
        written against this — which site gets audited, what the posts may claim, and what
        they are forbidden from saying.
      </p>

      {state.kind === "loading" && (
        <p className="mt-10 text-sm" style={{ color: "var(--text-muted)" }}>
          Loading…
        </p>
      )}

      {state.kind === "error" && (
        <SoftCard className="mt-10 p-6" size="lg">
          <p role="alert" className="text-sm font-medium" style={{ color: "var(--err)" }}>
            {state.message}
          </p>
        </SoftCard>
      )}

      {state.kind === "ready" && <Ready data={state.data} />}
    </Shell>
  );
}

function Ready({ data }: { data: Loaded }) {
  const { setup, memory } = data;

  if (!setup.hasBusiness) {
    return (
      <SoftCard className="mt-10 p-6" size="lg">
        <h2 className="text-sm font-semibold">No business on this account yet</h2>
        <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
          Name your business on the dashboard first — everything on this page belongs to
          one.
        </p>
        <Link
          href="/"
          className="mt-4 inline-block text-sm font-medium underline"
          style={{ color: "var(--primary)" }}
        >
          Go to the dashboard
        </Link>
      </SoftCard>
    );
  }

  return (
    <div className="mt-10 grid items-start gap-8 lg:grid-cols-2 lg:gap-10">
      <div className="space-y-8">
        <Identity setup={setup} />
        <BrandVoice memory={memory} />
      </div>
      <div className="space-y-8">
        <BannedClaims memory={memory} />
        <AuditPlaceholder onboarded={setup.onboarded} />
        <Elsewhere />
      </div>
    </div>
  );
}

/** Name, website, and whether the profile has actually been confirmed. */
function Identity({ setup }: { setup: OnboardingState }) {
  return (
    <SoftCard className="p-6" size="lg">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Identity</h2>
        {/* The distinction is load-bearing: an unconfirmed profile means HARVEST has no
            website to crawl and the claim gate has no banned list, so a run stops at
            INTAKE. A green tick on an unconfirmed profile would hide that. */}
        {setup.onboarded ? (
          <Pill tone="accent">confirmed</Pill>
        ) : (
          <Pill>not confirmed yet</Pill>
        )}
      </div>

      <dl className="mt-4 space-y-3 text-sm">
        <Row label="Business name" value={setup.name} />
        <Row
          label="Website"
          value={
            setup.website ? (
              <a
                href={setup.website}
                target="_blank"
                rel="noreferrer noopener"
                className="underline"
                style={{ color: "var(--primary)" }}
              >
                {setup.website}
              </a>
            ) : null
          }
        />
      </dl>

      {!setup.onboarded && (
        <p className="mt-4 text-xs" style={{ color: "var(--text-muted)" }}>
          Until this is confirmed, a run stops at its first step: there is no site to
          audit and no forbidden-claim list to enforce.
        </p>
      )}

      <Link
        href="/onboard"
        className="mt-4 inline-block text-sm font-medium underline"
        style={{ color: "var(--primary)" }}
      >
        {setup.onboarded ? "Re-read the website and correct this" : "Onboard your business"}
      </Link>
    </SoftCard>
  );
}

/** Tone and audience — the brand decisions, which are user-mode, not model knobs. */
function BrandVoice({ memory }: { memory: BusinessMemory | null }) {
  const preferences = memory?.preferences ?? [];

  return (
    <SoftCard className="p-6" size="lg">
      <h2 className="text-sm font-semibold">Voice and audience</h2>
      <dl className="mt-4 space-y-3 text-sm">
        <Row label="Tone" value={memory?.tone ?? null} />
        <Row label="Audience" value={memory?.audience ?? null} />
      </dl>

      <h3 className="mt-5 text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
        Remembered preferences
      </h3>
      {preferences.length === 0 ? (
        <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
          None yet. Anything added here is carried into every run from then on.
        </p>
      ) : (
        <ul className="mt-2 space-y-2 text-sm">
          {preferences.map((rule) => (
            <li key={rule.id} className="flex gap-2">
              <span aria-hidden style={{ color: "var(--text-faint)" }}>
                •
              </span>
              <span>{rule.rule}</span>
            </li>
          ))}
        </ul>
      )}

      <Link
        href="/memory"
        className="mt-4 inline-block text-sm font-medium underline"
        style={{ color: "var(--primary)" }}
      >
        Edit what I remember
      </Link>
    </SoftCard>
  );
}

/**
 * The forbidden claims, given their own panel rather than a row in a list.
 *
 * They are the one part of the profile that BLOCKS publication: VALIDATE fails a draft
 * carrying one, and REPACK drops a per-channel post that carries one rather than
 * publishing it. A regulated-claims list buried in a table of profile fields does not
 * read as the thing that can stop a run.
 */
function BannedClaims({ memory }: { memory: BusinessMemory | null }) {
  const claims = memory?.bannedClaims ?? [];

  return (
    <SoftCard className="p-6" size="lg">
      <h2 className="text-sm font-semibold">Claims the agent may never make</h2>
      <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
        Checked twice — once on the article and again on every channel post after it is
        trimmed. A post carrying one of these is withheld rather than published, and the
        run says which channel lost it.
      </p>

      {claims.length === 0 ? (
        <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
          None recorded. If your trade has phrases it must not use — a guarantee, a
          superlative, a medical promise — add them during onboarding.
        </p>
      ) : (
        <ul className="mt-3 flex flex-wrap gap-2">
          {claims.map((claim) => (
            <li
              key={claim}
              className="soft-edge px-3 py-1.5 text-xs font-medium"
              style={{ borderRadius: "var(--r-pill)", color: "var(--err)" }}
            >
              {claim}
            </li>
          ))}
        </ul>
      )}
    </SoftCard>
  );
}

/**
 * The SEO audit of the customer's own site.
 *
 * The engine exists and is tested (`engines/seo/audit.py`) and every run computes the
 * audit into its checkpoint via `summarise_crawl` — but there is no read for "the
 * latest audit for this business" yet, and this page must not invent one. Showing a
 * placeholder that says where the data is beats an empty card that reads as "your site
 * is fine".
 */
function AuditPlaceholder({ onboarded }: { onboarded: boolean }) {
  return (
    <SoftCard className="p-6" size="lg">
      <h2 className="text-sm font-semibold">SEO audit of your site</h2>
      <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
        {onboarded
          ? "Every run audits the pages you already have — titles, meta descriptions, H1s, thin pages, missing structured data, plus the cross-page problems a single page cannot show: duplicate titles and pages nothing links to. Open your latest run to see it."
          : "Confirm your website first. The audit reads the pages you already have, so it needs to know where they are."}
      </p>
      <Link
        href={onboarded ? "/runs" : "/onboard"}
        className="mt-4 inline-block text-sm font-medium underline"
        style={{ color: "var(--primary)" }}
      >
        {onboarded ? "See your runs" : "Onboard your business"}
      </Link>
    </SoftCard>
  );
}

const RELATED: ReadonlyArray<{ href: string; title: string; blurb: string }> = [
  { href: "/content", title: "Content", blurb: "The posts written for each channel." },
  { href: "/documents", title: "Your documents", blurb: "The material the agent may quote." },
  {
    href: "/connections",
    title: "Platform accounts",
    blurb: "What a publish step could actually do.",
  },
];

function Elsewhere() {
  return (
    <SoftCard className="p-6" size="lg">
      <h2 className="text-sm font-semibold">Related</h2>
      <ul className="mt-3 space-y-2 text-sm">
        {RELATED.map((item) => (
          <li key={item.href}>
            <Link
              href={item.href}
              className="font-medium underline"
              style={{ color: "var(--primary)" }}
            >
              {item.title}
            </Link>
            <span style={{ color: "var(--text-muted)" }}> — {item.blurb}</span>
          </li>
        ))}
      </ul>
    </SoftCard>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1">
      <dt className="min-w-[9rem] text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
        {label}
      </dt>
      {/* "Not set" rather than an empty cell: a blank looks like a rendering bug, and
          the whole point of this page is saying what is and is not known. */}
      <dd>{value ?? <span style={{ color: "var(--text-faint)" }}>Not set</span>}</dd>
    </div>
  );
}
