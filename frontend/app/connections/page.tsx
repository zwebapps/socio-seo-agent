"use client";

/**
 * Platform accounts: what is connected, what it can actually do, and how to disconnect it.
 *
 * The four routes in `backend/app/api/connections.py` were shipped and tested and nothing
 * rendered them, so a business could not connect an account at all — the same gap the
 * documents screen closed for the knowledge base. This is the screen, and nothing more:
 * no route is changed and no usability decision is made here.
 *
 * Five decisions shape it, and each one is a rule about what this page is not allowed to
 * do.
 *
 * **Usability is the server's verdict, rendered.** `usable` and `unusableReason` come
 * from `ConnectionView.unusable_reason` — the same function `actuators/social.py` asks
 * before it refuses to publish. So the sentence an owner reads here is the sentence the
 * refusal would carry, and there is no clock arithmetic anywhere in this file. A client
 * that recomputed it would drift, and the failure is the expensive direction: a green
 * account whose posts silently go nowhere.
 *
 * **A simulated connection is labelled as simulated, everywhere it appears.** Every
 * provider behind this today is `FakeOAuthProvider` (`platform_oauth`'s docstring says
 * why no real client is written), so `fake` is true on the status, on the connect
 * response and on every row that came from one. Rendering one of those identically to a
 * real connection would have an owner believe their Instagram is live. It is the worst
 * thing this screen could do, so it is said three times: on the capability card, on the
 * row, and on the panel that ends a connect attempt.
 *
 * **App Review is stated as somebody else's queue.** Facebook, Instagram, LinkedIn and
 * TikTok gate publishing on their own review — weeks, with a screencast and business
 * verification, refusable. A screen that offers "Connect Instagram" and lets an owner
 * infer that the button is the last step generates a support ticket at best and a
 * misplaced launch plan at worst.
 *
 * **Credential storage is checked BEFORE the button, not after the round trip.** With no
 * `PLATFORM_CREDENTIAL_KEY` the API refuses the connect with a 503 — correctly — but
 * discovering that at the end of a consent flow means the customer has authorised an
 * account whose token we then throw away, leaving a live grant we never recorded and can
 * therefore never revoke. So the state is on the screen first and the buttons are
 * disabled with the reason beside them.
 *
 * **The credential is write-only.** `credentialHint` (four and four) is the most this
 * page ever shows, and `hasCredential` is rendered as a sentence about existence rather
 * than as a value. There is no field on the wire that could carry a token, which is a
 * property of the API's types rather than of the care taken here.
 *
 * A client component, for the usual reason: every call carries the session cookie, and
 * the API's Origin-CSRF guard refuses a cookie-bearing write that arrives with no
 * `Origin` header — which is exactly what `fetch` from a server component sends.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/app/components/page-shell";
import { Pill, SoftButton, SoftCard, SoftWell } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import {
  type Connection,
  type ConnectStart,
  type ConnectionList,
  type CredentialStorage,
  type OAuthStatus,
  connectionTone,
  connectionVerdict,
  disconnectPlatform,
  fetchConnections,
  platformLabel,
  platformRows,
  startConnect,
} from "@/app/lib/connections-api";

type ListState =
  | { kind: "loading" }
  | { kind: "ready"; list: ConnectionList }
  | { kind: "error"; message: string };

/**
 * A connect attempt, which belongs to ONE platform at a time.
 *
 * Keyed by platform rather than held as a flat flag so the outcome renders inside the row
 * it belongs to: a panel about Instagram floating above a list of six platforms is a
 * panel an owner has to guess the subject of.
 */
type ConnectState =
  | { kind: "idle" }
  | { kind: "starting"; platform: string }
  | { kind: "started"; start: ConnectStart }
  | { kind: "error"; platform: string; message: string };

