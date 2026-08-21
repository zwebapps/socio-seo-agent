"use client";

/** Sign in, or create the first account. Same soft-UI language as the admin screens. */

import { useState } from "react";
import { Pill, SoftButton, SoftCard, SoftInput } from "../components/soft";
import { landingFor, safeNext } from "../lib/roles";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

type Mode = "login" | "signup";

export default function LoginPage() {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [business, setBusiness] = useState("");
  const [state, setState] = useState<
    { kind: "idle" } | { kind: "busy" } | { kind: "error"; message: string }
  >({ kind: "idle" });

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setState({ kind: "busy" });

    const body =
      mode === "signup"
        ? { email, password, businessName: business }
        : { email, password };

    try {
      const response = await fetch(`${API_URL}/api/v1/auth/${mode}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        const payload: unknown = await response.json().catch(() => null);
        const detail = (payload as { detail?: unknown } | null)?.detail;
        const message =
          detail && typeof detail === "object" && "message" in detail
            ? String((detail as { message: string }).message)
            : Array.isArray(detail) && detail.length
              ? String((detail[0] as { msg?: string }).msg ?? "That did not work.")
              : "That did not work.";
        setState({ kind: "error", message });
        return;
      }

      // Role-based, and read from the response we already have: `POST /login` returns
      // `UserOut`, which carries `role`. A signup has no role in its response and is
      // always a business owner, so it takes the owner landing.
      const landed: unknown = await response.json().catch(() => null);
      const role =
        mode === "signup"
          ? "owner"
          : ((landed as { role?: string } | null)?.role ?? null);

      const requested = safeNext(new URLSearchParams(window.location.search).get("next"));
      window.location.href = requested ?? landingFor(role);
    } catch {
      setState({
        kind: "error",
        message: `Cannot reach the API at ${API_URL}. Start it with \`make api\`.`,
      });
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-12">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Social Marketing Agent
      </p>
      <h1 className="mt-2 text-[26px] font-semibold tracking-tight">
        {mode === "login" ? "Sign in" : "Create your account"}
      </h1>

      <SoftCard className="mt-7 p-6" size="lg">
        <form onSubmit={submit} className="space-y-4">
          <Field label="Email">
            <SoftInput value={email} onChange={setEmail} label="Email" className="w-full" />
          </Field>

          <Field label="Password" hint={mode === "signup" ? "At least 12 characters" : undefined}>
            <input
              aria-label="Password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="soft-sunken soft-edge w-full px-3 py-2 text-sm"
              style={{ borderRadius: "var(--r-sm)", color: "var(--text)" }}
            />
          </Field>

          {mode === "signup" && (
            <Field label="Business name">
              <SoftInput
                value={business}
                onChange={setBusiness}
                label="Business name"
                className="w-full"
              />
            </Field>
          )}

          {state.kind === "error" && (
            <div aria-live="polite">
              <Pill tone="err">{state.message}</Pill>
            </div>
          )}

          <SoftButton
            type="submit"
            variant="primary"
            className="w-full"
            disabled={state.kind === "busy"}
          >
            {state.kind === "busy"
              ? "Working…"
              : mode === "login"
                ? "Sign in"
                : "Create account"}
          </SoftButton>
        </form>
      </SoftCard>

      <p className="mt-5 text-center text-sm" style={{ color: "var(--text-muted)" }}>
        {mode === "login" ? "No account yet?" : "Already have one?"}{" "}
        <button
          type="button"
          onClick={() => {
            setMode(mode === "login" ? "signup" : "login");
            setState({ kind: "idle" });
          }}
          className="font-medium underline decoration-2 underline-offset-4"
          style={{ color: "var(--primary)" }}
        >
          {mode === "login" ? "Create one" : "Sign in"}
        </button>
      </p>
    </main>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label
        className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wider"
        style={{ color: "var(--text-muted)" }}
      >
        {label}
      </label>
      {children}
      {hint && (
        <p className="mt-1 text-xs" style={{ color: "var(--text-faint)" }}>
          {hint}
        </p>
      )}
    </div>
  );
}
