"use client";

/**
 * Admin: model routing.
 *
 * Two audiences, and the layout follows that split rather than the data model:
 * PROVIDERS first, because a route can only name a provider that is usable, and
 * ROUTES second, because that is the per-task detail you tune once the plumbing works.
 *
 * The screen tells the truth about three things a settings page usually hides: whether
 * a route is actually configured or just showing a default, whether cost can be
 * reported for the models chosen, and — for a local provider — whether anything is
 * reachable at all.
 *
 * The error card comes from `../shell` rather than living here. It was written here first
 * and copied out, which left two of them, and the copies drifted the moment `/developer/
 * cost` needed a 403 sentence that was true for a screen with no settings on it: this one
 * kept claiming "platform-wide settings" on a page refusing a spend figure. Routing IS a
 * platform-wide setting, so this screen wants the shell's default and passes no override.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Pill,
  SoftButton,
  SoftCard,
  SoftInput,
  SoftSelect,
  SoftTile,
  SoftToggle,
  SoftWell,
} from "../../components/soft";
import { ApiError } from "../../lib/api";
import { adminApi, type Catalogue, type Provider, type Route } from "../../lib/admin-api";
import { ErrorCard } from "../shell";

const TIERS = ["cheap", "mid", "strong", "embed"];

const TASK_HELP: Record<string, string> = {
  classify: "Cheap, high volume. Safe place for a local model.",
  extract: "Structured pulls from a page. Needs reliable tool calling.",
  repack: "One message rendered per channel.",
  plan: "Chooses the shape of a page.",
  prioritise: "Ranks opportunities against effort.",
  generate: "Writes the page. The only task on the strong tier, and ~86% of run cost.",
  review: "Checks a draft against stated constraints.",
  embed: "Vectors for the knowledge base. Changing this invalidates the index.",
};

type Loaded = { routes: Route[]; providers: Provider[] };

export default function ModelsAdminPage() {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [catalogues, setCatalogues] = useState<Record<string, Catalogue>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [routes, providers] = await Promise.all([adminApi.routes(), adminApi.providers()]);
      setData({ routes: routes.routes, providers: providers.providers });

      const entries = await Promise.all(
        providers.providers.map(async (p) => {
          try {
            return [p.provider, await adminApi.catalogue(p.provider)] as const;
          } catch {
            return null;
          }
        }),
      );
      setCatalogues(Object.fromEntries(entries.filter((e): e is NonNullable<typeof e> => !!e)));
    } catch (e) {
      const err = e as ApiError;
      setError({ code: err.code ?? "unknown", message: err.message });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function withBusy(key: string, work: () => Promise<unknown>) {
    setBusy(key);
    setSaved(null);
    try {
      await work();
      await load();
      setSaved(key);
      window.setTimeout(() => setSaved(null), 2500);
    } catch (e) {
      const err = e as ApiError;
      setError({ code: err.code ?? "unknown", message: err.message });
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="mx-auto w-full max-w-[1800px] px-6 lg:px-10 xl:px-14 py-12">
      <header className="mb-10">
        <p
          className="text-[11px] font-semibold uppercase tracking-[0.18em]"
          style={{ color: "var(--accent)" }}
        >
          Admin · Developer settings
        </p>
        <h1 className="mt-2 text-[28px] font-semibold tracking-tight">Model routing</h1>
        <p className="mt-2 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
          Choose which model serves each task, and which providers may be used. Changes
          take effect on the next run — no redeploy. An unconfigured task uses the
          built-in default.
        </p>
      </header>

      {error && (
        <ErrorCard
          error={error}
          returnTo="/developer/models"
          onRetry={() => void load()}
        />
      )}

      {!data && !error && <Loading />}

      {data && (
        <div className="space-y-10">
          <Providers
            providers={data.providers}
            catalogues={catalogues}
            busy={busy}
            saved={saved}
            onSave={(p, enabled, baseUrl) =>
              withBusy(`provider:${p}`, () => adminApi.saveProvider(p, { enabled, baseUrl }))
            }
          />

          <section>
            <div className="mb-4 flex items-baseline justify-between">
              <h2 className="text-lg font-semibold">Task routing</h2>
              <p className="text-xs" style={{ color: "var(--text-muted)" }}>
                {data.routes.filter((r) => r.source === "configured").length} of{" "}
                {data.routes.length} configured
              </p>
            </div>
            <div className="space-y-4">
              {data.routes.map((route) => (
                <RouteRow
                  key={route.taskClass}
                  route={route}
                  providers={data.providers}
                  catalogues={catalogues}
                  busy={busy}
                  saved={saved}
                  onSave={(tier, chain) =>
                    withBusy(`route:${route.taskClass}`, () =>
                      adminApi.saveRoute(route.taskClass, { tier, chain }),
                    )
                  }
                  onRevert={() =>
                    withBusy(`route:${route.taskClass}`, () =>
                      adminApi.revertRoute(route.taskClass),
                    )
                  }
                />
              ))}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

function Loading() {
  return (
    <div className="space-y-4" aria-label="Loading settings">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="soft-sunken h-20 animate-pulse"
          style={{ borderRadius: "var(--r-md)" }}
        />
      ))}
    </div>
  );
}

/* --------------------------------------------------------------------------- */