export default function ConnectionsPage() {
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [connect, setConnect] = useState<ConnectState>({ kind: "idle" });
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setState({ kind: "ready", list: await fetchConnections() });
    } catch (exc) {
      // A 409 `no_business` is an account that has not finished onboarding, and a 401 is
      // a session that has gone. The API's own message says which; passed through rather
      // than replaced with a guess.
      setState({
        kind: "error",
        message:
          exc instanceof ApiError ? exc.message : "Could not load your platform accounts.",
      });
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function begin(platform: string) {
    setNotice(null);
    setConnect({ kind: "starting", platform });
    try {
      setConnect({ kind: "started", start: await startConnect(platform) });
    } catch (exc) {
      // The 503 here is the credential-storage refusal, and its message names the
      // environment variable and what to set it to. Shown verbatim: a rewrite would drop
      // the only actionable part of it.
      setConnect({
        kind: "error",
        platform,
        message:
          exc instanceof ApiError
            ? exc.message
            : `Could not start connecting ${platformLabel(platform)}.`,
      });
    }
  }

  async function remove(platform: string) {
    setNotice(null);
    try {
      await disconnectPlatform(platform);
      // The route is idempotent and answers 204 either way, so the only honest report is
      // about the END STATE — "removed" would claim knowledge of a row we were never told
      // about.
      setNotice(
        `${platformLabel(platform)} is disconnected. The credential was revoked at the ` +
          "provider where possible and forgotten here either way.",
      );
      setConnect({ kind: "idle" });
      await reload();
    } catch (exc) {
      setNotice(
        exc instanceof ApiError
          ? exc.message
          : `Could not disconnect ${platformLabel(platform)}.`,
      );
    }
  }

  return (
    <Shell className="py-12">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Platform accounts
      </p>
      <h1 className="mt-2 text-[26px] font-semibold tracking-tight">
        Accounts this business has connected
      </h1>
      <p className="mt-3 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
        A connection is a stored credential for one account, and it is what a publish step
        checks before it does anything. This screen shows whether each one would actually
        be accepted right now — in the same words the refusal uses — and lets you connect
        or disconnect an account.
      </p>

      {state.kind === "loading" && (
        <p className="mt-8 text-sm" style={{ color: "var(--text-muted)" }}>
          Loading your platform accounts…
        </p>
      )}

      {state.kind === "error" && (
        <SoftCard className="mt-8 p-5" size="md">
          <p className="text-sm font-semibold" style={{ color: "var(--err)" }} role="alert">
            {state.message}
          </p>
          <p className="mt-3">
            <SoftButton onClick={() => void reload()}>Try again</SoftButton>
          </p>
        </SoftCard>
      )}

      {state.kind === "ready" && (
        <>
          {/* Storage first, because it decides whether the buttons below can work at
              all. See the module note on why this is not a footnote. */}
          <StorageCard storage={state.list.credentialStorage} />
          <CapabilityCard oauth={state.list.oauth} />

          <section className="mt-10" aria-labelledby="accounts-heading">
            <h2 id="accounts-heading" className="text-sm font-semibold">
              Every platform you can connect
            </h2>
            <p className="mt-1.5 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
              Listed whether or not it is connected, so a missing account is visibly missing
              rather than absent from the page.
            </p>

            {/* One live region for every outcome, so a disconnect or a refusal is
                announced without the focus having to move. */}
            <p
              aria-live="polite"
              className="mt-4 min-h-5 max-w-[70ch] text-sm font-medium"
              style={{ color: "var(--accent)" }}
            >
              {notice}
            </p>

            <ul className="mt-2 space-y-3">
              {platformRows(state.list).map((row) => (
                <PlatformCard
                  key={row.platform}
                  platform={row.platform}
                  connection={row.connection}
                  canConnect={state.list.credentialStorage.canStoreCredentials}
                  appReview={state.list.oauth.blockedOnAppReview.includes(row.platform)}
                  connect={connect}
                  onConnect={begin}
                  onDisconnect={remove}
                />
              ))}
            </ul>
          </section>
        </>
      )}

      <p className="mt-10">
        <Link
          href="/"
          className="text-sm font-medium underline"
          style={{ color: "var(--primary)" }}
        >
          Back to the dashboard
        </Link>
      </p>
    </Shell>
  );
}

/* ------------------------------------------------------------------------- */

/**
 * Whether a credential could be stored at all — and it is the first card on purpose.
 *
 * `canStoreCredentials` false is not a warning to read afterwards: it means every button
 * below is inert, and saying so here is the difference between "this is not set up yet"
 * and a customer authorising an account for nothing.
 *
 * The pill states the SCHEME, which is a fact, rather than a verdict about it. The
 * nuance that a green-looking `v1.ephemeral` does not survive a restart is in the
 * server's own sentence, which is rendered verbatim rather than summarised.
 */
