"use client";

import { useState } from "react";

import { Shell } from "@/app/components/page-shell";
import {
  ApiError,
  confirmOnboarding,
  previewOnboarding,
  type PreviewResponse,
} from "../lib/api";

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; data: PreviewResponse }
  | { kind: "error"; code: string; message: string };

/** Where the confirm step is, separate from the draft's own state. */
type SaveState =
  | { kind: "unsaved" }
  | { kind: "saving" }
  | { kind: "saved"; website: string }
  | { kind: "failed"; message: string };

export default function OnboardPage() {
  const [url, setUrl] = useState("");
  const [state, setState] = useState<State>({ kind: "idle" });
  const [save, setSave] = useState<SaveState>({ kind: "unsaved" });

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", data: await previewOnboarding(url.trim()) });
      // A new draft has not been confirmed, whatever the previous one's state was.
      setSave({ kind: "unsaved" });
    } catch (error) {
      const e = error as ApiError;
      setState({ kind: "error", code: e.code ?? "unknown", message: e.message });
    }
  }

  async function confirm(data: PreviewResponse) {
    setSave({ kind: "saving" });
    try {
      const stored = await confirmOnboarding(data.dna, data.sourceUrl);
      setSave({ kind: "saved", website: stored.website });
    } catch (error) {
      const e = error as ApiError;
      setSave({ kind: "failed", message: e.message });
    }
  }

  return (
    <Shell className="py-14">
      <p
        className="text-xs font-semibold uppercase tracking-widest"
        style={{ color: "var(--accent)" }}
      >
        Step 1 of 3 · Your business
      </p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight">
        Let&apos;s start with your website
      </h1>
      <p className="mt-3 text-base" style={{ color: "var(--text-muted)" }}>
        We read your homepage and draft a profile. You confirm or correct it —
        nothing is used until you do.
      </p>

      <form onSubmit={submit} className="mt-8 flex flex-col gap-3 sm:flex-row">
        <label htmlFor="url" className="sr-only">
          Website address
        </label>
        <input
          id="url"
          type="url"
          required
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://your-business.de"
          className="flex-1 rounded-lg border px-4 py-3 text-base"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border)",
            color: "var(--text)",
          }}
        />
        <button
          type="submit"
          disabled={state.kind === "loading" || url.trim() === ""}
          className="rounded-lg px-5 py-3 text-base font-medium text-white transition-opacity disabled:opacity-50"
          style={{ background: "var(--brand)" }}
        >
          {state.kind === "loading" ? "Reading…" : "Read my site"}
        </button>
      </form>

      <div className="mt-8" aria-live="polite">
        {state.kind === "loading" && <Skeleton />}
        {state.kind === "error" && <ErrorPanel code={state.code} message={state.message} />}
        {state.kind === "ready" && (
          <>
            <Draft data={state.data} />
            <Confirm state={save} onConfirm={() => void confirm(state.data)} />
          </>
        )}
      </div>
    </Shell>
  );
}

/**
 * The step that used to be missing.
 *
 * Before this, the draft was displayed and thrown away: `businesses.dna` stayed `{}` for
 * every business ever created, so a later run had no website to crawl and the
 * regulated-claim guard had no claims to enforce. The page's own copy promised "nothing
 * is used until you confirm", which was accidentally true because nothing was used at all.
 *
 * The saved state names the website explicitly rather than saying "Saved", because that
 * key is the one doing the work -- it is what makes the next run read this site.
 */
function Confirm({
  state,
  onConfirm,
}: {
  state: SaveState;
  onConfirm: () => void;
}) {
  if (state.kind === "saved") {
    return (
      <div
        className="mt-6 rounded-xl border p-4"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        // Announced, because the button that triggered this is replaced by it.
        aria-live="polite"
      >
        <p className="text-sm font-medium" style={{ color: "var(--ok, var(--primary))" }}>
          Saved. Runs for this business will now read {state.website}.
        </p>
        <a
          href="/"
          className="soft-press mt-3 inline-block px-4 py-2 text-sm font-medium"
          style={{
            borderRadius: "var(--r-pill)",
            background: "var(--primary)",
            color: "var(--primary-ink)",
            boxShadow:
              "-4px -4px 10px var(--shadow-light), 5px 5px 14px var(--shadow-dark)",
          }}
        >
          Next: start a run
        </a>
      </div>
    );
  }

  return (
    <div className="mt-6" aria-live="polite">
      <button
        type="button"
        onClick={onConfirm}
        disabled={state.kind === "saving"}
        className="soft-press px-4 py-2 text-sm font-medium disabled:opacity-45"
        style={{
          borderRadius: "var(--r-pill)",
          background: "var(--primary)",
          color: "var(--primary-ink)",
          boxShadow:
            "-4px -4px 10px var(--shadow-light), 5px 5px 14px var(--shadow-dark)",
        }}
      >
        {state.kind === "saving" ? "Saving…" : "This is correct — save it"}
      </button>
      <p className="mt-2 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
        Until you save, this draft is not used by anything.
      </p>
      {state.kind === "failed" ? (
        <p className="mt-2 max-w-[70ch] text-sm" style={{ color: "var(--err)" }}>
          {state.message}
        </p>
      ) : null}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="animate-pulse space-y-3" aria-label="Reading your website">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="h-4 rounded"
          style={{ background: "var(--border)", width: `${90 - i * 20}%` }}
        />
      ))}
    </div>
  );
}

