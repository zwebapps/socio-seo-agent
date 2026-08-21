"use client";

/**
 * `/content` — the social posts the agent wrote, per channel, ready to use.
 *
 * The gap this fills: REPACK writes a post for every channel and the ONLY place they
 * could be seen was a tab inside one run's review screen. There was no answer to "show
 * me my content" — the thing the product is for. A run is a process; this page is the
 * output.
 *
 * **Copy is the primary action, and that is not a limitation to apologise for.** Direct
 * publishing to LinkedIn, Facebook, Instagram and TikTok needs per-platform App Review —
 * weeks of somebody else's approval queue, not code — so `actuators/social.py` honestly
 * refuses. Pasting a finished, channel-shaped post works today, on every channel,
 * including the ones no API can reach. The copy button IS the working path.
 *
 * A client component: every call carries the session cookie, and the API's Origin-CSRF
 * guard refuses a cookie-bearing request that arrives with no `Origin` header, which is
 * what `fetch` from a server component sends.
 *
 * Reads only endpoints that already exist: the runs list, then the review projection
 * per run (which is where `renderings` is already turned into per-channel posts).
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/app/components/page-shell";
import { Pill, SoftButton, SoftCard } from "@/app/components/soft";
import { fetchReview, type SocialPost } from "@/app/lib/review-api";
import { fetchRuns, type RunSummary } from "@/app/lib/runs-api";

/**
 * How many recent runs to pull posts from.
 *
 * Bounded because each one is its own review fetch, and a review payload carries the
 * whole draft. Six is enough to cover a fortnight of a weekly cadence and cheap enough
 * to load on one screen.
 */
const RUNS_TO_SCAN = 6;

type RunPosts = {
  run: RunSummary;
  posts: SocialPost[];
};

type State =
  | { kind: "loading" }
  | { kind: "ready"; groups: RunPosts[] }
  | { kind: "error"; message: string };

export default function ContentPage() {
  const [state, setState] = useState<State>({ kind: "loading" });

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const page = await fetchRuns(RUNS_TO_SCAN);
      const runs = page.runs ?? [];

      // Sequential rather than parallel, deliberately: this is a handful of requests
      // against our own API and firing them at once buys a few hundred milliseconds in
      // exchange for a burst that the per-user rate limiter is entitled to refuse.
      const groups: RunPosts[] = [];
      for (const run of runs) {
        try {
          const review = await fetchReview(run.runId);
          const posts = review.social ?? [];
          if (posts.length > 0) groups.push({ run, posts });
        } catch {
          // One unreadable run must not cost the others. A run that failed before
          // REPACK legitimately has no posts, and that is not an error to report here.
        }
      }
      setState({ kind: "ready", groups });
    } catch (exc) {
      setState({
        kind: "error",
        message: exc instanceof Error ? exc.message : "Could not load your content.",
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Shell className="py-14">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Your content
      </p>
      <h1 className="mt-2 text-[28px] font-semibold tracking-tight">Social posts</h1>
      <p className="mt-3 max-w-[70ch] text-base" style={{ color: "var(--text-muted)" }}>
        Every post the agent wrote, shaped for its channel — the claim is identical across
        them, only the register and the length change. Copy one and paste it; that works on
        every channel today, including the ones no API can publish to.
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

      {state.kind === "ready" && state.groups.length === 0 && (
        <SoftCard className="mt-10 p-6" size="lg">
          <h2 className="text-sm font-semibold">No posts yet</h2>
          <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
            Posts are written in the REPACK step, near the end of a run. If a run stopped
            earlier than that, its timeline says which step it reached and why.
          </p>
          <Link
            href="/"
            className="mt-4 inline-block text-sm font-medium underline"
            style={{ color: "var(--primary)" }}
          >
            Start a run
          </Link>
        </SoftCard>
      )}

      {state.kind === "ready" && state.groups.length > 0 && (
        <div className="mt-10 space-y-10">
          {state.groups.map((group) => (
            <section key={group.run.runId} aria-labelledby={`run-${group.run.runId}`}>
              <div className="flex flex-wrap items-baseline justify-between gap-3">
                <h2 id={`run-${group.run.runId}`} className="text-sm font-semibold">
                  {group.run.goal}
                </h2>
                <Link
                  href={`/runs/${group.run.runId}`}
                  className="text-xs font-medium underline"
                  style={{ color: "var(--primary)" }}
                >
                  Open the run
                </Link>
              </div>
              <div className="mt-4 grid gap-5 md:grid-cols-2 xl:grid-cols-3">
                {group.posts.map((post) => (
                  <PostCard key={`${group.run.runId}-${post.channel}`} post={post} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </Shell>
  );
}

function PostCard({ post }: { post: SocialPost }) {
  const [copied, setCopied] = useState(false);

  // Body and hashtags together: that is what gets pasted, and a copy button that gave
  // only the body would silently drop the tags the model was asked to produce.
  const full = post.hashtags.length > 0 ? `${post.body}\n\n${post.hashtags.join(" ")}` : post.body;

  async function copy() {
    try {
      await navigator.clipboard.writeText(full);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be refused (an insecure origin, or a denied permission).
      // Saying so beats a button that appears to work: the text is on screen and can
      // still be selected by hand.
      setCopied(false);
    }
  }

  return (
    <SoftCard className="flex flex-col p-5" size="lg">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider">
          {label(post.channel)}
        </h3>
        <div className="flex items-center gap-2">
          {/* Over the EDITORIAL target is not over the platform's limit, and the two
              deserve different words: a 1,900-character LinkedIn post publishes fine and
              is simply longer than it should be. Nothing here was truncated to it. */}
          {post.overTarget && <Pill>long</Pill>}
          {/* The server's count, not `body.length`. `SocialPost.characters` is
              measured server-side precisely so the number on screen is the number the
              limit was enforced against. */}
          <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>
            {post.characters}
            {post.characterLimit ? ` / ${post.characterLimit}` : ""} chars
          </span>
        </div>
      </div>

      <p className="mt-3 flex-1 whitespace-pre-wrap text-sm" style={{ color: "var(--text)" }}>
        {post.body}
      </p>

      {post.hashtags.length > 0 && (
        <p className="mt-3 text-xs" style={{ color: "var(--text-muted)" }}>
          {post.hashtags.join(" ")}
        </p>
      )}

      <div className="mt-4 flex items-center gap-3">
        <SoftButton onClick={() => void copy()} variant="primary">
          {copied ? "Copied" : "Copy post"}
        </SoftButton>
        {/* `aria-live` so the confirmation is announced rather than only appearing on
            the button face, which a screen-reader user has already moved past. */}
        <span aria-live="polite" className="text-xs" style={{ color: "var(--text-muted)" }}>
          {copied ? "Ready to paste." : ""}
        </span>
      </div>
    </SoftCard>
  );
}

/** Channel ids as the product stores them, in the words a person uses. */
function label(channel: string): string {
  const names: Record<string, string> = {
    linkedin: "LinkedIn",
    facebook: "Facebook",
    instagram: "Instagram",
    x: "X",
    email: "Email",
    blog_article: "Article",
    link_hub: "Link hub",
  };
  return names[channel] ?? channel;
}
