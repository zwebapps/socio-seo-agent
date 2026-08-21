"use client";

/**
 * The public front page. What this is, for somebody who has never signed in.
 *
 * `/` used to be the dashboard, so a visitor who had not signed in got a shell of
 * authenticated panels and no explanation of the product. The dashboard now lives at
 * `/dashboard` and this is the page that says what the thing does.
 *
 * **Every number and claim here is one the product can support, and that ruled out most
 * of a conventional landing page.** No user count, no "trusted by N teams", no uptime
 * figure, no revenue or ROI — there are no customers yet and no uptime history, so those
 * would be invented, and `docs/CRITERIA_MAP.md` §7 makes claims discipline binding on UI
 * copy as much as on the README. What is here instead is what the machine actually does:
 * eleven named nodes, a human gate two of them sit behind, a deterministic SEO score, and
 * an audit of the site the visitor already owns. That is a stronger pitch than a fake
 * user count and it survives the first question anybody asks.
 *
 * The phrasings follow the same document's list of things not to say. Not "we generate
 * traffic" — Google movement takes 6-12 weeks and the honest promise is the content and
 * the instrumentation. Not "the agent learns" — it updates preferences from explicit
 * feedback. AI share of voice is a sample, never a census.
 *
 * A client component because it reads the session to decide between "start free" and "go
 * to your dashboard": the API's Origin-CSRF guard refuses a cookie-bearing request with
 * no `Origin` header, which is what a server component's `fetch` sends.
 */

import Link from "next/link";

import { Shell } from "@/app/components/page-shell";
import { useSession } from "@/app/components/session-context";
import { SoftCard } from "@/app/components/soft";

/** The pipeline, in the order it runs. Mirrors `ORDER` in `agents/graph.py`. */
const PIPELINE = [
  ["Intake", "Reads your profile and what you asked for"],
  ["Harvest", "Crawls your site, expands keywords, finds competitors, audits your address"],
  ["Opportunity", "Ranks what is worth writing, and says so if nothing is"],
  ["Plan", "Outlines against one target keyword"],
  ["Generate", "Writes the article"],
  ["Convert", "Writes the ask, per channel"],
  ["Validate", "Scores it against the SEO rules and the claims you forbid"],
  ["Repack", "Adapts it for each channel inside that channel's limits"],
  ["Review", "Stops. You decide."],
  ["Export", "Publishes what you approved"],
  ["Measure", "Clicks per channel, and your AI answer-engine share"],
] as const;

