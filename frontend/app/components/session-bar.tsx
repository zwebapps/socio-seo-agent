"use client";

/**
 * Who you are signed in as, and the way out.
 *
 * `POST /api/v1/auth/logout` has existed since the auth work landed and NOTHING in the
 * UI called it. There was no sign-out control on any screen and no global navigation, so
 * a signed-in session could only be ended by clearing a cookie by hand -- and switching
 * between an owner account and a platform-admin one meant typing `/login` into the
 * address bar from memory.
 *
 * A client component, and it has to be. The API's Origin-CSRF guard refuses a
 * cookie-bearing write that arrives with no `Origin` header, and `fetch` from a server
 * component sends none -- the same reason `app/lib/admin-api.ts` carries that warning.
 *
 * It renders NOTHING when nobody is signed in. A "Sign out" button on the login screen
 * is noise at best, and at worst it suggests the session outlived a logout that worked.
 */

import { useEffect, useState } from "react";

import { SoftButton } from "@/app/components/soft";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

type Me = { email: string; role: string };

type State =
  | { kind: "loading" }
  | { kind: "anonymous" }
  | { kind: "signed-in"; me: Me }
  | { kind: "leaving" };

export function SessionBar() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`${API_URL}/api/v1/auth/me`, {
          credentials: "include",
        });
        if (cancelled) return;
        if (!response.ok) {
          // 401 is the ordinary case for a visitor, not an error worth showing.
          setState({ kind: "anonymous" });
          return;
        }
        setState({ kind: "signed-in", me: (await response.json()) as Me });
      } catch {
        // The API being unreachable is the login page's story to tell, not this bar's.
        if (!cancelled) setState({ kind: "anonymous" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function signOut() {
    setState({ kind: "leaving" });
    try {
      await fetch(`${API_URL}/api/v1/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Deliberately ignored. Logout revokes EVERY session for the user server-side, so
      // a failed request is worth retrying by landing on the login page rather than
      // leaving somebody stuck on a screen they wanted to leave. The next request will
      // be refused anyway if the revocation did land.
    }
    // A full navigation, not a router push: every page here holds fetched state, and a
    // client-side transition would leave the previous account's data on screen under a
    // logged-out session.
    window.location.href = "/login";
  }

  if (state.kind === "loading" || state.kind === "anonymous") return null;

  const me = state.kind === "signed-in" ? state.me : null;

  return (
    <div
      className="mx-auto flex w-full max-w-[1800px] flex-wrap items-center justify-end gap-3 px-6 pt-6 lg:px-10 xl:px-14"
      // `aria-live` so the sign-out transition is announced rather than silently
      // replacing the row a screen-reader user was on.
      aria-live="polite"
    >
      {me ? (
        <span className="text-sm" style={{ color: "var(--text-muted)" }}>
          Signed in as <strong style={{ color: "var(--text)" }}>{me.email}</strong>
          {me.role === "platform_admin" ? " · platform admin" : null}
        </span>
      ) : null}
      <SoftButton
        onClick={signOut}
        variant="quiet"
        disabled={state.kind === "leaving"}
        ariaLabel={me ? `Sign out of ${me.email}` : "Sign out"}
      >
        {state.kind === "leaving" ? "Signing out…" : "Sign out"}
      </SoftButton>
    </div>
  );
}
