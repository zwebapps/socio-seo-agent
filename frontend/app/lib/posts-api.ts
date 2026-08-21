/**
 * The post queue: the calendar's read and the four actions on a post.
 *
 * `simulated` on a publish response is the field this module exists to keep honest.
 * With no publisher configured — every deployment until a platform's App Review is
 * approved — a publish "succeeds" without anything leaving the process, and a screen
 * that rendered that as sent would be lying to the person who pressed the button. It is
 * a separate field from `status` precisely so a caller cannot forget it.
 */

import { request } from "@/app/lib/api";

/** The six states a post can be in. Mirrors `SocialPostStatus` in `db/models.py`. */
export type PostStatus =
  | "queued"
  | "scheduled"
  | "published"
  | "failed"
  | "refused"
  | "cancelled";

export type Post = {
  id: string;
  contentPieceId: string;
  platform: string;
  body: string;
  hashtags: string[];
  status: PostStatus;
  /** `null` for a queued post: rendered, not yet timed. */
  scheduledAt: string | null;
  publishedAt: string | null;
  createdAt: string;
  pieceTitle: string | null;
};

export type PublishOutcome = {
  post: Post;
  status: PostStatus;
  /** True when nothing left this process. Render it differently from a real send. */
  simulated: boolean;
  externalRef: string | null;
  error: string | null;
};

/**
 * The queue for a window, plus every untimed post.
 *
 * The window is inclusive of untimed posts by design on the server — a queued post has
 * no date, so filtering them out would hide exactly the backlog the calendar helps
 * place. Passing no window returns everything up to the server's cap.
 */
export function fetchPosts(from?: Date, to?: Date): Promise<{ posts: Post[] }> {
  const query = new URLSearchParams();
  if (from) query.set("from", from.toISOString());
  if (to) query.set("to", to.toISOString());
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<{ posts: Post[] }>(`/api/v1/posts${suffix}`);
}

/** Put an approved run's channel posts on the calendar. */
export function queueFromRun(runId: string, scheduledAt?: Date): Promise<{ posts: Post[] }> {
  return request<{ posts: Post[] }>(`/api/v1/posts/from-run/${runId}`, {
    method: "POST",
    body: JSON.stringify(scheduledAt ? { scheduledAt: scheduledAt.toISOString() } : {}),
  });
}

/** Give a post a time, or pass `null` to move it back to the untimed queue. */
export function schedulePost(postId: string, when: Date | null): Promise<Post> {
  return request<Post>(`/api/v1/posts/${postId}/schedule`, {
    method: "POST",
    // An ISO string, so it carries its offset: the API refuses a naive instant, because
    // one read as local time shifts every slot by the host's offset and still looks
    // like a valid schedule.
    body: JSON.stringify({ when: when ? when.toISOString() : null }),
  });
}

export function cancelPost(postId: string): Promise<Post> {
  return request<Post>(`/api/v1/posts/${postId}/cancel`, { method: "POST" });
}

export function publishPost(postId: string): Promise<PublishOutcome> {
  return request<PublishOutcome>(`/api/v1/posts/${postId}/publish`, { method: "POST" });
}

/** Channel ids as the product stores them, in the words a person uses. */
export function platformLabel(platform: string): string {
  const names: Record<string, string> = {
    linkedin: "LinkedIn",
    facebook: "Facebook",
    instagram: "Instagram",
    x: "X",
    email: "Email",
    blog_article: "Article",
    link_hub: "Link hub",
  };
  return names[platform] ?? platform;
}

/**
 * The tone a status should be rendered in.
 *
 * `refused` is deliberately NOT an error tone. Every social publish is refused today
 * because posting for other people is gated on App Review, so painting the ordinary
 * state red would make the whole calendar look broken — and would train the owner to
 * ignore the colour that matters when something genuinely fails.
 */
export function statusTone(status: PostStatus): "ok" | "accent" | "warn" | "err" | "muted" {
  switch (status) {
    case "published":
      return "ok";
    case "scheduled":
      return "accent";
    case "failed":
      return "err";
    case "refused":
      return "warn";
    default:
      return "muted";
  }
}

/**
 * The days of a month, padded to whole weeks starting Monday.
 *
 * Exported for its own test: an off-by-one here silently puts every post on the wrong
 * weekday, which looks like a scheduling bug rather than a grid bug. Monday-first
 * because the product's locale default is `de` and a German calendar starts on Monday —
 * `getDay()` returns 0 for Sunday, which is the off-by-one waiting to happen.
 */
export function monthGrid(year: number, month: number): Date[] {
  const first = new Date(Date.UTC(year, month, 1));
  // 0 for Monday .. 6 for Sunday.
  const lead = (first.getUTCDay() + 6) % 7;
  const start = new Date(Date.UTC(year, month, 1 - lead));

  const days: Date[] = [];
  // Six weeks always, rather than a variable number: a grid that changes height as you
  // page through months makes the whole panel jump, and a 31-day month starting on a
  // Sunday genuinely needs six rows.
  for (let index = 0; index < 42; index += 1) {
    days.push(new Date(start.getTime() + index * 86_400_000));
  }
  return days;
}

/** `YYYY-MM-DD` in UTC, for grouping posts onto a day without a timezone shift. */
export function dayKey(value: Date | string): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return date.toISOString().slice(0, 10);
}
