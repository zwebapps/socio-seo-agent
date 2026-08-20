"use client";

/**
 * Sampling (temperature, output ceiling) and the prompt-version inventory.
 *
 * Two features on one screen because they answer the same question — "what exactly is
 * this model being asked to do" — and because the second one is small and honest enough
 * that giving it a page of its own would overstate it.
 *
 * Three decisions worth stating:
 *
 * **A number is never shown on its own.** The output ceiling is displayed with what it
 * COSTS: the pre-call budget guard reserves the full allowance on every call, so 8192
 * tokens on the strong tier reserves ~$0.20 against a $0.50 run cap — two calls and the
 * run refuses itself. The server computes that figure (it owns the price table) and the
 * slider's description says it. Likewise the floor is shown as "about a 2,500-character
 * German article", because 1024 means nothing and the article does.
 *
 * **A control that cannot work is disabled and says why.** Several Anthropic models
 * reject `temperature` outright, and the strong tier's first choice is one of them. A
 * slider offered anyway would be tuned, saved, and silently skipped. So when every model
 * in a task's chain refuses the parameter, the temperature slider is disabled with the
 * model named; when only some refuse, it stays live with a warning.
 *
 * **The prompt section is an inventory, not a dropdown.** There is exactly one version of
 * each runtime prompt and no way to switch, so a `<select>` with one option would be a
 * control pretending to be a choice. It reports what is in force, where it is defined,
 * and what changing it would actually involve. See `services/prompt_inventory.py`.
 */

import { useCallback, useState } from "react";
import { Pill, SoftButton, SoftCard, SoftRange, SoftWell } from "../../components/soft";
import { adminApi, type Sampling, type SamplingBounds } from "../../lib/admin-api";
import { ErrorCard, Loading, PageHeader, SavedPill, useAdminResource } from "../shell";

const TASK_HELP: Record<string, string> = {
  classify: "Cheap, high volume. A low temperature keeps labels consistent.",
  extract: "Structured pulls from a page. Wants determinism, not flair.",
  repack: "One message rendered per channel.",
  plan: "Chooses the shape of a page.",
  prioritise: "Ranks opportunities against effort.",
  generate: "Writes the page, and needs the largest output ceiling of any task here.",
  review: "Checks a draft against stated constraints.",
  embed: "Vectors for the knowledge base. Neither control applies to an embedding call.",
};

/** Rough reading of what an output ceiling buys, in the unit an operator thinks in. */
function articleLength(tokens: number): string {
  // The inverse of the server's own arithmetic, minus the envelope and markup terms:
  // ~4 characters per token of German prose. Rounded hard, because a precise-looking
  // figure here would imply a precision the estimate does not have.
  const chars = Math.round(((tokens - 80) * 4) / 100) * 100;
  return `${chars.toLocaleString("en-GB")} characters of German prose`;
}