/** Every anticipated failure gets its own next step. A dead end is a lost signup. */
function ErrorPanel({ code, message }: { code: string; message: string }) {
  const nextStep: Record<string, string> = {
    thin_site: "Fill in the short form instead — it takes about a minute.",
    unsafe_url: "Please enter the public address of your website.",
    site_unreachable: "Check the address, or try again in a moment.",
    extraction_failed: "Fill in the short form instead — it takes about a minute.",
    invalid_request: "Include the full address, starting with https://",
    network: "Start the API with `make api`, then try again.",
  };

  return (
    <div
      className="rounded-xl border p-5"
      style={{ background: "var(--surface)", borderColor: "var(--err)" }}
      role="alert"
    >
      <p className="text-sm font-semibold" style={{ color: "var(--err)" }}>
        We couldn&apos;t read that site
      </p>
      <p className="mt-1 text-sm" style={{ color: "var(--text)" }}>
        {message}
      </p>
      {nextStep[code] && (
        <p className="mt-3 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
          {nextStep[code]}
        </p>
      )}
    </div>
  );
}

function Draft({ data }: { data: PreviewResponse }) {
  const { dna, usage, factGaps, instructionLikeContent } = data;

  return (
    <div className="space-y-4">
      {instructionLikeContent && (
        <div
          className="rounded-xl border p-4"
          style={{ background: "var(--surface)", borderColor: "var(--warn)" }}
          role="status"
        >
          <p className="text-sm font-semibold" style={{ color: "var(--warn)" }}>
            Instruction-like content was found on that page and ignored
          </p>
          <p className="mt-1 text-sm" style={{ color: "var(--text-muted)" }}>
            Text on the page tried to give our system instructions. It was treated
            as data, not as a command, and the analysis continued.
          </p>
        </div>
      )}

      <section
        className="rounded-xl border p-6"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-lg font-semibold">Here&apos;s what we understood</h2>
          <span className="text-xs" style={{ color: "var(--text-muted)" }}>
            {usage.tokensIn + usage.tokensOut} tokens · ${usage.usd.toFixed(4)}
          </span>
        </div>

        <dl className="mt-5 grid gap-x-8 gap-y-4 sm:grid-cols-2">
          <Field label="Business" value={dna.name} />
          <Field label="Industry" value={dna.industry} />
          <Field label="City" value={dna.city} />
          <Field label="Language" value={dna.locale} />
          <ListField label="Services" values={dna.services} />
          <ListField label="Customers" values={dna.audience} />
        </dl>

        {factGaps.length > 0 && (
          <p className="mt-5 text-sm" style={{ color: "var(--text-muted)" }}>
            We couldn&apos;t determine: <strong>{factGaps.join(", ")}</strong>. You can
            add those next — we&apos;d rather ask than guess.
          </p>
        )}

        <div className="mt-6 flex flex-wrap gap-3">
          <button
            type="button"
            className="rounded-lg px-4 py-2 text-sm font-medium text-white"
            style={{ background: "var(--brand)" }}
          >
            Looks right — continue
          </button>
          <button
            type="button"
            className="rounded-lg border px-4 py-2 text-sm font-medium"
            style={{ borderColor: "var(--border)", color: "var(--text)" }}
          >
            Correct something
          </button>
        </div>
      </section>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide" style={{ color: "var(--text-muted)" }}>
        {label}
      </dt>
      <dd className="mt-1 text-sm">
        {value ?? <span style={{ color: "var(--text-muted)" }}>not stated on the page</span>}
      </dd>
    </div>
  );
}

function ListField({ label, values }: { label: string; values: string[] }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide" style={{ color: "var(--text-muted)" }}>
        {label}
      </dt>
      <dd className="mt-1 flex flex-wrap gap-1.5">
        {values.length === 0 ? (
          <span className="text-sm" style={{ color: "var(--text-muted)" }}>
            none found
          </span>
        ) : (
          values.map((v) => (
            <span
              key={v}
              className="rounded-full px-2.5 py-1 text-xs"
              style={{ background: "var(--bg)", color: "var(--text)" }}
            >
              {v}
            </span>
          ))
        )}
      </dd>
    </div>
  );
}
