"use client";

/**
 * The leads, and where each one came from.
 *
 * This is the payoff screen of the whole product and it did not exist. `GET /api/v1/leads`
 * has been returning captured leads with their attribution the entire time and nothing
 * rendered it — so the one thing the product measures itself in was invisible to the person
 * it belongs to.
 *
 * **Attribution is why this screen is worth building, and it is also where it would be
 * easiest to lie.** The endpoint records which content piece captured a lead and which short
 * link the visitor arrived by — as UUIDs. There is no title and no short-link code on this
 * response, and no other endpoint to join them against: `/go/{business}` returns labels but
 * no ids. So this screen shows the link that genuinely exists (the landing page's own public
 * URL, which IS derivable from the content piece id) and names the rest as ids, rather than
 * inventing a headline to put beside a lead. Nothing here is a placeholder standing in for
 * data the API did not send. See `app/lib/leads-api.ts` for the shape.
 *
 * Two consequences of the data being personal:
 *
 * - the body is a list of named people with their phone numbers, so the API sends
 *   `Cache-Control: no-store` and every request here is a browser request under the session
 *   cookie — never a server component, which would send no `Origin` and be refused anyway;
 * - consent is shown, because the API refuses a submission without it. A screen full of
 *   contact details with no evidence of consent is the compliance problem this product would
 *   otherwise hand to every customer it has.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Pill, SoftButton, SoftCard, SoftWell } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import {
  asText,
  fetchLeads,
  landingPageUrl,
  UTM_KEYS,
  type Lead,
} from "@/app/lib/leads-api";

type State =
  | { kind: "loading" }
  | { kind: "ready"; leads: Lead[] }
  | { kind: "error"; message: string };

export default function LeadsPage() {
  const [state, setState] = useState<State>({ kind: "loading" });

  const load = useCallback(async () => {
    try {
      setState({ kind: "ready", leads: (await fetchLeads()).leads });
    } catch (exc) {
      setState({
        kind: "error",
        // The API's own message. A 409 "this account has no business yet" is actionable;
        // "Request failed (409)" is not.
        message: exc instanceof ApiError ? exc.message : "Could not load your leads.",
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Leads
      </p>
      <h1 className="mt-2 text-[26px] font-semibold tracking-tight">Who got in touch</h1>
      <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
        Every lead is recorded against the landing page whose form captured it and the short
        link the visitor arrived by. That chain is what makes a lead traceable to a specific
        piece of content rather than to a guess.
      </p>

      <div className="mt-8 flex flex-wrap items-center justify-between gap-3">
        <span className="text-xs" style={{ color: "var(--text-muted)" }}>
          {state.kind === "ready"
            ? `${state.leads.length} ${state.leads.length === 1 ? "lead" : "leads"}, newest first`
            : " "}
        </span>
        <SoftButton onClick={() => void load()} variant="quiet" ariaLabel="Refresh the leads list">
          Refresh
        </SoftButton>
      </div>

      <div className="mt-5" aria-live="polite">
        {state.kind === "loading" && (
          <p className="text-sm" style={{ color: "var(--text-muted)" }}>
            Loading your leads…
          </p>
        )}

        {state.kind === "error" && (
          <SoftWell className="p-4">
            <p className="text-sm font-medium" style={{ color: "var(--err)" }}>
              {state.message}
            </p>
          </SoftWell>
        )}

        {state.kind === "ready" && state.leads.length === 0 && (
          <SoftWell className="p-5">
            <p className="text-sm font-medium">No leads yet.</p>
            <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
              A lead arrives when somebody submits the form on one of your published landing
              pages. Approve a run&apos;s output, publish the page, and share its short link —
              the link is what ties a lead back to the content that earned it.
            </p>
          </SoftWell>
        )}

        {state.kind === "ready" && state.leads.length > 0 && (
          <ul className="space-y-4">
            {state.leads.map((lead) => (
              <LeadCard key={lead.id} lead={lead} />
            ))}
          </ul>
        )}
      </div>

      <p className="mt-8">
        <Link href="/" className="text-sm font-medium underline" style={{ color: "var(--primary)" }}>
          Back to the dashboard
        </Link>
      </p>
    </main>
  );
}

function LeadCard({ lead }: { lead: Lead }) {
  const name = asText(lead.fields, "name");
  const email = asText(lead.fields, "email");
  const phone = asText(lead.fields, "phone");
  const message = asText(lead.fields, "message");
  // The API requires consent and writes it as a literal `true`, so this is a record of what
  // was captured rather than a field a submitter could have left ambiguous.
  const consented = lead.fields["consent"] === true;

  // Used to keep the accessible names of the contact links distinct. Several leads on one
  // screen would otherwise give a screen-reader user a list of identical "Email" links.
  const who = name ?? email ?? phone ?? "this lead";

  return (
    <li>
      <SoftCard as="article" className="p-5" size="md">
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={lead.status === "new" ? "accent" : "muted"}>{lead.status}</Pill>
          <span
            className="text-[11px] font-semibold uppercase tracking-wider"
            style={{ color: "var(--text-muted)" }}
          >
            via {lead.source}
          </span>
          <span className="ms-auto text-[11px]" style={{ color: "var(--text-muted)" }}>
            <LeadTime iso={lead.createdAt} />
          </span>
        </div>

        <h2 className="mt-3 text-base font-semibold">{name ?? "No name given"}</h2>

        <dl className="mt-2 grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-[auto_1fr]">
          <dt style={{ color: "var(--text-muted)" }}>Email</dt>
          <dd>
            {email ? (
              <a
                href={`mailto:${email}`}
                className="underline"
                style={{ color: "var(--primary)" }}
                aria-label={`Email ${who} at ${email}`}
              >
                {email}
              </a>
            ) : (
              <span style={{ color: "var(--text-muted)" }}>not given</span>
            )}
          </dd>

          <dt style={{ color: "var(--text-muted)" }}>Phone</dt>
          <dd>
            {phone ? (
              <a
                href={`tel:${phone.replace(/\s+/g, "")}`}
                className="underline"
                style={{ color: "var(--primary)" }}
                aria-label={`Call ${who} on ${phone}`}
              >
                {phone}
              </a>
            ) : (
              <span style={{ color: "var(--text-muted)" }}>not given</span>
            )}
          </dd>
        </dl>

        {message && (
          <SoftWell className="mt-4 p-4">
            {/* `whitespace-pre-line` so the line breaks a person typed survive, and the text
                is rendered as text — never as HTML. It came from an anonymous public form. */}
            <p className="whitespace-pre-line text-sm">{message}</p>
          </SoftWell>
        )}

        <Attribution lead={lead} who={who} />

        <p className="mt-3 text-[11px]" style={{ color: "var(--text-muted)" }}>
          {consented
            ? "Consent recorded at submission."
            : "No consent recorded — the form should not have accepted this."}
        </p>
      </SoftCard>
    </li>
  );
}