function StorageCard({ storage }: { storage: CredentialStorage }) {
  const tone = !storage.canStoreCredentials ? "err" : storage.protectsAtRest ? "ok" : "warn";

  return (
    <SoftCard className="mt-8 p-6" size="lg">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">Where a credential would be kept</h2>
        <Pill tone={tone}>{storage.scheme}</Pill>
      </div>

      <SoftWell className="mt-4 p-4">
        <p className="max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
          {storage.message}
        </p>
      </SoftWell>

      {!storage.canStoreCredentials && (
        <p className="mt-3 max-w-[70ch] text-sm font-medium" style={{ color: "var(--err)" }}>
          Connecting is unavailable until that is set, and it is refused here rather than
          after the round trip. Otherwise the authorisation would succeed at the platform
          and the token would come back with nowhere safe to put it — leaving a live grant
          on the account holder&rsquo;s side that we hold no record of and could never
          revoke.
        </p>
      )}
    </SoftCard>
  );
}

/**
 * What connecting can do today, in the server's words plus the part it only names.
 *
 * `oauth.message` is rendered verbatim (it is written to be read by a human as-is), and
 * the App Review sentence expands `blockedOnAppReview` from a list of keys into the thing
 * that list means: a third party's queue, measured in weeks, that connecting an account
 * does not enter.
 */
function CapabilityCard({ oauth }: { oauth: OAuthStatus }) {
  const gated = oauth.blockedOnAppReview.map(platformLabel);

  return (
    <SoftCard className="mt-6 p-6" size="lg" as="section">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">What connecting can do today</h2>
        <Pill tone={oauth.usingFakeProviders ? "warn" : "ok"}>
          {oauth.usingFakeProviders ? "simulated providers" : "live providers"}
        </Pill>
      </div>

      <SoftWell className="mt-4 p-4">
        <p className="max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
          {oauth.message}
        </p>
      </SoftWell>

      {gated.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
            Waiting on App Review
          </h3>
          <p className="mt-2 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
            Publishing to {sentenceList(gated)} is gated on each platform&rsquo;s own App
            Review: a submission with a screencast, a privacy policy and business
            verification, assessed by that platform over roughly two to six weeks, and
            refusable. That queue belongs to them, not to us. Connecting an account here
            does not enter it, shorten it or stand in for it — until a platform approves the
            app, nothing reaches that platform from this product, however healthy this
            screen looks.
          </p>
          <p className="mt-2 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
            What works in the meantime is the export pack on each run: the copy is written,
            adapted per channel and measured against that channel&rsquo;s limits, for a
            person to paste in themselves.
          </p>
        </div>
      )}

      {oauth.realProviders.length > 0 && (
        <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
          Real adapters: {sentenceList(oauth.realProviders.map(platformLabel))}.
        </p>
      )}
    </SoftCard>
  );
}

/* ------------------------------------------------------------------------- */

/**
 * One platform: its connection if it has one, and the controls that change that.
 *
 * The disconnect confirms inline rather than through `window.confirm` — the same choice
 * the memory panel makes, and for a stronger reason here: this one revokes a credential at
 * the provider, so a stray click costs a re-authorisation the owner has to do at the
 * platform.
 */
