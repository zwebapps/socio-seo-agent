/**
 * Where each role lands after signing in, defined once and explicitly.
 *
 * The bug this replaces: `login/page.tsx` sent EVERY account to
 * `/developer/models`, so a business owner signing in arrived on the platform
 * operator's model-routing screen and was met with "Not available on this account".
 * The role was in the login response the whole time and nothing read it. An owner's
 * home is the business dashboard; the admin screens are the operator's.
 *
 * A map rather than a chain of `if`s, and exported rather than inlined, because two
 * places already need it — the login form and the signup branch — and a second copy
 * is how they start disagreeing about where an owner belongs.
 */

/** Roles as `backend/app/db/models.py::Role` spells them. */
export type Role = "member" | "owner" | "platform_admin";

/**
 * Role → the screen that role's work starts on.
 *
 * `member` maps with `owner` deliberately: the role exists in the enum and the DB
 * check constraint, nothing in the codebase creates or checks one, and until it means
 * something a member is a business person and belongs on the business dashboard —
 * never silently on the operator's.
 */
export const ROLE_LANDING: Record<Role, string> = {
  member: "/dashboard",
  owner: "/dashboard",
  platform_admin: "/developer/models",
};

/** The business dashboard: the safe answer for any role we do not recognise. */
const DEFAULT_LANDING = "/dashboard";

export function landingFor(role: string | null | undefined): string {
  if (role && role in ROLE_LANDING) return ROLE_LANDING[role as Role];
  // An unknown role is a server that has grown a role this build has not heard of.
  // The business dashboard is the honest fallback: it works for everyone, and it
  // never shows somebody a screen their account cannot use.
  return DEFAULT_LANDING;
}

/**
 * A `?next=` destination, but only if it is a path on this site.
 *
 * Unguarded this was an open redirect: `/login?next=https://evil.example` sent the
 * visitor there immediately after they authenticated, which is the most valuable
 * moment to phish somebody. Only a single-slash-rooted relative path is accepted —
 * `//evil.example` is protocol-relative and is a different site, which is exactly the
 * case a naive `startsWith("/")` check lets through.
 */
export function safeNext(raw: string | null): string | null {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return null;
  // A backslash is normalised to a forward slash by some browsers, so `/\evil.example`
  // can also leave the site.
  if (raw.startsWith("/\\")) return null;
  return raw;
}
