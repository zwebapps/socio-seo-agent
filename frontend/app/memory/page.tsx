"use client";

/**
 * "What I remember about your business" — the editable memory panel.
 *
 * This is the screen that has to make one specific claim honest. The product does not say
 * "the agent learns"; it says the agent updates persistent business preferences from
 * explicit feedback, and obeys them next time without being told again. A list of rules on
 * a page does not demonstrate that. What demonstrates it is showing the EXACT lines the
 * next run's system prompt receives, straight from the same function the graph calls
 * (`to_prompt_lines`), so the panel is evidence rather than an assertion. That block is
 * the first thing on the screen for that reason.
 *
 * Design decisions worth naming:
 *
 * - **Every write repaints from the server's response**, never from an optimistic local
 *   edit. So a de-duplicated add is visibly a no-op — the list comes back unchanged —
 *   rather than a phantom success that leaves the owner believing a rule is in force.
 * - **Editing is in place**, and the backend keeps the rule's position. The order is the
 *   order the owner confirmed things in and it reaches the model verbatim, so a typo fix
 *   must not quietly reorder the instructions.
 * - **Delete asks first, inline.** Not `window.confirm`, which cannot be styled and reads
 *   as a browser error; and not a bare destructive button either, because these rules are
 *   the owner's own brand voice and a stray click should not cost one.
 * - **Refusals are shown verbatim.** The API's messages explain the limit and say what to
 *   do instead ("delete one of them instead"), which is more use than "error 409".
 * - **Tone, audience and banned claims are shown read-only**, because this API does not
 *   own them — and the panel says so rather than offering an edit that would be refused.
 *   Which fields are writable comes from the response (`editableFields`), so the markers
 *   cannot drift from what the server accepts.
 */

import { useCallback, useEffect, useState } from "react";
import { Shell } from "@/app/components/page-shell";
import { Pill, SoftButton, SoftCard, SoftInput, SoftWell } from "../components/soft";
import { ApiError } from "../lib/api";
import {
  addPreference,
  approveProposal,
  deletePreference,
  fetchMemory,
  fetchProposals,
  updatePreference,
  type BusinessMemory,
  type Proposal,
} from "../lib/memory-api";