function Providers({
  providers,
  catalogues,
  busy,
  saved,
  onSave,
}: {
  providers: Provider[];
  catalogues: Record<string, Catalogue>;
  busy: string | null;
  saved: string | null;
  onSave: (provider: string, enabled: boolean, baseUrl: string | null) => void;
}) {
  return (
    <section>
      <h2 className="mb-4 text-lg font-semibold">Providers</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        {providers.map((p) => (
          <ProviderCard
            key={p.provider}
            provider={p}
            catalogue={catalogues[p.provider]}
            busy={busy === `provider:${p.provider}`}
            saved={saved === `provider:${p.provider}`}
            onSave={onSave}
          />
        ))}
      </div>
    </section>
  );
}

function ProviderCard({
  provider,
  catalogue,
  busy,
  saved,
  onSave,
}: {
  provider: Provider;
  catalogue?: Catalogue;
  busy: boolean;
  saved: boolean;
  onSave: (provider: string, enabled: boolean, baseUrl: string | null) => void;
}) {
  const [baseUrl, setBaseUrl] = useState(provider.baseUrl ?? "");
  const local = !provider.requiresKey && provider.provider !== "fake";

  return (
    <SoftCard className="p-5" size="md">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <SoftTile active={provider.available} className="h-10 w-10 text-sm font-bold">
            {provider.provider.slice(0, 2).toUpperCase()}
          </SoftTile>
          <div>
            <p className="text-sm font-semibold capitalize">{provider.provider}</p>
            <p className="mt-0.5 text-xs" style={{ color: "var(--text-muted)" }}>
              {provider.requiresKey ? "Needs an API key in the environment" : "No key needed"}
            </p>
          </div>
        </div>
        <SoftToggle
          checked={provider.enabled}
          onChange={(next) => onSave(provider.provider, next, baseUrl || null)}
          label={`Enable ${provider.provider}`}
          disabled={busy}
        />
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {provider.available ? (
          <Pill tone="ok">available</Pill>
        ) : (
          <Pill tone="muted">{provider.enabled ? "not configured" : "off"}</Pill>
        )}
        {catalogue && catalogue.models.length > 0 && (
          <Pill tone="muted">{catalogue.models.length} models</Pill>
        )}
        {catalogue && !catalogue.live && catalogue.models.length > 0 && (
          <Pill tone="warn">known list only</Pill>
        )}
        {saved && <Pill tone="accent">saved</Pill>}
      </div>

      {local && (
        <div className="mt-4">
          <label
            className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wider"
            style={{ color: "var(--text-muted)" }}
          >
            Server address
          </label>
          <div className="flex gap-2">
            <SoftInput
              value={baseUrl}
              onChange={setBaseUrl}
              label={`${provider.provider} base URL`}
              placeholder="http://localhost:11434/v1"
              className="flex-1"
            />
            <SoftButton
              onClick={() => onSave(provider.provider, provider.enabled, baseUrl || null)}
              disabled={busy || baseUrl === (provider.baseUrl ?? "")}
            >
              Save
            </SoftButton>
          </div>
        </div>
      )}

      {catalogue?.message && (
        <p className="mt-3 text-xs leading-relaxed" style={{ color: "var(--warn)" }}>
          {catalogue.message}
        </p>
      )}
    </SoftCard>
  );
}

/* --------------------------------------------------------------------------- */