export default function RuntimePage() {
  const sampling = useAdminResource(useCallback(() => adminApi.sampling(), []));
  const prompts = useAdminResource(useCallback(() => adminApi.promptVersions(), []));

  const error = sampling.error ?? prompts.error;

  return (
    <main className="mx-auto w-full max-w-[1800px] px-6 lg:px-10 xl:px-14 py-12">
      <PageHeader title="Sampling and prompts">
        How much freedom each task class gets, and how long its answer may be. Changes take
        effect on the next run — no redeploy. A task with nothing set sends no sampling
        parameters at all and takes the provider default, which is what every call does
        today.
      </PageHeader>

      {error && (
        <ErrorCard
          error={error}
          returnTo="/developer/runtime"
          onRetry={() => {
            void sampling.reload();
            void prompts.reload();
          }}
        />
      )}

      {!sampling.data && !error && <Loading />}

      {sampling.data && (
        <section className="space-y-4">
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="text-lg font-semibold">Sampling per task class</h2>
            <p className="text-xs" style={{ color: "var(--text-muted)" }}>
              {sampling.data.sampling.filter((s) => s.source === "configured").length} of{" "}
              {sampling.data.sampling.length} configured
            </p>
          </div>

          <SoftWell className="p-4">
            <div className="text-xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
              <p>
                <strong style={{ color: "var(--text)" }}>Why these limits.</strong>{" "}
                {sampling.data.bounds.temperatureReason}
              </p>
              <p className="mt-2">{sampling.data.bounds.maxTokensReason}</p>
            </div>
          </SoftWell>

          {sampling.data.sampling.map((row) => (
            <SamplingRow
              key={row.taskClass}
              row={row}
              bounds={sampling.data!.bounds}
              runCapUsd={sampling.data!.runCapUsd}
              busy={sampling.busy === row.taskClass}
              saved={sampling.saved === row.taskClass}
              onSave={(temperature, maxOutputTokens) =>
                void sampling.save(row.taskClass, () =>
                  adminApi.saveSampling(row.taskClass, { temperature, maxOutputTokens }),
                )
              }
              onRevert={() =>
                void sampling.save(row.taskClass, () => adminApi.revertSampling(row.taskClass))
              }
            />
          ))}
        </section>
      )}

      {prompts.data && (
        <section className="mt-12">
          <h2 className="mb-4 text-lg font-semibold">Prompt versions</h2>
          <SoftCard className="p-5" size="md">
            <p className="text-sm leading-relaxed">{prompts.data.summary}</p>

            <div className="mt-5 space-y-3">
              {prompts.data.surfaces.map((surface) => (
                <SoftWell key={surface.key} className="p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-sm font-semibold">{surface.label}</p>
                    <div className="flex items-center gap-2">
                      {surface.version ? (
                        <code
                          className="soft-flat px-2.5 py-1 text-xs"
                          style={{ borderRadius: "var(--r-pill)", color: "var(--primary)" }}
                        >
                          {surface.version}
                        </code>
                      ) : (
                        <Pill tone="err">unreadable</Pill>
                      )}
                      <Pill tone="muted">
                        {surface.variants === 1 ? "no alternative" : `${surface.variants} versions`}
                      </Pill>
                    </div>
                  </div>
                  <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
                    Defined in{" "}
                    <code>
                      {surface.module}.{surface.attribute}
                    </code>
                    . {surface.error ?? surface.howToChange}
                  </p>
                </SoftWell>
              ))}
            </div>

            <p className="mt-5 text-xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
              {prompts.data.evalHarnessNote}
            </p>
          </SoftCard>
        </section>
      )}
    </main>
  );
}

