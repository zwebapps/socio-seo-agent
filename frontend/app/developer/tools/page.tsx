"use client";

/**
 * Per-node tool access. Switch OFF only.
 *
 * The important thing about this screen is what it does NOT have: a way to grant a tool.
 * The backend allowlist (`agents/tools.NODE_TOOLS`) is a prompt-injection barrier — it is
 * what stops a crawled competitor page talking the model into reaching a publish
 * actuator, and there is a test asserting a fully compliant malicious router still
 * cannot publish. A grant control here would move that barrier behind a session cookie.
 *
 * So the shape is: `granted` is rendered as read-only fact, each granted tool has a
 * switch that can only remove it, and the effective set is shown as the arithmetic it is.
 * The API refuses a `granted` field outright, so this is enforced on both sides rather
 * than being a UI convention.
 *
 * **The screen states that revocations are not yet read by the running graph.** They are
 * stored and the effect is computed, but the graph still builds each node's toolbox
 * straight from the code allowlist. Rendering a kill switch as though it were armed would
 * be worse than not offering one, so the banner says so and the actuator rows repeat it.
 *
 * Local state deliberately holds a DRAFT of the revocation set, applied on Save rather
 * than per switch. Toggling `publish` off should not fire a request on the way past while
 * somebody is reading the row.
 */

import { useCallback, useState } from "react";
import { Pill, SoftButton, SoftCard, SoftToggle, SoftWell } from "../../components/soft";
import { adminApi, type NodeTools } from "../../lib/admin-api";
import { ErrorCard, Loading, PageHeader, SavedPill, useAdminResource } from "../shell";

const NODE_HELP: Record<string, string> = {
  INTAKE: "Reads business memory. No model call and no harvested text.",
  HARVEST: "The widest grant, and the node that handles attacker-controllable text.",
  OPPORTUNITY: "Ranks what is worth doing.",
  PLAN: "Chooses the shape of a page.",
  GENERATE: "Writes the page. Revoke web search to confine it to the business's own material.",
  CONVERT: "Landing page and the per-channel ask that points at it.",
  VALIDATE: "Deterministic verdicts only. No model.",
  REPACK: "Renders one message per channel.",
  REVIEW: "The human interrupt. Holds nothing, so a paused run cannot act.",
  EXPORT: "The only node that reaches the outside world.",
  MEASURE: "Specified, not yet built.",
};

export default function ToolsPage() {
  const tools = useAdminResource(useCallback(() => adminApi.tools(), []));

  return (
    <main className="mx-auto w-full max-w-[1800px] px-6 lg:px-10 xl:px-14 py-12">
      <PageHeader title="Tool access">
        What each node in the graph is allowed to call. Tools can be switched off here and
        cannot be switched on — see below for why.
      </PageHeader>

      {tools.error && (
        <ErrorCard
          error={tools.error}
          returnTo="/developer/tools"
          onRetry={() => void tools.reload()}
        />
      )}

      {!tools.data && !tools.error && <Loading rows={4} />}

      {tools.data && (
        <div className="space-y-4">
          <SoftCard className="p-5" size="md">
            <p className="text-sm font-semibold">Why there is no way to grant a tool here</p>
            <p className="mt-1.5 text-sm leading-relaxed" style={{ color: "var(--text-muted)" }}>
              {tools.data.policy}
            </p>
            {!tools.data.enforced && (
              <div className="mt-4 flex flex-wrap items-start gap-3">
                <Pill tone="warn">not yet enforced</Pill>
                <p className="max-w-xl text-xs leading-relaxed" style={{ color: "var(--warn)" }}>
                  Revocations are saved and the effect is shown, but the running graph still
                  builds each node&apos;s toolbox from the code allowlist, so switching a
                  tool off here does not stop it being used yet. Do not treat this as a kill
                  switch until this notice is gone.
                </p>
              </div>
            )}
          </SoftCard>

          {tools.data.nodes.map((node) => (
            <NodeRow
              key={node.node}
              node={node}
              busy={tools.busy === node.node}
              saved={tools.saved === node.node}
              onSave={(revoked) =>
                void tools.save(node.node, () => adminApi.revokeTools(node.node, revoked))
              }
            />
          ))}
        </div>
      )}
    </main>
  );
}

