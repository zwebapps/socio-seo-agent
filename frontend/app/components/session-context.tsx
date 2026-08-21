"use client";

/**
 * Who is signed in, fetched ONCE per page and shared.
 *
 * `SessionBar` already fetched `/auth/me`, and the new sidebar needs the same answer to
 * decide whether to show the operator section. Two components each fetching it means two
 * requests for one fact on every navigation, and — worse — two components that can
 * disagree about whether you are signed in while one of them is still in flight.
 *
 * So the fetch moves here, into a provider in the root layout, and both consume it. This
 * is also the seam a third consumer should use rather than adding a third fetch.
 *
 * A client component, and it has to be. The call carries the session cookie and the
 * API's Origin-CSRF guard refuses a cookie-bearing request that arrives with no `Origin`
 * header, which is exactly what `fetch` from a server component sends.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

export type Me = {
  id: string;
  email: string;
  role: string;
  businessId: string | null;
};

export type SessionState =
  /** The answer has not arrived. NOT the same as anonymous — see the consumers. */
  | { kind: "loading" }
  | { kind: "anonymous" }
  | { kind: "signed-in"; me: Me };

type Session = {
  state: SessionState;
  /** Re-read `/auth/me`. For after a sign-in or a business being created. */
  refresh: () => void;
};

const SessionContext = createContext<Session>({ state: { kind: "loading" }, refresh: () => {} });

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<SessionState>({ kind: "loading" });
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const response = await fetch(`${API_URL}/api/v1/auth/me`, { credentials: "include" });
        if (!live) return;
        if (!response.ok) {
          // 401 is the ordinary case for a visitor, not an error worth surfacing.
          setState({ kind: "anonymous" });
          return;
        }
        setState({ kind: "signed-in", me: (await response.json()) as Me });
      } catch {
        // An unreachable API is the login page's story to tell, not this provider's.
        if (live) setState({ kind: "anonymous" });
      }
    })();
    return () => {
      live = false;
    };
  }, [nonce]);

  return (
    <SessionContext.Provider value={{ state, refresh }}>{children}</SessionContext.Provider>
  );
}

export function useSession(): Session {
  return useContext(SessionContext);
}

/** Whether this account may see the operator screens. NOT the authorisation decision. */
export function isOperator(state: SessionState): boolean {
  // The server re-checks the role on every `/api/v1/admin/*` call — see
  // `docs/ARCHITECTURE.md` §14. This only decides whether to show a link to a screen
  // the account cannot use anyway, and a second authorisation check in the browser
  // would put a decision somewhere that cannot enforce it.
  return state.kind === "signed-in" && state.me.role === "platform_admin";
}
