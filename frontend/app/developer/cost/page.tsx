"use client";

/**
 * Model spend: by model, by node, by day, by prompt version, and against the run cap.
 *
 * Three things this screen refuses to do, each because the alternative is a confident
 * wrong number:
 *
 * **It never renders an unrecorded ledger as `$0.00`.** `model_usage` is the cost ledger
 * and nothing writes to it yet, so a business with runs and no usage rows would otherwise
 * show a tidy zero. The server detects that combination (`ledgerWired`) and this screen
 * leads with it instead of the total.
 *
 * **It does not do arithmetic on money.** Every `usd` arrives as a STRING because the
 * backend holds it as `Decimal`, and parsing it into a JS number to compute a percentage
 * would reintroduce exactly the float error the backend avoids. Bar widths are derived
 * from CALL COUNTS, which are integers, and the money is only ever displayed.
 *
 * **It says whose spend it is.** The ledger is under row-level security and this reads it
 * as the signed-in account's own business, so it is one tenant's spend and never a
 * platform total. A cross-business view would need a `SECURITY DEFINER` function written
 * for the purpose, not a looser session.
 */

import { useCallback, useState } from "react";
import { Pill, SoftCard, SoftSelect, SoftWell } from "../../components/soft";
import { adminApi, type SpendRow } from "../../lib/admin-api";
import { ErrorCard, Loading, PageHeader, useAdminResource } from "../shell";

const WINDOWS = [
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "365", label: "Last year" },
];

export default function CostPage() {
  const [window, setWindow] = useState("30");
  const cost = useAdminResource(
    useCallback(() => adminApi.cost(Number(window)), [window]),
  );

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <PageHeader title="Cost">
        Model spend for your own business, summed from the <code>model_usage</code> ledger.
        The ledger is under row-level security and is read as your business, so these are
        one tenant&apos;s numbers — not a platform total.
      </PageHeader>

      <div className="mb-6 flex items-center gap-3">
        <SoftSelect
          value={window}
          onChange={setWindow}
          label="Reporting window"
          options={WINDOWS}
        />
        {cost.data && (
          <p className="text-xs" style={{ color: "var(--text-faint)" }}>
            since {new Date(cost.data.report.since).toLocaleDateString("en-GB")}
          </p>
        )}
      </div>

      {cost.error && (
        <ErrorCard
          error={cost.error}
          returnTo="/developer/cost"
          onRetry={() => void cost.reload()}
        />
      )}

      {!cost.data && !cost.error && <Loading rows={2} />}

      {cost.data && (
        <div className="space-y-8">
          {!cost.data.report.ledgerWired ? (
            // Leads, and REPLACES the totals rather than sitting above them: showing
            // "$0.00000000" next to "these numbers are unavailable" invites reading the
            // first and ignoring the second.
            <SoftCard className="p-6" size="md">
              <div className="flex flex-wrap items-start gap-3">
                <Pill tone="warn">not recorded</Pill>
                <p className="max-w-2xl text-sm leading-relaxed">{cost.data.report.message}</p>
              </div>
              <p className="mt-4 text-xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
                The breakdowns below are the queries that will answer this once usage is
                being written. They are empty, not zero.
              </p>
            </SoftCard>
          ) : (
            <SoftCard className="p-6" size="md">
              <div className="grid gap-6 sm:grid-cols-3">
                <Figure label="Spend" value={`$${cost.data.report.totalUsd}`} />
                <Figure
                  label="Model calls"
                  value={cost.data.report.calls.toLocaleString("en-GB")}
                />
                <Figure
                  label="Tokens in / out"
                  value={`${cost.data.report.tokensIn.toLocaleString("en-GB")} / ${cost.data.report.tokensOut.toLocaleString("en-GB")}`}
                />
              </div>
              <p className="mt-5 text-xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
                {cost.data.report.message}
              </p>
            </SoftCard>
          )}

          <SoftCard className="p-6" size="md">
            <h2 className="text-lg font-semibold">Against the run cap</h2>
            <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
              Each run carries its own ceiling and stops when it reaches it, returning what
              it has. The cap compared against here is the one stored on the run, not a
              constant — a run is held to the ceiling it was created with.
            </p>
            <div className="mt-5 grid gap-6 sm:grid-cols-3">
              <Figure label="Runs in window" value={String(cost.data.report.runsInWindow)} />
              <Figure label="Runs at their cap" value={String(cost.data.report.runsAtCap)} />
              <Figure label="Current cap" value={`$${cost.data.report.defaultRunCapUsd}`} />
            </div>

            {cost.data.report.topRuns.length > 0 && (
              <ul className="mt-5 space-y-2">
                {cost.data.report.topRuns.map((run) => (
                  <li key={run.runId} className="flex flex-wrap items-center gap-3 text-xs">
                    <code className="truncate" style={{ color: "var(--text-muted)" }}>
                      {run.runId.slice(0, 8)}
                    </code>
                    <span className="tabular font-medium">${run.usd}</span>
                    <span style={{ color: "var(--text-faint)" }}>of ${run.capUsd}</span>
                    {run.atCap && <Pill tone="warn">hit the cap</Pill>}
                  </li>
                ))}
              </ul>
            )}
          </SoftCard>

          <Breakdown
            title="By model"
            note="The one breakdown that can be understated: a model with no price-table entry contributes $0.00 whatever it actually cost."
            rows={cost.data.report.byModel}
          />
          <Breakdown
            title="By node"
            note="Which part of the graph the money went to. GENERATE is expected to dominate."
            rows={cost.data.report.byNode}
          />
          <Breakdown
            title="By prompt version"
            note="Why the version is recorded on every call: it makes a quality change attributable to a prompt or to a model rather than to folklore."
            rows={cost.data.report.byPromptVersion}
          />

          {cost.data.report.byDay.length > 0 && (
            <SoftCard className="p-6" size="md">
              <h2 className="text-lg font-semibold">By day</h2>
              <ul className="mt-4 space-y-1.5">
                {cost.data.report.byDay.map((day) => (
                  <li key={day.day} className="flex items-center gap-3 text-xs">
                    <span className="tabular w-24 shrink-0" style={{ color: "var(--text-muted)" }}>
                      {day.day}
                    </span>
                    <span className="tabular w-24 shrink-0 font-medium">${day.usd}</span>
                    <span className="tabular" style={{ color: "var(--text-faint)" }}>
                      {day.calls} call{day.calls === 1 ? "" : "s"}
                    </span>
                  </li>
                ))}
              </ul>
            </SoftCard>
          )}
        </div>
      )}
    </main>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p
        className="text-[11px] font-semibold uppercase tracking-wider"
        style={{ color: "var(--text-muted)" }}
      >
        {label}
      </p>
      <p className="tabular mt-1 text-xl font-semibold">{value}</p>
    </div>
  );
}

