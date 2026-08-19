"use client";

/**
 * The load/error/save plumbing every developer screen needs, written once.
 *
 * `/developer/models` grew this pattern inline — `load()`, `withBusy()`, and a 90-line
 * error card that distinguishes 401 from 403 from a dead API. Three more screens using
 * the same API deserve the same behaviour, and three more copies of it is three chances
 * for one to drift into showing a login link to somebody who is already logged in.
 *
 * The error card is the part worth keeping identical. Its three cases are three
 * different actions for the reader, not three shades of failure:
 *
 * - `not_authenticated` (401): sign in. A link, because the fix is elsewhere.
 * - `forbidden` (403): this account cannot, and no amount of retrying changes that. NO
 *   retry button, and no login link — sending someone who is already signed in to the
 *   login page is a loop they cannot escape.
 * - anything else: retry, plus how to start the API if it is simply not running.
 */

import { useCallback, useEffect, useState } from "react";
import { Pill, SoftButton, SoftCard } from "../components/soft";
import { ApiError } from "../lib/api";

export type LoadError = { code: string; message: string };

export type Resource<T> = {
  data: T | null;
  error: LoadError | null;
  /** Key of the row currently saving, or null. */
  busy: string | null;
  /** Key of the row that just saved, cleared after a moment. */
  saved: string | null;
  reload: () => Promise<void>;
  /** Run a save keyed by row, then reload so the screen shows the server's own account. */
  save: (key: string, work: () => Promise<unknown>) => Promise<void>;
};

export function useAdminResource<T>(fetcher: () => Promise<T>): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<LoadError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setError(null);
    try {
      setData(await fetcher());
    } catch (e) {
      const err = e as ApiError;
      setError({ code: err.code ?? "unknown", message: err.message });
    }
  }, [fetcher]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const save = useCallback(
    async (key: string, work: () => Promise<unknown>) => {
      setBusy(key);
      setSaved(null);
      try {
        await work();
        // Reload rather than patching local state: the next paint is then the server's
        // account of what is in force, which is what keeps two open tabs from
        // disagreeing — and on these screens the server also RECOMPUTES things (the
        // reserved-USD figure, the effective tool set) that the client cannot derive.
        await reload();
        setSaved(key);
        window.setTimeout(() => setSaved(null), 2500);
      } catch (e) {
        const err = e as ApiError;
        setError({ code: err.code ?? "unknown", message: err.message });
      } finally {
        setBusy(null);
      }
    },
    [reload],
  );

  return { data, error, busy, saved, reload, save };
}

export function ErrorCard({
  error,
  onRetry,
  returnTo,
}: {
  error: LoadError;
  onRetry: () => void;
  /** Path to come back to after signing in. */
  returnTo: string;
}) {
  const terminal = error.code === "not_authenticated" || error.code === "forbidden";

  return (
    <SoftCard className="mb-8 p-5" size="md">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-sm font-semibold" style={{ color: "var(--err)" }}>
            {error.code === "not_authenticated"
              ? "Sign in required"
              : error.code === "forbidden"
                ? "Not available on this account"
                : "Something went wrong"}
          </p>
          <p className="mt-1 text-sm">{error.message}</p>
          {error.code === "network" && (
            <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
              Start the API with <code>make api</code>, then retry.
            </p>
          )}
          {error.code === "forbidden" && (
            <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
              These are platform-wide settings. Ask whoever operates this installation if
              you need them changed.
            </p>
          )}
          {error.code === "not_authenticated" && (
            <p className="mt-3 text-sm">
              <a
                href={`/login?next=${encodeURIComponent(returnTo)}`}
                className="font-medium underline decoration-2 underline-offset-4"
                style={{ color: "var(--primary)" }}
              >
                Go to sign in
              </a>
            </p>
          )}
        </div>
        {!terminal && <SoftButton onClick={onRetry}>Retry</SoftButton>}
      </div>
    </SoftCard>
  );
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-4" aria-label="Loading settings" aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          className="soft-sunken h-20 animate-pulse"
          style={{ borderRadius: "var(--r-md)" }}
        />
      ))}
    </div>
  );
}

export function PageHeader({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <header className="mb-10">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Admin · Developer settings
      </p>
      <h1 className="mt-2 text-[28px] font-semibold tracking-tight">{title}</h1>
      <div className="mt-2 max-w-2xl text-sm" style={{ color: "var(--text-muted)" }}>
        {children}
      </div>
    </header>
  );
}

/**
 * A short-lived "saved" acknowledgement.
 *
 * `role="status"` so a screen reader hears it: on these screens the visible confirmation
 * of a slider change is a pill appearing somewhere else on the row, which a sighted user
 * catches in peripheral vision and nobody else does.
 */
export function SavedPill({ show }: { show: boolean }) {
  return (
    <span role="status">
      {show ? <Pill tone="ok">saved</Pill> : null}
    </span>
  );
}
