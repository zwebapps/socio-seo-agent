/**
 * Typed client for the platform connections API.
 *
 * Mirrors `backend/app/api/connections.py`. Three things about this file are
 * load-bearing, and each of them is a rule about what the client is NOT allowed to do.
 *
 * **Usability is read, never computed.** `usable` and `unusableReason` come off the wire
 * exactly as the server derived them, from `ConnectionView.unusable_reason` — the same
 * function `actuators/social.py` asks before it refuses to publish. So there is no
 * expiry arithmetic in this file and no status interpretation beyond choosing a colour
 * for a verdict the server already reached. A client that recomputed it would eventually
 * disagree with the publish path, and the owner would read a green account while their
 * posts went nowhere. `connectionTone` and `connectionVerdict` below are the whole of
 * what this file is allowed to decide, and both are functions of the server's own
 * booleans.
 *
 * **There is no field here that could hold a credential, and that is by construction.**
 * `ConnectionOut` is projected from `ConnectionView`, which has none either;
 * `credentialHint` is `mask_secret`'s four-and-four form and is the most any surface
 * ever sees. `hasCredential` is a boolean about existence — rendering it as though it
 * were the secret is the mistake this comment exists to prevent.
 *
 * **Every call here runs in the browser.** The session cookie needs an `Origin` header
 * and the API's CSRF guard refuses a cookie-bearing write that arrives without one,
 * which is exactly what `fetch` from a server component sends. Same warning as
 * `documents-api.ts` and `runs-api.ts`, and the same reason it is there.
 */

import { request } from "./api";

/** One connected (or formerly connected) platform account. */
export type Connection = {
  platform: string;
  externalAccountId: string;
  externalAccountName: string | null;
  /** What was granted, which is not always what was asked for. */
  scopes: string[];
  /**
   * `connected` · `expired` · `revoked`.
   *
   * A `string` and not a union, for `documents-api.ts`'s reason: the server owns this
   * vocabulary, and a union would make the compiler assert something false the day it
   * grows a value.
   */
  status: string;
  expiresAt: string | null;
  /** Four leading and four trailing characters of the credential. Never more. */
  credentialHint: string;
  /** Which cipher wrote the envelope — `v1.ephemeral` is not `v1.aesgcm`. */
  credentialScheme: string;
  /** Whether a credential is stored at all. False after a revoke, which wipes it. */
  hasCredential: boolean;
  /** True when the grant came from `FakeOAuthProvider` rather than from the platform. */
  fake: boolean;
  /** The server's verdict. Never recomputed here — see the module note. */
  usable: boolean;
  /** The server's sentence for why not, in the words the publish refusal would use. */
  unusableReason: string | null;
  /** Expired, or close enough that publishing on it is a race. */
  needsRenewal: boolean;
};

/** What connecting a platform would actually do right now. */
export type OAuthStatus = {
  /** Every platform a connection row may name. The list the screen iterates. */
  platforms: string[];
  /** Platforms with a real adapter behind them. Empty today. */
  realProviders: string[];
  usingFakeProviders: boolean;
  /**
   * Platforms whose publish permission is somebody else's approval queue.
   *
   * Named by the server so a screen can say so rather than implying the delay is ours.
   */
  blockedOnAppReview: string[];
  /** The server's own sentence. Rendered verbatim, never summarised. */
  message: string;
};

/** What protection a stored credential would actually get, in that process. */
export type CredentialStorage = {
  scheme: string;
  protectsAtRest: boolean;
  /** False means connecting is refused before anybody is sent to a platform. */
  canStoreCredentials: boolean;
  message: string;
};

export type ConnectionList = {
  connections: Connection[];
  oauth: OAuthStatus;
  credentialStorage: CredentialStorage;
};

/** Where to send the human, and what was asked for. */
export type ConnectStart = {
  platform: string;
  authorizationUrl: string;
  scopes: string[];
  /**
   * True when the URL points at `fake-oauth.invalid` rather than at a real platform.
   *
   * The screen must key on this rather than on the URL's shape: a simulated
   * authorisation that is offered as a working one is the single most misleading thing
   * this feature could render.
   */
  fake: boolean;
};