function SamplingRow({
  row,
  bounds,
  runCapUsd,
  busy,
  saved,
  onSave,
  onRevert,
}: {
  row: Sampling;
  bounds: SamplingBounds;
  runCapUsd: string;
  busy: boolean;
  saved: boolean;
  onSave: (temperature: number | null, maxOutputTokens: number | null) => void;
  onRevert: () => void;
}) {
  // Local slider state starts from the effective value, so an unconfigured task's slider
  // sits where the provider default actually is rather than at the range's minimum —
  // which would read as "temperature 0" and be wrong.
  const [temperature, setTemperature] = useState(row.temperature ?? 0.3);
  const [maxTokens, setMaxTokens] = useState(row.maxOutputTokens ?? 2048);
  const [open, setOpen] = useState(false);

  const embed = row.taskClass === "embed";
  const ceilingId = `ceiling-note-${row.taskClass}`;
  const tempId = `temp-note-${row.taskClass}`;

  return (
    <SoftCard className="p-5" size="md">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h3 className="text-sm font-semibold uppercase tracking-wide">{row.taskClass}</h3>
            <Pill tone={row.source === "configured" ? "accent" : "muted"}>{row.source}</Pill>
            {row.temperatureInert && <Pill tone="warn">temperature not accepted</Pill>}
            <SavedPill show={saved} />
          </div>
          <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
            {TASK_HELP[row.taskClass] ?? ""}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span
            className="soft-flat px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider"
            style={{ borderRadius: "var(--r-pill)", color: "var(--primary)" }}
          >
            {row.tier}
          </span>
          <SoftButton variant="quiet" onClick={() => setOpen((v) => !v)}>
            {open ? "Close" : "Change"}
          </SoftButton>
        </div>
      </div>

      <SoftWell className="mt-4 p-3">
        <dl className="flex flex-wrap gap-x-8 gap-y-1 text-xs">
          <div className="flex gap-2">
            <dt style={{ color: "var(--text-faint)" }}>Temperature</dt>
            <dd className="tabular font-medium">
              {row.temperature ?? "provider default"}
            </dd>
          </div>
          <div className="flex gap-2">
            <dt style={{ color: "var(--text-faint)" }}>Output ceiling</dt>
            <dd className="tabular font-medium">
              {row.maxOutputTokens ? `${row.maxOutputTokens} tokens` : "provider default"}
            </dd>
          </div>
          {row.reservedUsdPerCall && (
            <div className="flex gap-2">
              <dt style={{ color: "var(--text-faint)" }}>Guard reserves</dt>
              <dd className="tabular font-medium">${row.reservedUsdPerCall} per call</dd>
            </div>
          )}
        </dl>
      </SoftWell>

      {open && (
        <div className="mt-5 space-y-6">
          {embed ? (
            <p className="text-xs leading-relaxed" style={{ color: "var(--warn)" }}>
              Neither control does anything on an embedding call: there is no sampling and
              no generated output to cap. Left editable rather than hidden only so the
              screen does not silently omit a task class.
            </p>
          ) : null}

          <div>
            <SoftRange
              controlId={`temperature-${row.taskClass}`}
              label="Temperature"
              value={temperature}
              onChange={setTemperature}
              min={bounds.temperatureMin}
              max={bounds.temperatureMax}
              step={bounds.temperatureStep}
              valueText={`${temperature.toFixed(2)}`}
              describedBy={tempId}
              disabled={busy || row.temperatureInert}
            />
            <p id={tempId} className="mt-1.5 text-xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
              {row.temperatureInert ? (
                <>
                  Disabled: every model in this task&apos;s chain rejects{" "}
                  <code>temperature</code> outright (
                  {row.modelsRejectingTemperature.join(", ")}). Steer these by prompt
                  instead.
                </>
              ) : row.modelsRejectingTemperature.length > 0 ? (
                <>
                  Applies to some of this chain only —{" "}
                  {row.modelsRejectingTemperature.join(", ")}{" "}
                  {row.modelsRejectingTemperature.length === 1 ? "rejects" : "reject"}{" "}
                  <code>temperature</code> outright, and the value is skipped for{" "}
                  {row.modelsRejectingTemperature.length === 1 ? "it" : "them"} rather than
                  failing the call. 0 is repeatable; higher is more varied.
                </>
              ) : (
                <>
                  0 is as repeatable as the model gets; higher is more varied. Above about
                  1.2 marketing copy starts inventing specifics, which this pipeline turns
                  into claim-gate refusals rather than flair — so the slider stops at{" "}
                  {bounds.temperatureMax}.
                </>
              )}
            </p>
          </div>

          <div>
            <SoftRange
              controlId={`ceiling-${row.taskClass}`}
              label="Output ceiling"
              value={maxTokens}
              onChange={setMaxTokens}
              min={bounds.maxTokensMin}
              max={bounds.maxTokensMax}
              step={bounds.maxTokensStep}
              valueText={`${maxTokens.toLocaleString("en-GB")} tokens`}
              describedBy={ceilingId}
              disabled={busy}
            />
            <p
              id={ceilingId}
              className="mt-1.5 text-xs leading-relaxed"
              style={{ color: "var(--text-muted)" }}
            >
              Room for roughly {articleLength(maxTokens)}. The budget guard reserves this
              whole allowance before every call, so on a ${runCapUsd} run cap a larger
              ceiling means fewer calls per run, not longer articles. Below{" "}
              {bounds.maxTokensMin} a blog draft is truncated mid-JSON and the node gets
              nothing at all, which is why the slider does not go lower.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {row.source === "configured" && (
              <SoftButton variant="quiet" onClick={onRevert} disabled={busy}>
                Revert to provider default
              </SoftButton>
            )}
            <div className="flex-1" />
            <SoftButton
              variant="primary"
              disabled={busy}
              onClick={() => onSave(row.temperatureInert ? null : temperature, maxTokens)}
            >
              {busy ? "Saving…" : "Save"}
            </SoftButton>
          </div>
        </div>
      )}
    </SoftCard>
  );
}
