"use client";

/**
 * The post calendar, and the panel that acts on one post.
 *
 * Two decisions about honesty shape most of this file.
 *
 * **A date on this grid is a plan, not a promise.** Nothing in this product wakes up and
 * sends a scheduled post — there is no worker yet — and posting on behalf of other
 * people is gated on each platform's App Review. So the calendar states plainly, once,
 * near the top, that a time is a note to yourself and publishing is a button somebody
 * presses. A grid that implied automatic delivery would be asserting a capability the
 * system does not have, which is the failure this codebase is most careful about.
 *
 * **A simulated publish is never rendered as a send.** The publish response carries
 * `simulated` separately from `status` exactly so this component cannot conflate them,
 * and the outcome line says which happened in the words the refusal itself used.
 *
 * `refused` is rendered amber rather than red on purpose: every social publish is
 * refused today, so painting the ordinary state as an error would make the whole screen
 * look broken and would train the owner to ignore the colour that matters when
 * something genuinely fails.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { Pill, SoftButton, SoftCard } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import {
  cancelPost,
  dayKey,
  fetchPosts,
  monthGrid,
  platformLabel,
  publishPost,
  schedulePost,
  statusTone,
  type Post,
  type PublishOutcome,
} from "@/app/lib/posts-api";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

type State =
  | { kind: "loading" }
  | { kind: "ready"; posts: Post[] }
  | { kind: "error"; message: string };

export function PostCalendar() {
  const [month, setMonth] = useState(() => {
    const now = new Date();
    return { year: now.getUTCFullYear(), month: now.getUTCMonth() };
  });
  const [state, setState] = useState<State>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    // Deliberately NOT `setState({kind: "loading"})` on a refresh. Emptying the list for
    // the duration of the fetch unmounts the selected post, which resets the detail
    // panel and throws away the publish outcome that was just put there — so pressing
    // "Post now" showed the new status and silently lost the REASON, which is the part
    // the owner needs. Only the first load shows a loading state.
    setState((current) => (current.kind === "ready" ? current : { kind: "loading" }));
    try {
      // No window passed: the read is capped server-side, and asking for a month would
      // drop the untimed backlog this screen exists to help place.
      const { posts } = await fetchPosts();
      setState({ kind: "ready", posts });
    } catch (exc) {
      setState({
        kind: "error",
        message: exc instanceof ApiError ? exc.message : "The queue could not be loaded.",
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const posts = state.kind === "ready" ? state.posts : [];
  const byDay = useMemo(() => {
    const map = new Map<string, Post[]>();
    for (const post of posts) {
      if (!post.scheduledAt) continue;
      const key = dayKey(post.scheduledAt);
      map.set(key, [...(map.get(key) ?? []), post]);
    }
    return map;
  }, [posts]);

  const untimed = useMemo(
    () => posts.filter((post) => !post.scheduledAt && post.status !== "cancelled"),
    [posts],
  );

  const days = useMemo(() => monthGrid(month.year, month.month), [month]);
  const chosen = posts.find((post) => post.id === selected) ?? null;
  const monthName = new Date(Date.UTC(month.year, month.month, 1)).toLocaleString("en", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });

  function shift(by: number) {
    setMonth((current) => {
      const next = new Date(Date.UTC(current.year, current.month + by, 1));
      return { year: next.getUTCFullYear(), month: next.getUTCMonth() };
    });
  }

  return (
    <section aria-labelledby="calendar-heading" className="mt-10">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="calendar-heading" className="text-sm font-semibold">
          {monthName}
        </h2>
        <div className="flex items-center gap-2">
          <SoftButton onClick={() => shift(-1)} variant="quiet" ariaLabel="Previous month">
            ←
          </SoftButton>
          <SoftButton onClick={() => shift(1)} variant="quiet" ariaLabel="Next month">
            →
          </SoftButton>
          <SoftButton onClick={() => void load()} variant="quiet" ariaLabel="Reload the queue">
            Refresh
          </SoftButton>
        </div>
      </div>

      {/*
        Said once, near the top, and not buried in a tooltip. A grid of dates implies
        something will happen at those times, and nothing here does that yet: there is no
        worker, and every social publish is gated on the platform's own App Review.
      */}
      <p className="mt-2 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
        A date here is a note to yourself, not an automatic send — nothing publishes on a
        schedule yet. Publishing is the button on a post, and it reports exactly what the
        platform did.
      </p>

      {state.kind === "error" && (
        <SoftCard className="mt-4 p-5" size="md">
          <p role="alert" className="text-sm font-medium" style={{ color: "var(--err)" }}>
            {state.message}
          </p>
        </SoftCard>
      )}

      <div className="mt-4 grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
        <div>
          <div className="grid grid-cols-7 gap-1.5">
            {WEEKDAYS.map((day) => (
              <div
                key={day}
                className="px-1 pb-1 text-[10px] font-semibold uppercase tracking-wider"
                style={{ color: "var(--text-faint)" }}
              >
                {day}
              </div>
            ))}
            {days.map((day) => {
              const key = dayKey(day);
              const dayPosts = byDay.get(key) ?? [];
              const inMonth = day.getUTCMonth() === month.month;
              return (
                <div
                  key={key}
                  className="soft-edge min-h-[5.5rem] p-1.5"
                  style={{
                    borderRadius: "var(--r-sm)",
                    // Days outside the month are dimmed rather than blank: an empty cell
                    // reads as a gap in the calendar, and a dimmed date reads as
                    // belonging to the next month, which is what it is.
                    opacity: inMonth ? 1 : 0.4,
                  }}
                >
                  <div className="text-[10px]" style={{ color: "var(--text-faint)" }}>
                    {day.getUTCDate()}
                  </div>
                  <ul className="mt-1 space-y-1">
                    {dayPosts.map((post) => (
                      <li key={post.id}>
                        <button
                          type="button"
                          onClick={() => setSelected(post.id)}
                          className="soft-edge block w-full px-1.5 py-1 text-left text-[10px] font-semibold"
                          style={{
                            borderRadius: "var(--r-sm)",
                            background:
                              selected === post.id ? "var(--primary)" : "var(--surface-raised)",
                            color: selected === post.id ? "var(--primary-ink)" : "var(--text)",
                          }}
                        >
                          {platformLabel(post.platform)}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>

          <h3 className="mt-6 text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
            Not scheduled ({untimed.length})
          </h3>
          {/*
            Its own column rather than being hidden. A queued post carries no date, so a
            calendar that only drew dated posts would hide the entire backlog — which is
            the thing this screen is meant to help place.
          */}
          {untimed.length === 0 ? (
            <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
              Nothing waiting. Posts arrive here when you queue an approved run.
            </p>
          ) : (
            <ul className="mt-2 flex flex-wrap gap-2">
              {untimed.map((post) => (
                <li key={post.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(post.id)}
                    className="soft-edge px-3 py-1.5 text-xs font-semibold"
                    style={{
                      borderRadius: "var(--r-pill)",
                      background:
                        selected === post.id ? "var(--primary)" : "var(--surface-raised)",
                      color: selected === post.id ? "var(--primary-ink)" : "var(--text)",
                    }}
                  >
                    {platformLabel(post.platform)}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <PostDetail post={chosen} onChanged={load} />
      </div>
    </section>
  );
}

function PostDetail({ post, onChanged }: { post: Post | null; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<PublishOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [when, setWhen] = useState("");

  // Clearing on selection change is not cosmetic: leaving the previous post's publish
  // outcome on screen beside a different post's body would attribute one post's result
  // to another.
  useEffect(() => {
    setOutcome(null);
    setError(null);
    setWhen("");
  }, [post?.id]);

  if (!post) {
    return (
      <SoftCard className="p-5" size="lg">
        <h3 className="text-sm font-semibold">No post selected</h3>
        <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
          Pick one from the calendar or the not-scheduled list to publish, time or cancel
          it.
        </p>
      </SoftCard>
    );
  }

  async function run(label: string, action: () => Promise<unknown>) {
    setBusy(label);
    setError(null);
    try {
      const result = await action();
      if (result && typeof result === "object" && "simulated" in result) {
        setOutcome(result as PublishOutcome);
      }
      onChanged();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : `${label} did not work.`);
    } finally {
      setBusy(null);
    }
  }

  const full = post.hashtags.length > 0 ? `${post.body}\n\n${post.hashtags.join(" ")}` : post.body;
  const canAct = post.status !== "published" && post.status !== "cancelled";

  return (
    <SoftCard className="p-5" size="lg">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{platformLabel(post.platform)}</h3>
        <Pill tone={statusTone(post.status)}>{post.status}</Pill>
      </div>
      {post.pieceTitle && (
        <p className="mt-1 text-xs" style={{ color: "var(--text-muted)" }}>
          {post.pieceTitle}
        </p>
      )}

      <p className="mt-3 whitespace-pre-wrap text-sm">{post.body}</p>
      {post.hashtags.length > 0 && (
        <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
          {post.hashtags.join(" ")}
        </p>
      )}

      {canAct && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <SoftButton
              onClick={() => void run("Publish", () => publishPost(post.id))}
              variant="primary"
              disabled={busy !== null}
            >
              {busy === "Publish" ? "Publishing…" : "Post now"}
            </SoftButton>
            {/* Copy stays beside publish rather than being replaced by it: pasting is
                the path that works on every channel today, including the ones no API can
                reach at all. */}
            <SoftButton
              onClick={() => void navigator.clipboard.writeText(full)}
              variant="quiet"
            >
              Copy
            </SoftButton>
            <SoftButton
              onClick={() => void run("Cancel", () => cancelPost(post.id))}
              variant="quiet"
              disabled={busy !== null}
            >
              Cancel post
            </SoftButton>
          </div>

          <div className="mt-4">
            <label
              className="block text-[11px] font-semibold uppercase tracking-wider"
              style={{ color: "var(--text-muted)" }}
            >
              Planned time
              <input
                type="datetime-local"
                value={when}
                onChange={(event) => setWhen(event.target.value)}
                className="soft-sunken soft-edge mt-1 block w-full px-2 py-1.5 text-sm font-normal normal-case tracking-normal"
                style={{ borderRadius: "var(--r-sm)", color: "var(--text)" }}
              />
            </label>
            <div className="mt-2 flex flex-wrap gap-2">
              <SoftButton
                onClick={() =>
                  void run("Schedule", () =>
                    // `new Date(local)` attaches the browser's offset, so the API receives
                    // an instant rather than a wall-clock time. It refuses a naive one.
                    schedulePost(post.id, when ? new Date(when) : null),
                  )
                }
                variant="quiet"
                disabled={busy !== null || !when}
              >
                {busy === "Schedule" ? "Saving…" : "Set time"}
              </SoftButton>
              {post.scheduledAt && (
                <SoftButton
                  onClick={() => void run("Unschedule", () => schedulePost(post.id, null))}
                  variant="quiet"
                  disabled={busy !== null}
                >
                  Clear time
                </SoftButton>
              )}
            </div>
          </div>
        </>
      )}

      {/* `aria-live` so the result of pressing publish is announced. A screen-reader user
          who pressed the button and heard nothing has no way to learn what happened, and
          this is the only place the answer exists. */}
      <div aria-live="polite" className="mt-4">
        {outcome && <Outcome outcome={outcome} />}
        {error && (
          <p role="alert" className="text-sm font-medium" style={{ color: "var(--err)" }}>
            {error}
          </p>
        )}
      </div>
    </SoftCard>
  );
}

/**
 * What actually happened, in the words the refusal used.
 *
 * A 200 from the publish route does NOT mean the post went out — `simulated` is the
 * field that says so, and it is rendered before anything else because it is the part a
 * reader would otherwise assume.
 */
function Outcome({ outcome }: { outcome: PublishOutcome }) {
  if (outcome.simulated) {
    return (
      <div>
        <p className="text-sm font-semibold" style={{ color: "var(--warn)" }}>
          Nothing was sent.
        </p>
        <p className="mt-1 text-xs" style={{ color: "var(--text-muted)" }}>
          {outcome.error ??
            "No publisher is connected for this platform, so this was simulated. The post is still in the queue."}
        </p>
      </div>
    );
  }
  if (outcome.status === "published") {
    return (
      <div>
        <p className="text-sm font-semibold" style={{ color: "var(--ok)" }}>
          Published.
        </p>
        {outcome.externalRef && (
          <p className="mt-1 break-all text-xs" style={{ color: "var(--text-muted)" }}>
            {outcome.externalRef}
          </p>
        )}
      </div>
    );
  }
  return (
    <div>
      <p className="text-sm font-semibold" style={{ color: "var(--err)" }}>
        {outcome.status === "refused" ? "Refused." : "Failed."}
      </p>
      <p className="mt-1 text-xs" style={{ color: "var(--text-muted)" }}>
        {outcome.error ?? "The platform gave no reason."}
      </p>
    </div>
  );
}