function NodeRow({
  node,
  busy,
  saved,
  onSave,
}: {
  node: NodeTools;
  busy: boolean;
  saved: boolean;
  onSave: (revoked: string[]) => void;
}) {
  const [draft, setDraft] = useState<string[]>(node.revoked);
  const dirty =
    draft.length !== node.revoked.length || draft.some((t) => !node.revoked.includes(t));

  const effective = node.granted.filter((tool) => !draft.includes(tool));

  function toggle(tool: string, allowed: boolean) {
    setDraft((prev) => (allowed ? prev.filter((t) => t !== tool) : [...prev, tool]));
  }

  if (node.granted.length === 0) {
    return (
      <SoftCard className="p-5" size="md">
        <div className="flex flex-wrap items-center gap-2.5">
          <h3 className="text-sm font-semibold uppercase tracking-wide">{node.node}</h3>
          <Pill tone="muted">holds nothing</Pill>
        </div>
        <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
          {NODE_HELP[node.node] ?? ""} Nothing to switch off.
        </p>
      </SoftCard>
    );
  }

  return (
    <SoftCard className="p-5" size="md">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h3 className="text-sm font-semibold uppercase tracking-wide">{node.node}</h3>
            {node.actuators.length > 0 && <Pill tone="warn">reaches the outside world</Pill>}
            {draft.length > 0 && <Pill tone="accent">{draft.length} off</Pill>}
            <SavedPill show={saved} />
          </div>
          <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
            {NODE_HELP[node.node] ?? ""}
          </p>
        </div>
      </div>

      <ul className="mt-4 space-y-2">
        {node.granted.map((tool) => {
          const allowed = !draft.includes(tool);
          const actuator = node.actuators.includes(tool);
          return (
            <li
              key={tool}
              className="flex flex-wrap items-center justify-between gap-3 py-1"
            >
              <div className="min-w-0">
                <code className="text-sm font-medium" style={{ color: "var(--text)" }}>
                  {tool}
                </code>
                {actuator && (
                  <span className="ml-2 text-[11px]" style={{ color: "var(--warn)" }}>
                    side effect outside this system
                  </span>
                )}
              </div>
              <SoftToggle
                checked={allowed}
                onChange={(next) => toggle(tool, next)}
                // Every switch on this page would otherwise be named "on"/"off". The
                // accessible name has to say WHICH tool on WHICH node, or a screen-reader
                // user hears eleven identical controls.
                label={`${tool} for ${node.node}`}
                disabled={busy}
              />
            </li>
          );
        })}
      </ul>

      <SoftWell className="mt-4 p-3">
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          <span style={{ color: "var(--text-faint)" }}>Effective:</span>{" "}
          {effective.length > 0 ? (
            <span className="font-medium" style={{ color: "var(--primary)" }}>
              {effective.join(", ")}
            </span>
          ) : (
            <span className="font-medium" style={{ color: "var(--warn)" }}>
              nothing — this node will be unable to do its job
            </span>
          )}
        </p>
        {node.ignored.length > 0 && (
          <p className="mt-2 text-xs" style={{ color: "var(--warn)" }}>
            Stored but inert: {node.ignored.join(", ")} — this node does not hold{" "}
            {node.ignored.length === 1 ? "it" : "them"}, most likely left behind by a
            rename. Save to clear.
          </p>
        )}
      </SoftWell>

      {dirty && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <SoftButton variant="quiet" onClick={() => setDraft(node.revoked)} disabled={busy}>
            Discard
          </SoftButton>
          <div className="flex-1" />
          <SoftButton variant="primary" onClick={() => onSave(draft)} disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </SoftButton>
        </div>
      )}
    </SoftCard>
  );
}