export function fetchConnections(): Promise<ConnectionList> {
  return request<ConnectionList>("/api/v1/connections");
}

/**
 * Start one connect. Returns where to send the human; sends nobody anywhere.
 *
 * `POST` because it has an effect — it writes the signed `state` cookie that will
 * validate the callback — and because being a state-changing method puts it under the
 * API's origin check.
 */
export function startConnect(platform: string): Promise<ConnectStart> {
  return request<ConnectStart>(
    `/api/v1/connections/${encodeURIComponent(platform)}/connect`,
    { method: "POST" },
  );
}

/** Disconnect one platform. 204 whether or not there was anything to disconnect. */
export function disconnectPlatform(platform: string): Promise<void> {
  return request<void>(`/api/v1/connections/${encodeURIComponent(platform)}`, {
    method: "DELETE",
  });
}

/**
 * How a platform is named on screen.
 *
 * Deliberately NOT `export-api.ts`'s `channelLabel`, and the difference is not cosmetic:
 * a channel is a content format we render copy for, a platform is an account we hold a
 * credential for, and the two lists are not the same list. `channelLabel` has no
 * `tiktok`, `youtube` or `google_business` — all three connectable — and carries
 * `blog_article`, `link_hub` and `landing_page`, none of which can be connected. Sharing
 * one map would mean either lying about what is connectable or teaching the export panel
 * about accounts.
 */
const PLATFORM_LABEL: Record<string, string> = {
  facebook: "Facebook",
  instagram: "Instagram",
  linkedin: "LinkedIn",
  tiktok: "TikTok",
  youtube: "YouTube",
  google_business: "Google Business Profile",
};

/**
 * The platform's display name, or the stored key unchanged.
 *
 * Unknown platforms fall through as themselves rather than being prettified: the stored
 * key is what the connection row and the publish path name, and a guessed title case
 * would make the screen and the data disagree about which account this is.
 */
export function platformLabel(platform: string): string {
  return PLATFORM_LABEL[platform] ?? platform;
}

/** A row on the screen: one connectable platform, and its connection if it has one. */
export type PlatformRow = {
  platform: string;
  connection: Connection | null;
};

/**
 * One row per connectable platform, in the server's order, plus any orphan.
 *
 * Iterating `connections` alone would show only what is already connected, which is the
 * opposite of what this screen is for. Iterating `oauth.platforms` alone would silently
 * drop a stored connection for a platform that has since left the connectable list — and
 * that row is precisely the one an owner still needs to be able to disconnect, because it
 * holds a credential and a live grant on their account. So: every connectable platform,
 * then anything held that is no longer among them.
 */
export function platformRows(list: ConnectionList): PlatformRow[] {
  const byPlatform = new Map(list.connections.map((c) => [c.platform, c]));
  const rows: PlatformRow[] = list.oauth.platforms.map((platform) => ({
    platform,
    connection: byPlatform.get(platform) ?? null,
  }));
  const known = new Set(list.oauth.platforms);
  for (const connection of list.connections) {
    if (!known.has(connection.platform)) rows.push({ platform: connection.platform, connection });
  }
  return rows;
}

/**
 * The pill colour for a connection — a function of the server's verdict, nothing else.
 *
 * No clock is read and no status string is interpreted here. `usable` is the same
 * boolean the publish actuator acts on, and `needsRenewal` is the server's own
 * "expiring, publishing on it is a race" flag, so this cannot drift from either.
 */
export function connectionTone(connection: Connection): "ok" | "warn" | "err" {
  if (!connection.usable) return "err";
  if (connection.needsRenewal) return "warn";
  return "ok";
}

/**
 * The pill's TEXT, which carries the same information as its colour.
 *
 * Colour alone is not an accessible signal (SC 1.4.1), and on this screen the signal is
 * the whole point: "we will not publish on this" has to survive being read in greyscale.
 */
export function connectionVerdict(connection: Connection): string {
  if (!connection.usable) return "not usable";
  if (connection.needsRenewal) return "expiring";
  return "ready to publish";
}
