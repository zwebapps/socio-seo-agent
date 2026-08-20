"use client";

/**
 * Every run this business has asked for, newest first.
 *
 * The gap this fills: `POST /api/v1/runs` handed back an id and nothing in the product could
 * tell you what ids existed, so a run you navigated away from was gone — the timeline at
 * `/runs/{id}` was reachable only by pasting an id out of curl. There was not even a list
 * endpoint to call, so `GET /api/v1/runs` is new alongside this screen.
 *
 * Deliberately thin. The rows, the polling and the state-to-colour mapping all live in
 * `components/run-rows.tsx` and are shared with the dashboard panel, because they are the
 * same list at two lengths and two copies would drift on the one thing that matters here:
 * that a run which stopped short says so, and says why.
 *
 * A client component for the usual reason — the session cookie needs an `Origin` header and
 * a server component's `fetch` sends none.
 */

import Link from "next/link";

import { RunRows, useRuns } from "@/app/components/run-rows";
import { Pill, SoftButton } from "@/app/components/soft";
import { ALL_RUNS } from "@/app/lib/runs-api";

export default function RunsPage() {
  const { state, live, reload, loadMore, canLoadMore, loadingMore } = useRuns(ALL_RUNS);

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Runs
      </p>
      <h1 className="mt-2 text-[26px] font-semibold tracking-tight">Everything you have asked for</h1>
      <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
        Newest first. Open one to see which step it reached, what each step cost, and the
        draft, SEO findings, social posts and AI answer blocks it produced. A run left
        mid-flight by a restart can be picked back up from its checkpoint.
      </p>

      <div className="mt-8 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {live && <Pill tone="accent">live</Pill>}
          {state.kind === "ready" && (
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              {/* The number shown, and whether there is more — never a claim about the
                  total. The list used to be CAPPED here, so older runs were unreachable
                  and the honest wording was "the most recent 50"; it is paginated now, so
                  the wording follows the mechanism rather than the other way round. */}
              showing {state.runs.length}
              {canLoadMore ? " — more below" : ""}
            </span>
          )}
        </div>
        <SoftButton
          onClick={() => void reload()}
          variant="quiet"
          ariaLabel="Refresh the list of runs"
        >
          Refresh
        </SoftButton>
      </div>

      <div className="mt-5" aria-live="polite">
        <RunRows
          state={state}
          emptyNote="No runs yet. Start one from the dashboard and it will appear here."
          onLoadMore={() => void loadMore()}
          canLoadMore={canLoadMore}
          loadingMore={loadingMore}
          onResumed={() => void reload()}
        />
      </div>

      <p className="mt-8">
        <Link
          href="/"
          className="text-sm font-medium underline"
          style={{ color: "var(--primary)" }}
        >
          Back to the dashboard
        </Link>
      </p>
    </main>
  );
}