function Breakdown({
  title,
  note,
  rows,
}: {
  title: string;
  note: string;
  rows: SpendRow[];
}) {
  // Widths come from CALL COUNTS, never from the money strings: parsing a Decimal into a
  // JS number to size a bar is the float bug this codebase avoids everywhere else, and a
  // bar is not worth it.
  const busiest = Math.max(1, ...rows.map((r) => r.calls));

  return (
    <SoftCard className="p-6" size="md">
      <h2 className="text-lg font-semibold">{title}</h2>
      <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
        {note}
      </p>

      {rows.length === 0 ? (
        <SoftWell className="mt-4 p-4">
          <p className="text-xs" style={{ color: "var(--text-muted)" }}>
            No rows in this window.
          </p>
        </SoftWell>
      ) : (
        <ul className="mt-4 space-y-3">
          {rows.map((row) => (
            <li key={row.key}>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="flex items-center gap-2">
                  <code className="text-sm font-medium">{row.key}</code>
                  {row.priced === false && <Pill tone="warn">cost not metered</Pill>}
                </div>
                <span className="tabular text-sm font-semibold">${row.usd}</span>
              </div>
              <div className="mt-1.5 flex items-center gap-3">
                <div
                  className="soft-sunken h-2 flex-1 overflow-hidden"
                  style={{ borderRadius: "var(--r-pill)" }}
                >
                  <div
                    className="h-full"
                    style={{
                      width: `${(row.calls / busiest) * 100}%`,
                      background: "var(--primary)",
                      borderRadius: "var(--r-pill)",
                    }}
                  />
                </div>
                <span className="tabular shrink-0 text-[11px]" style={{ color: "var(--text-faint)" }}>
                  {row.calls} call{row.calls === 1 ? "" : "s"} ·{" "}
                  {(row.tokensIn + row.tokensOut).toLocaleString("en-GB")} tokens
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </SoftCard>
  );
}