export default function MemoryPage() {
  const [memory, setMemory] = useState<BusinessMemory | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await fetchMemory();
      setMemory(next);
      setLoadError(null);
      try {
        setProposals(await fetchProposals(next.businessId));
      } catch {
        // Proposals are an enhancement on this screen. Failing to read them must not
        // cost the owner the memory panel itself, which is what they came for.
        setProposals([]);
      }
    } catch (exc) {
      setLoadError(
        exc instanceof ApiError
          ? exc.message
          : "Could not load what the agent remembers about your business.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * One place that runs a write, replaces the memory from the response, and turns a
   * refusal into a message. Every mutation goes through it so none of them can forget the
   * repaint or swallow the reason.
   */
  const mutate = useCallback(
    async (action: () => Promise<BusinessMemory>, success: string): Promise<boolean> => {
      setBusy(true);
      setNotice(null);
      try {
        setMemory(await action());
        setNotice(success);
        return true;
      } catch (exc) {
        setNotice(
          exc instanceof ApiError ? exc.message : "That change could not be saved.",
        );
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  return (
    <Shell className="py-12">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Business memory
      </p>
      <h1 className="mt-2 text-[26px] font-semibold tracking-tight">
        What I remember about your business
      </h1>
      <p className="mt-3 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
        These are carried into every run — read once when a run starts, then included in
        the instructions for every step of it. Change them here and the next run obeys the
        change without being told again.
      </p>

      {loadError && (
        <SoftCard className="mt-7 p-5" size="md">
          <p className="text-sm font-semibold" style={{ color: "var(--err)" }} role="alert">
            {loadError}
          </p>
          <p className="mt-3">
            <SoftButton onClick={() => void load()}>Try again</SoftButton>
          </p>
        </SoftCard>
      )}

      {/* A single live region for every outcome, so a screen reader hears the result of a
          save, a refusal or a delete without the focus having to move. */}
      <p
        aria-live="polite"
        className="mt-4 min-h-5 text-sm font-medium"
        style={{ color: "var(--accent)" }}
      >
        {notice}
      </p>

      {memory && (
        <>
          <PromptPreview lines={memory.promptLines} count={memory.rememberedCount} />

          <Preferences
            memory={memory}
            busy={busy}
            onAdd={(rule) => mutate(() => addPreference(rule), "Saved. The next run will follow it.")}
            onEdit={(id, rule) =>
              mutate(() => updatePreference(id, rule), "Reworded, and kept in the same place.")
            }
            onDelete={(id) =>
              mutate(() => deletePreference(id), "Removed. Future runs will not see it.")
            }
          />

          <Proposals
            proposals={proposals}
            busy={busy}
            onApprove={async (id) => {
              setBusy(true);
              setNotice(null);
              try {
                const { rule } = await approveProposal(id);
                setNotice(`Approved: “${rule}” is now in force.`);
                await load();
              } catch (exc) {
                setNotice(
                  exc instanceof ApiError ? exc.message : "That rule could not be approved.",
                );
              } finally {
                setBusy(false);
              }
            }}
          />

          <FromOnboarding memory={memory} />
        </>
      )}
    </Shell>
  );
}

/* ------------------------------------------------------------------------- */

/** The claim's evidence: literally what the model is told. */
function PromptPreview({ lines, count }: { lines: string[]; count: number }) {
  return (
    <SoftCard className="mt-7 p-6" size="lg">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">What the next run is told</h2>
        <Pill tone={count > 0 ? "accent" : "muted"}>
          {count === 1 ? "1 remembered preference" : `${count} remembered preferences`}
        </Pill>
      </div>
      <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
        Copied word for word into the instructions of every step, by the same code the
        agent runs. Nothing is paraphrased on the way.
      </p>

      <SoftWell className="mt-4 p-4">
        {lines.length === 0 ? (
          <p className="text-sm" style={{ color: "var(--text-muted)" }}>
            Nothing yet — which is an honest state, not a problem. A business that has
            confirmed no preferences contributes no extra instructions, and the agent works
            from its profile alone.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {lines.map((line) => (
              <li key={line} className="flex gap-2 text-sm leading-relaxed">
                <span aria-hidden style={{ color: "var(--text-faint)" }}>
                  –
                </span>
                <span>{line}</span>
              </li>
            ))}
          </ul>
        )}
      </SoftWell>
    </SoftCard>
  );
}

/* ------------------------------------------------------------------------- */

function Preferences({
  memory,
  busy,
  onAdd,
  onEdit,
  onDelete,
}: {
  memory: BusinessMemory;
  busy: boolean;
  onAdd: (rule: string) => Promise<boolean>;
  onEdit: (id: string, rule: string) => Promise<boolean>;
  onDelete: (id: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState("");
  const full = memory.preferences.length >= memory.maxPreferences;

  const submit = async () => {
    const rule = draft.trim();
    if (!rule) return;
    if (await onAdd(rule)) setDraft("");
  };

  return (
    <SoftCard className="mt-6 p-6" size="lg" as="section">
      <h2 className="text-sm font-semibold">Preferences you have confirmed</h2>
      <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
        Kept in the order you confirmed them, because that is the order the agent reads
        them in. Editing one leaves it where it is.
      </p>

      {memory.preferences.length === 0 ? (
        <SoftWell className="mt-4 p-4">
          <p className="text-sm" style={{ color: "var(--text-muted)" }}>
            You have not confirmed any preferences yet. Add one below, or approve a rule the
            agent has noticed from your feedback.
          </p>
        </SoftWell>
      ) : (
        <ul className="mt-4 space-y-2.5">
          {memory.preferences.map((preference) => (
            <PreferenceRow
              key={preference.id}
              id={preference.id}
              rule={preference.rule}
              busy={busy}
              maxLength={memory.maxRuleLength}
              onEdit={onEdit}
              onDelete={onDelete}
            />
          ))}
        </ul>
      )}

      <form
        className="mt-6"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label htmlFor="new-preference" className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
          Add a preference
        </label>
        <div className="mt-2 flex flex-wrap gap-2">
          <div className="min-w-56 flex-1">
            <SoftInput
              label="A preference the agent should follow from now on"
              value={draft}
              onChange={setDraft}
              placeholder="Never use exclamation marks"
              describedBy="new-preference-help"
              className="w-full"
            />
          </div>
          <SoftButton type="submit" variant="primary" disabled={busy || full || !draft.trim()}>
            Remember this
          </SoftButton>
        </div>
        <p id="new-preference-help" className="mt-2 max-w-[70ch] text-xs" style={{ color: "var(--text-faint)" }}>
          {full ? (
            <span style={{ color: "var(--warn)" }}>
              You have the maximum of {memory.maxPreferences}. Remove one before adding
              another — past roughly this many, a model starts trading instructions off
              against each other instead of following them all.
            </span>
          ) : (
            <>
              One instruction, up to {memory.maxRuleLength} characters
              {draft.length > 0 && (
                <>
                  {" "}
                  <span
                    className="tabular"
                    style={{
                      color: draft.trim().length > memory.maxRuleLength ? "var(--warn)" : undefined,
                    }}
                  >
                    ({draft.trim().length} so far)
                  </span>
                </>
              )}
              . Longer guidance belongs in an uploaded document, where the agent can pull
              out the part that matters instead of carrying all of it every time.
            </>
          )}
        </p>
      </form>
    </SoftCard>
  );
}

function PreferenceRow({
  id,
  rule,
  busy,
  maxLength,
  onEdit,
  onDelete,
}: {
  id: string;
  rule: string;
  busy: boolean;
  maxLength: number;
  onEdit: (id: string, rule: string) => Promise<boolean>;
  onDelete: (id: string) => Promise<boolean>;
}) {
  const [mode, setMode] = useState<"view" | "edit" | "confirm-delete">("view");
  const [text, setText] = useState(rule);

  const save = async () => {
    const next = text.trim();
    if (!next) return;
    if (next === rule) {
      setMode("view");
      return;
    }
    if (await onEdit(id, next)) setMode("view");
  };

  if (mode === "edit") {
    return (
      <li>
        <SoftWell className="p-3">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
            // Keydown bubbles from the input, so the form is the right listener. Escape
            // abandons the edit — the help text below promises it, and a promise the
            // keyboard does not keep is worse than no promise.
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              setText(rule);
              setMode("view");
            }}
          >
            <div className="flex flex-wrap items-center gap-2">
              <div className="min-w-56 flex-1">
                <SoftInput
                  label={`Reword the preference: ${rule}`}
                  value={text}
                  onChange={setText}
                  autoFocus
                  describedBy={`counter-${id}`}
                  className="w-full"
                />
              </div>
              <SoftButton type="submit" variant="primary" disabled={busy || !text.trim()}>
                Save
              </SoftButton>
              <SoftButton
                variant="quiet"
                onClick={() => {
                  setText(rule);
                  setMode("view");
                }}
              >
                Cancel
              </SoftButton>
            </div>
            <p id={`counter-${id}`} className="mt-2 max-w-[70ch] text-xs" style={{ color: "var(--text-faint)" }}>
              <span
                className="tabular"
                style={{ color: text.trim().length > maxLength ? "var(--warn)" : undefined }}
              >
                {text.trim().length}
              </span>
              {" / "}
              {maxLength} characters. Press Enter to save, Escape or Cancel to leave it as
              it was.
            </p>
          </form>
        </SoftWell>
      </li>
    );
  }

  if (mode === "confirm-delete") {
    return (
      <li>
        <SoftWell className="p-3">
          <p className="text-sm">
            Stop following <strong>{rule}</strong>?
          </p>
          <p className="mt-1 text-xs" style={{ color: "var(--text-muted)" }}>
            Future runs will not see it. Already-published content does not change.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <SoftButton
              variant="primary"
              disabled={busy}
              onClick={() => void onDelete(id)}
              ariaLabel={`Confirm removing the preference: ${rule}`}
            >
              Remove it
            </SoftButton>
            <SoftButton variant="quiet" onClick={() => setMode("view")}>
              Keep it
            </SoftButton>
          </div>
        </SoftWell>
      </li>
    );
  }

  return (
    <li className="flex flex-wrap items-center gap-3">
      <span className="min-w-48 flex-1 text-sm">{rule}</span>
      <span className="flex gap-2">
        <SoftButton
          onClick={() => {
            setText(rule);
            setMode("edit");
          }}
          disabled={busy}
          ariaLabel={`Reword the preference: ${rule}`}
        >
          Edit
        </SoftButton>
        <SoftButton
          variant="quiet"
          onClick={() => setMode("confirm-delete")}
          disabled={busy}
          ariaLabel={`Remove the preference: ${rule}`}
        >
          Remove
        </SoftButton>
      </span>
    </li>
  );
}

/* ------------------------------------------------------------------------- */

function Proposals({
  proposals,
  busy,
  onApprove,
}: {
  proposals: Proposal[];
  busy: boolean;
  onApprove: (id: string) => Promise<void>;
}) {
  const pending = proposals.filter((p) => p.status === "proposed");
  if (pending.length === 0) return null;

  return (
    <SoftCard className="mt-6 p-6" size="lg" as="section">
      <h2 className="text-sm font-semibold">Noticed from your feedback</h2>
      <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
        A theme came up more than once in what you rejected, so the agent has drafted a
        rule. It is <strong>not</strong> in force: nothing changes how your content is
        written until you approve it here.
      </p>

      <ul className="mt-4 space-y-3">
        {pending.map((proposal) => (
          <li key={proposal.id}>
            <SoftWell className="p-4">
              <p className="text-sm font-medium">{proposal.rule}</p>
              {proposal.derivedFrom.length > 0 && (
                <details className="mt-2">
                  <summary
                    className="cursor-pointer text-xs font-semibold"
                    style={{ color: "var(--text-muted)" }}
                  >
                    Why — {proposal.derivedFrom.length} thing
                    {proposal.derivedFrom.length === 1 ? "" : "s"} you said
                  </summary>
                  <ul className="mt-2 space-y-1 text-xs" style={{ color: "var(--text-muted)" }}>
                    {proposal.derivedFrom.map((reason, index) => (
                      <li key={`${proposal.id}-${index}`}>“{reason}”</li>
                    ))}
                  </ul>
                </details>
              )}
              <div className="mt-3">
                <SoftButton
                  variant="primary"
                  disabled={busy}
                  onClick={() => void onApprove(proposal.id)}
                  ariaLabel={`Approve the rule: ${proposal.rule}`}
                >
                  Approve
                </SoftButton>
              </div>
            </SoftWell>
          </li>
        ))}
      </ul>
      <p className="mt-4 text-xs" style={{ color: "var(--text-faint)" }}>
        There is no way to dismiss a proposal yet, so leaving one unapproved is how you
        decline it for now.
      </p>
    </SoftCard>
  );
}

/* ------------------------------------------------------------------------- */

function FromOnboarding({ memory }: { memory: BusinessMemory }) {
  const editable = new Set(memory.editableFields);
  const hasAny = memory.tone || memory.audience || memory.bannedClaims.length > 0;

  return (
    <SoftCard className="mt-6 p-6" size="lg" as="section">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">From your business profile</h2>
        {!editable.has("profile") && <Pill tone="muted">read-only here</Pill>}
      </div>
      <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
        The agent uses these too, but they belong to your business profile rather than to
        this panel, so they cannot be changed from here yet.
      </p>

      {hasAny ? (
        <dl className="mt-4 space-y-3 text-sm">
          {memory.tone && <Detail label="Tone" value={memory.tone} />}
          {memory.audience && <Detail label="Audience" value={memory.audience} />}
          {memory.bannedClaims.length > 0 && (
            <div>
              <dt className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
                Never claim
              </dt>
              <dd className="mt-1">
                <ul className="space-y-1">
                  {memory.bannedClaims.map((claim) => (
                    <li key={claim}>{claim}</li>
                  ))}
                </ul>
              </dd>
            </div>
          )}
        </dl>
      ) : (
        <SoftWell className="mt-4 p-4">
          <p className="text-sm" style={{ color: "var(--text-muted)" }}>
            Nothing on record. Tone, audience and forbidden claims are drafted during
            onboarding.
          </p>
        </SoftWell>
      )}
    </SoftCard>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
        {label}
      </dt>
      <dd className="mt-0.5">{value}</dd>
    </div>
  );
}