export default function Marketing() {
  const { state } = useSession();
  const signedIn = state.kind === "signed-in";

  return (
    <Shell className="py-16">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Social Marketing Agent
      </p>

      <h1 className="mt-3 max-w-[24ch] text-[40px] font-semibold leading-[1.08] tracking-tight sm:text-[52px]">
        An agent that does the marketing
        <span style={{ color: "var(--accent)" }}> you keep meaning to do.</span>
      </h1>

      <p className="mt-5 max-w-[62ch] text-base" style={{ color: "var(--text-muted)" }}>
        Give it your website. It reads your business, audits the pages you already have,
        picks something worth writing, writes it, scores it against the SEO rules, and
        shapes a post for every channel — then stops and waits for you to approve it.
      </p>

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <Link
          href={signedIn ? "/dashboard" : "/login"}
          className="soft-edge inline-flex items-center px-5 py-2.5 text-sm font-semibold"
          style={{
            borderRadius: "var(--r-pill)",
            background: "var(--primary)",
            color: "var(--primary-ink)",
          }}
        >
          {signedIn ? "Go to your dashboard" : "Create an account"}
        </Link>
        <Link
          href="#how"
          className="soft-edge inline-flex items-center px-5 py-2.5 text-sm font-semibold"
          style={{ borderRadius: "var(--r-pill)" }}
        >
          See how it works
        </Link>
      </div>

      {/*
        Where a landing page would put "12,400 teams" and a 99.98% uptime figure. There
        are no customers yet and no uptime history, so those numbers would be invented —
        and inventing them is the one thing this product refuses to do anywhere else, on
        its own dashboard included. These three are true of the code as it stands.
      */}
      <dl className="mt-12 flex flex-wrap gap-x-12 gap-y-6">
        {[
          ["11", "named steps, every exit deliberate"],
          ["0", "posts published without your approval"],
          ["85", "minimum SEO score before a draft passes"],
        ].map(([figure, label]) => (
          <div key={label}>
            <dt className="text-[28px] font-semibold tracking-tight">{figure}</dt>
            <dd className="mt-1 max-w-[22ch] text-xs" style={{ color: "var(--text-muted)" }}>
              {label}
            </dd>
          </div>
        ))}
      </dl>

      {/* --------------------------------------------------------------- */}

      <h2 id="how" className="mt-20 max-w-[28ch] text-[30px] font-semibold tracking-tight">
        Eleven steps. Every exit deliberate.
      </h2>
      <p className="mt-3 max-w-[62ch] text-sm" style={{ color: "var(--text-muted)" }}>
        No path loops forever and none fails silently — the two ways an autonomous system
        usually burns money. When a run stops short, it says which step it reached and why.
      </p>

      <ol className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {PIPELINE.map(([name, blurb], index) => (
          <li key={name}>
            <SoftCard className="h-full p-4" size="md">
              <div className="flex items-baseline gap-2">
                <span className="text-[11px] font-semibold" style={{ color: "var(--text-faint)" }}>
                  {String(index + 1).padStart(2, "0")}
                </span>
                <h3 className="text-sm font-semibold">{name}</h3>
              </div>
              <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
                {blurb}
              </p>
            </SoftCard>
          </li>
        ))}
      </ol>

      {/* --------------------------------------------------------------- */}

      <h2 className="mt-20 max-w-[30ch] text-[30px] font-semibold tracking-tight">
        What you get, and what you do not.
      </h2>

      <div className="mt-8 grid gap-4 lg:grid-cols-2">
        <SoftCard className="p-6" size="lg">
          <h3 className="text-sm font-semibold">An audit of the site you already have</h3>
          <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
            Titles, meta descriptions, headings, thin pages, missing structured data — plus
            the problems no single page can show you: the same title on four pages, and
            pages nothing links to. The fastest wins are here, not in new content: a new
            article takes six to twelve weeks to rank at best.
          </p>
        </SoftCard>

        <SoftCard className="p-6" size="lg">
          <h3 className="text-sm font-semibold">A post shaped for each channel</h3>
          <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
            The claim stays identical across channels; only the register and the length
            change. Lengths and hashtag counts are enforced in code, not asked of a model,
            because counting is arithmetic — and a post the platform would reject is not a
            deliverable.
          </p>
        </SoftCard>

        <SoftCard className="p-6" size="lg">
          <h3 className="text-sm font-semibold">Nothing goes out without you</h3>
          <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
            Publishing sits after the review step in the machine and is unreachable
            without an approval, so that is a property of how it is built rather than a
            promise on a page. Claims you forbid are checked twice — on the article, and
            again on every post after it is trimmed.
          </p>
        </SoftCard>

        <SoftCard className="p-6" size="lg">
          <h3 className="text-sm font-semibold">Measured in clicks, not vibes</h3>
          <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
            Every call to action carries a link we own, so a click is attributable to the
            piece that earned it whether we posted it or you pasted it. Your visibility in
            AI answers is sampled too — a sample of what a few models say, never a census.
          </p>
        </SoftCard>
      </div>

      {/* --------------------------------------------------------------- */}

      {/*
        The honest limits, on the front page rather than discovered after signing up. A
        visitor who finds this out later feels misled; one who reads it here knows what
        they are getting, and it is the part of this page most likely to earn trust.
      */}
      <SoftCard className="mt-16 p-6" size="lg">
        <h2 className="text-sm font-semibold">Straight about what it cannot do yet</h2>
        <ul className="mt-3 space-y-2.5 text-sm" style={{ color: "var(--text-muted)" }}>
          <li>
            <strong style={{ color: "var(--text)" }}>Posting directly to your accounts
            needs each platform&rsquo;s approval.</strong>{" "}
            Facebook, Instagram, LinkedIn and TikTok each review an app before it may post
            on someone&rsquo;s behalf — weeks of their queue, and refusable. Until then you
            get the finished post to paste, which works on every channel including the ones
            no API can reach.
          </li>
          <li>
            <strong style={{ color: "var(--text)" }}>It does not make images or video.</strong>{" "}
            This is a text and research engine. For video channels the deliverable is a
            shootable script, not a claim that we filmed something.
          </li>
          <li>
            <strong style={{ color: "var(--text)" }}>Google movement takes six to twelve
            weeks.</strong>{" "}
            Anyone promising traffic next week is selling you reach, not customers. What is
            reported here are the leading indicators, and they are labelled as such.
          </li>
        </ul>
      </SoftCard>

      <div className="mt-12 flex flex-wrap items-center gap-3 border-t pt-8" style={{ borderColor: "var(--edge)" }}>
        <Link
          href={signedIn ? "/dashboard" : "/login"}
          className="soft-edge inline-flex items-center px-5 py-2.5 text-sm font-semibold"
          style={{
            borderRadius: "var(--r-pill)",
            background: "var(--primary)",
            color: "var(--primary-ink)",
          }}
        >
          {signedIn ? "Go to your dashboard" : "Create an account"}
        </Link>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          No card, no trial timer — this is a working build, not a storefront.
        </p>
      </div>
    </Shell>
  );
}