/**
 * Where the lead came from.
 *
 * The honest version. Three things can be known and each is shown only when it is:
 *
 * - the **landing page** that captured it — its public URL is derivable from the content
 *   piece id, so this is a real link and not an id dressed up as one. It resolves only once
 *   the page is published; a draft answers 404 by design, which the copy says;
 * - the **UTM parameters** the visitor arrived with, which are the only human-readable part
 *   of the attribution the API stores;
 * - the **short link**, which the API records as an id. There is no code on this response and
 *   nothing to join it against, so it is shown as an id and called one.
 *
 * When none of them is present the card says so plainly. A lead with no attribution is a
 * real outcome — somebody typed the URL, or the page was reached without a tracked link —
 * and filling that space with an invented source would corrupt the one number this product
 * asks to be judged on.
 */
function Attribution({ lead, who }: { lead: Lead; who: string }) {
  const utm = UTM_KEYS.map((key) => [key, asText(lead.utm, key)] as const).filter(
    (pair): pair is readonly [(typeof UTM_KEYS)[number], string] => pair[1] !== null,
  );
  const nothing = !lead.contentPieceId && !lead.shortLinkId && utm.length === 0;

  return (
    <div className="mt-4 border-t pt-3.5" style={{ borderColor: "var(--edge)" }}>
      <h3
        className="text-[11px] font-semibold uppercase tracking-wider"
        style={{ color: "var(--text-muted)" }}
      >
        What earned this lead
      </h3>

      {nothing && (
        <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
          Nothing recorded. The form was reached without a tracked link, so this lead cannot
          be attributed to a piece of content.
        </p>
      )}

      {lead.contentPieceId && (
        <p className="mt-2 text-sm">
          <a
            href={landingPageUrl(lead.contentPieceId)}
            target="_blank"
            rel="noreferrer noopener"
            className="underline"
            style={{ color: "var(--primary)" }}
            aria-label={`Open the landing page that captured the lead from ${who}, in a new tab`}
          >
            The landing page that captured it
          </a>{" "}
          <span style={{ color: "var(--text-muted)" }}>
            (opens in a new tab; the page resolves only while it is published)
          </span>
        </p>
      )}

      {utm.length > 0 && (
        <ul className="mt-2.5 flex flex-wrap gap-2">
          {utm.map(([key, value]) => (
            <li key={key}>
              {/* The raw parameter name is kept rather than prettified into "Source". These
                  are the exact values the visitor's URL carried, and relabelling them invites
                  a reader to hear a claim the measurement does not make. */}
              <Pill tone="muted">
                {key.replace("utm_", "")}: {value}
              </Pill>
            </li>
          ))}
        </ul>
      )}

      {lead.shortLinkId && (
        <p className="mt-2.5 text-xs" style={{ color: "var(--text-muted)" }}>
          Arrived by one of your short links — recorded as id{" "}
          <code className="tabular rounded px-1" style={{ background: "var(--surface-sunken)" }}>
            {lead.shortLinkId}
          </code>
          . This endpoint returns the id rather than the link&apos;s code.
        </p>
      )}
    </div>
  );
}

/** See `RunTime` in `components/run-rows.tsx` for why this is absolute and NaN-guarded. */
function LeadTime({ iso }: { iso: string }) {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return <>time not recorded</>;
  return (
    <time className="tabular" dateTime={iso}>
      {parsed.toLocaleString()}
    </time>
  );
}