function PlatformCard({
  platform,
  connection,
  canConnect,
  appReview,
  connect,
  onConnect,
  onDisconnect,
}: {
  platform: string;
  connection: Connection | null;
  canConnect: boolean;
  appReview: boolean;
  connect: ConnectState;
  onConnect: (platform: string) => Promise<void>;
  onDisconnect: (platform: string) => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const label = platformLabel(platform);
  const mine =
    (connect.kind === "starting" && connect.platform === platform) ||
    (connect.kind === "started" && connect.start.platform === platform) ||
    (connect.kind === "error" && connect.platform === platform);

  return (
    <li>
      {/* `soft-edge` because this row holds interactive controls and a neumorphic shadow
          measures about 1.2:1 — the hairline is what carries SC 1.4.11. */}
      <div className="soft-flat soft-edge px-4 py-4" style={{ borderRadius: "var(--r-sm)" }}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-56 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold">{label}</span>
              {connection ? (
                <Pill tone={connectionTone(connection)}>{connectionVerdict(connection)}</Pill>
              ) : (
                <Pill tone="muted">not connected</Pill>
              )}
              {/* Said on the row as well as on the card above: this is the fact most
                  easily missed and the most costly to miss. */}
              {connection?.fake && <Pill tone="warn">simulated</Pill>}
            </div>

            {connection ? (
              <ConnectionDetail connection={connection} />
            ) : (
              <p className="mt-2 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
                No credential is stored for {label}, so a publish step for it is refused
                before it starts.
                {appReview &&
                  " Connecting it would not change that yet — see App Review above."}
              </p>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {/* Every connect button is disabled while ANY of them is starting, not just
                this one. The `state` nonce lives in one signed cookie, so a second flow
                begun before the first finishes overwrites the nonce and silently refuses
                the earlier callback — a failure that would surface as "connecting is
                broken" rather than as "you started two". */}
            <SoftButton
              onClick={() => void onConnect(platform)}
              disabled={!canConnect || connect.kind === "starting"}
              ariaLabel={
                connection
                  ? `Reconnect the ${label} account`
                  : `Start connecting a ${label} account`
              }
            >
              {connection ? "Reconnect" : "Connect"}
            </SoftButton>

            {connection && !confirming && (
              <SoftButton
                variant="quiet"
                onClick={() => setConfirming(true)}
                ariaLabel={`Disconnect the ${label} account`}
              >
                Disconnect
              </SoftButton>
            )}
          </div>
        </div>

        {!canConnect && (
          <p className="mt-2 text-xs" style={{ color: "var(--warn)" }}>
            Connecting is unavailable until credential storage is configured.
          </p>
        )}

        {confirming && connection && (
          <SoftWell className="mt-3 p-3">
            <p className="text-sm">
              Disconnect {label}
              {connection.externalAccountName ? ` (${connection.externalAccountName})` : ""}?
            </p>
            <p className="mt-1 max-w-[70ch] text-xs" style={{ color: "var(--text-muted)" }}>
              The credential is revoked at the provider where that is possible, and
              forgotten here either way. Reconnecting means authorising the account again.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <SoftButton
                variant="primary"
                onClick={() => {
                  setConfirming(false);
                  void onDisconnect(platform);
                }}
                ariaLabel={`Confirm disconnecting the ${label} account`}
              >
                Disconnect it
              </SoftButton>
              <SoftButton variant="quiet" onClick={() => setConfirming(false)}>
                Keep it
              </SoftButton>
            </div>
          </SoftWell>
        )}

        {mine && connect.kind === "starting" && (
          <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
            Preparing the {label} authorisation…
          </p>
        )}
        {mine && connect.kind === "error" && (
          <p className="mt-3 max-w-[70ch] text-sm font-medium" style={{ color: "var(--err)" }} role="alert">
            {connect.message}
          </p>
        )}
        {mine && connect.kind === "started" && <ConnectStarted start={connect.start} />}
      </div>
    </li>
  );
}

/**
 * The facts about a stored connection, and its verdict in words.
 *
 * `unusableReason` is printed as the server wrote it. It is the publish refusal's own
 * sentence, so an owner comparing this screen to a refused run reads the same thing twice
 * rather than two descriptions of one state.
 */
function ConnectionDetail({ connection }: { connection: Connection }) {
  return (
    <div className="mt-2">
      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        {connection.externalAccountName ?? "Unnamed account"} · account{" "}
        <span className="tabular">{connection.externalAccountId}</span> · stored status{" "}
        {connection.status}
      </p>

      {!connection.usable && connection.unusableReason && (
        <p className="mt-1.5 max-w-[70ch] text-xs font-medium" style={{ color: "var(--err)" }}>
          Nothing can be published on this: {connection.unusableReason}.
        </p>
      )}

      {connection.usable && connection.needsRenewal && (
        <p className="mt-1.5 max-w-[70ch] text-xs font-medium" style={{ color: "var(--warn)" }}>
          This credential is at or near its expiry, so publishing on it is a race.
          Reconnect the account to renew it.
        </p>
      )}

      {connection.fake && (
        <p className="mt-1.5 max-w-[70ch] text-xs" style={{ color: "var(--warn)" }}>
          Simulated: this grant came from the built-in fake provider, not from{" "}
          {platformLabel(connection.platform)}. It exercises the whole connect, expire and
          revoke lifecycle inside this process, and it reaches no platform and no network.
        </p>
      )}

      <p className="mt-1.5 text-xs" style={{ color: "var(--text-faint)" }}>
        {connection.hasCredential ? (
          <>
            {/* Four and four, which is `mask_secret`'s form. Enough to match this row
                against a token somebody is holding, and useless to anyone else — and it
                is the most any surface in this product ever shows. */}
            Credential <span className="tabular">{connection.credentialHint}</span> ·{" "}
            {connection.credentialScheme}
          </>
        ) : (
          <>No credential is stored for this account.</>
        )}
        {connection.expiresAt && (
          <>
            {" · expires "}
            <ExpiryTime iso={connection.expiresAt} />
          </>
        )}
      </p>

      {connection.scopes.length > 0 && (
        <p className="mt-1.5 max-w-[70ch] text-xs" style={{ color: "var(--text-faint)" }}>
          {/* What was GRANTED, which is not always what was asked for — a token issued
              with a subset of the publish scopes is the usual reason a publish fails long
              after the connection looked fine. */}
          Granted: {connection.scopes.join(", ")}
        </p>
      )}
    </div>
  );
}

/**
 * What a started connect produced — and the branch that matters is `fake`.
 *
 * **Both branches are links now, and the difference is what is at the other end.** A
 * simulated authorisation used to point at `fake-oauth.invalid` — a domain RFC 2606
 * reserves so that it can never resolve — so it was rendered as inert text: offering it
 * would have sent an owner to a browser error and let them conclude that connecting is
 * broken, when in fact there was no real platform app to connect to. It now points at a
 * stand-in consent screen served by the API itself (`api/connections.simulated_consent`),
 * which does resolve and does complete the round trip, so rendering it as text would be
 * the mistake: it would hide a working path.
 *
 * What must not change is that a simulation is never dressed as the real thing. The pill,
 * the heading and the copy all say so, and the copy says what approving it does and does
 * not do — the credential it produces is stored flagged as simulated, is labelled that way
 * on the row above, and cannot publish anything.
 *
 * It is a real anchor rather than a scripted redirect, deliberately: the destination is
 * visible before the click, it can be opened in a new tab, and it is keyboard reachable
 * and announced without any of that having to be re-implemented. The address is also
 * printed as text, because on the simulated branch it is the evidence for the sentence
 * beside it — the consent screen is on our own origin, which is what lets the browser
 * present the signed `state` cookie on the way back.
 */
function ConnectStarted({ start }: { start: ConnectStart }) {
  const label = platformLabel(start.platform);

  return (
    <SoftWell className="mt-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">
          {start.fake ? `Simulated ${label} authorisation` : `Continue at ${label}`}
        </span>
        {start.fake && <Pill tone="warn">simulated</Pill>}
      </div>

      {start.fake ? (
        <>
          <p className="mt-2 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
            There is no real {label} app behind this yet, so the link below is not{" "}
            {label} — it is a stand-in consent screen served by this application. Nothing
            signs you in at {label} and no real account is connected. Approving it stores a
            credential that is labelled simulated wherever it appears and that nothing can
            be published with.
          </p>
          <p className="mt-2">
            <a
              href={start.authorizationUrl}
              rel="noopener"
              className="text-sm font-medium underline"
              style={{ color: "var(--primary)" }}
            >
              Continue to the simulated consent screen
            </a>
          </p>
          <p
            className="tabular mt-2 overflow-x-auto rounded px-2 py-1 text-xs"
            style={{ background: "var(--surface-sunken)", color: "var(--text-faint)" }}
          >
            {start.authorizationUrl}
          </p>
        </>
      ) : (
        <>
          <p className="mt-2 max-w-[70ch] text-sm" style={{ color: "var(--text-muted)" }}>
            You will be asked to sign in at {label} and approve the permissions below.
            Nothing is stored until you come back.
          </p>
          <p className="mt-2">
            <a
              href={start.authorizationUrl}
              rel="noopener"
              className="text-sm font-medium underline"
              style={{ color: "var(--primary)" }}
            >
              Continue to {label}
            </a>
          </p>
        </>
      )}

      <p className="mt-2 max-w-[70ch] text-xs" style={{ color: "var(--text-faint)" }}>
        Permissions requested: {start.scopes.length > 0 ? start.scopes.join(", ") : "none"}
      </p>
    </SoftWell>
  );
}

/**
 * When a credential expires, as a `<time>`.
 *
 * Absolute and locale-formatted rather than "in 40 minutes": a relative string is wrong
 * the moment it is painted and needs a timer to stay true. `NaN` is handled rather than
 * assumed away — `expiresAt` is a string on the wire and `new Date("nonsense")` renders
 * "Invalid Date" without complaining. Same reasoning, and the same shape, as
 * `run-rows.tsx`'s `RunTime`.
 */
function ExpiryTime({ iso }: { iso: string }) {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return <>at an unrecorded time</>;
  return (
    <time className="tabular" dateTime={iso}>
      {parsed.toLocaleString()}
    </time>
  );
}

/** "a, b and c" — because "a, b, c" reads as a fragment inside a sentence. */
function sentenceList(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}