function RouteRow({
  route,
  providers,
  catalogues,
  busy,
  saved,
  onSave,
  onRevert,
}: {
  route: Route;
  providers: Provider[];
  catalogues: Record<string, Catalogue>;
  busy: string | null;
  saved: string | null;
  onSave: (tier: string, chain: { provider: string; model: string }[]) => void;
  onRevert: () => void;
}) {
  const key = `route:${route.taskClass}`;
  const isBusy = busy === key;
  const [open, setOpen] = useState(false);
  const [tier, setTier] = useState(route.tier);
  const [chain, setChain] = useState(route.chain);

  const usable = providers.filter((p) => p.available);

  function setEntry(i: number, patch: Partial<{ provider: string; model: string }>) {
    setChain((prev) => prev.map((e, idx) => (idx === i ? { ...e, ...patch } : e)));
  }

  return (
    <SoftCard className="p-5" size="md">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h3 className="text-sm font-semibold uppercase tracking-wide">{route.taskClass}</h3>
            <Pill tone={route.source === "configured" ? "accent" : "muted"}>{route.source}</Pill>
            {!route.costReportable && <Pill tone="warn">cost not metered</Pill>}
            {saved === key && <Pill tone="ok">saved</Pill>}
          </div>
          <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
            {TASK_HELP[route.taskClass] ?? ""}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span
            className="soft-flat px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider"
            style={{ borderRadius: "var(--r-pill)", color: "var(--primary)" }}
          >
            {route.tier}
          </span>
          <SoftButton variant="quiet" onClick={() => setOpen((v) => !v)}>
            {open ? "Close" : "Change"}
          </SoftButton>
        </div>
      </div>

      <SoftWell className="mt-4 p-3">
        <ol className="space-y-1.5">
          {route.chain.map((entry, i) => (
            <li key={`${entry.provider}-${entry.model}`} className="flex items-center gap-2 text-xs">
              <span
                className="tabular w-5 shrink-0 text-right font-semibold"
                style={{ color: "var(--text-faint)" }}
              >
                {i + 1}
              </span>
              <span className="font-medium" style={{ color: "var(--primary)" }}>
                {entry.provider}
              </span>
              <span style={{ color: "var(--text-faint)" }}>/</span>
              <span className="truncate" style={{ color: "var(--text)" }}>
                {entry.model}
              </span>
              {i === 0 && (
                <span className="ml-auto text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
                  first choice
                </span>
              )}
            </li>
          ))}
        </ol>
      </SoftWell>

      {open && (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
              Tier
            </label>
            <SoftSelect
              value={tier}
              onChange={setTier}
              label={`Tier for ${route.taskClass}`}
              options={TIERS.map((t) => ({ value: t, label: t }))}
            />
          </div>

          {chain.map((entry, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2">
              <span className="tabular w-5 text-right text-xs font-semibold" style={{ color: "var(--text-faint)" }}>
                {i + 1}
              </span>
              <SoftSelect
                value={entry.provider}
                onChange={(p) => setEntry(i, { provider: p, model: "" })}
                label={`Provider ${i + 1}`}
                options={usable.map((p) => ({ value: p.provider, label: p.provider }))}
              />
              <SoftSelect
                value={entry.model}
                onChange={(m) => setEntry(i, { model: m })}
                label={`Model ${i + 1}`}
                className="min-w-[240px] flex-1"
                options={[
                  { value: "", label: "Choose a model…" },
                  ...(catalogues[entry.provider]?.models ?? []).map((m) => ({
                    value: m.id,
                    label: m.priced ? m.label : `${m.label} · cost not metered`,
                  })),
                ]}
              />
              {chain.length > 1 && (
                <SoftButton
                  variant="quiet"
                  onClick={() => setChain((prev) => prev.filter((_, idx) => idx !== i))}
                >
                  Remove
                </SoftButton>
              )}
            </div>
          ))}

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <SoftButton
              variant="quiet"
              onClick={() =>
                setChain((prev) => [...prev, { provider: usable[0]?.provider ?? "openrouter", model: "" }])
              }
            >
              + Add fallback
            </SoftButton>
            <div className="flex-1" />
            {route.source === "configured" && (
              <SoftButton variant="quiet" onClick={onRevert} disabled={isBusy}>
                Revert to default
              </SoftButton>
            )}
            <SoftButton
              variant="primary"
              disabled={isBusy || chain.some((e) => !e.model)}
              onClick={() => onSave(tier, chain)}
            >
              {isBusy ? "Saving…" : "Save route"}
            </SoftButton>
          </div>

          <p className="text-xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
            Order is the fallback order: the first entry is tried first, and the next is
            used only if it fails. Prefer a different <em>vendor</em> for the fallback —
            rate limits and outages tend to be vendor-wide.
          </p>
        </div>
      )}
    </SoftCard>
  );
}
