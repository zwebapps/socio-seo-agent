"use client";

/**
 * The export pack: Tier 3 (docs/CHANNELS.md §2) as a real surface rather than a copy
 * button.
 *
 * This is the product's actual publishing story — "works on day one, on every platform,
 * forever" — so the screen's job is not to hand over text, it is to hand over text plus
 * everything the person pasting it needs to get it right: the length against this
 * channel's editorial target AND its platform ceiling, the hashtag count against the cap,
 * and whether a link in the body is clickable there at all. That last one is the
 * Instagram and TikTok truth from §1 of the same document: a URL in a feed caption is not
 * a broken link, it is NO link, so a poster who is not told will lose the click and the
 * attribution with it.
 *
 * The rules this screen keeps, and why each one matters more here than anywhere else:
 *
 * - **Nothing is labelled "Publish", "Post" or "Schedule".** Nothing in this product
 *   posts to a platform, and a control that implied otherwise would be the single most
 *   misleading thing on the screen — the owner would believe their content was live. Every
 *   control says what it actually does: "Copy for LinkedIn", "Download the pack (.md)".
 *   `export.test.tsx` asserts it, because this is a rule that gets broken by a
 *   well-meaning rename.
 * - **The counts come from the server and describe the paste.** `pasteCharacters` measures
 *   `pasteText`, the exact string the copy button puts on the clipboard, so what is
 *   measured and what is pasted cannot diverge.
 * - **An empty section names the node that fills it**, exactly as the review tabs do. A
 *   blank panel is indistinguishable from a rendering bug, and "REPACK has not completed"
 *   is a different problem from "the download is broken".
 * - **No tracked short link is invented.** None exists for a run yet, and a
 *   plausible-looking one would be a dead URL in somebody's Instagram bio. The bio-link
 *   hub, which is real, is offered instead with what it does and does not contain stated.
 *
 * The download is a plain `<a href>` to the Markdown rendering, not a scripted blob: it
 * works with JavaScript off, and the filename and the `attachment` disposition come from
 * the server rather than being re-invented here.
 */

import { useCallback, useEffect, useState } from "react";
import { Pill, SoftCard, SoftWell } from "../../components/soft";
import { ApiError } from "../../lib/api";
import {
  channelLabel,
  exportPackMarkdownUrl,
  fetchExportPack,
  type ExportChannel,
  type ExportPack,
} from "../../lib/export-api";
import { CopyButton } from "./copy-button";

export function ExportPanel({ runId, runState }: { runId: string; runState: string }) {
  const [pack, setPack] = useState<ExportPack | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPack(await fetchExportPack(runId));
      setError(null);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Could not load this run's export pack.");
    }
  }, [runId]);

  // Re-read as the run advances, for the same reason the review tabs do: the pack fills
  // up node by node, and one fetched at HARVEST would stay empty for the whole run.
  useEffect(() => {
    void load();
  }, [load, runState]);

  if (error) {
    return (
      <SoftWell className="p-5">
        <p className="text-sm font-semibold" style={{ color: "var(--err)" }}>
          {error}
        </p>
      </SoftWell>
    );
  }

  if (!pack) {
    return (
      <SoftWell className="p-5">
        <p className="text-sm" style={{ color: "var(--text-muted)" }} aria-live="polite">
          Loading the export pack…
        </p>
      </SoftWell>
    );
  }

  return (
    <div className="space-y-5">
      <SoftCard className="p-5" size="md" as="div">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-[16rem] flex-1">
            <h3 className="text-sm font-semibold">Copy and paste, channel by channel</h3>
            {/* The API's own sentence, rendered rather than paraphrased: it is the one
                claim this screen must not soften. */}
            <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
              {pack.notice}
            </p>
          </div>
          <a
            href={exportPackMarkdownUrl(runId)}
            download
            className="soft-press soft-raised soft-edge inline-flex shrink-0 items-center px-4 py-2 text-sm font-medium"
            style={{ borderRadius: "var(--r-pill)", color: "var(--text)" }}
          >
            Download the pack (.md)
          </a>
        </div>
        <p className="mt-3 text-xs" style={{ color: "var(--text-muted)" }}>
          The file holds every channel&rsquo;s copy, the landing page and the answer blocks
          as plain text, so it can go straight into a scheduler or an email to whoever
          posts.
        </p>
      </SoftCard>

      <section aria-labelledby="export-channels">
        <h3 id="export-channels" className="text-sm font-semibold">
          Channels
        </h3>
        {pack.channels.length === 0 ? (
          <div className="mt-3">
            <Missing note={pack.channelsNote} />
          </div>
        ) : (
          <div className="mt-3 space-y-4">
            {pack.channels.map((channel) => (
              <ChannelCard key={channel.channel} channel={channel} />
            ))}
          </div>
        )}
      </section>

      <LandingPageCard pack={pack} />
      <AnswerBlocksCard pack={pack} />
      <LinksCard pack={pack} />

      {/* The placeholder for a capability this tab does not have. Deliberately NOT a
          disabled button: a greyed-out "Publish" would announce a feature by implying it
          is nearly here, and it is a per-platform permission question (App Review), not a
          switch. And deliberately worded as a fact about THIS SCREEN rather than about the
          deployment — "no account is connected" is a claim that goes stale the day one is,
          at which point the reassurance becomes the misleading part. */}
      <SoftWell className="p-4">
        <h3 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
          Publishing straight to a platform
        </h3>
        <p className="mt-1.5 text-sm" style={{ color: "var(--text-muted)" }}>
          Not from this screen. There are two kinds of control on this tab — copy, and
          download — and neither of them reaches a platform. Direct publishing needs each
          platform&rsquo;s own permission, which is weeks of waiting rather than a switch,
          so this pack is the path that works everywhere in the meantime.
        </p>
      </SoftWell>
    </div>
  );
}

/** The one empty state, shared by every section so they cannot drift apart. */
function Missing({ note }: { note: string | null }) {
  return (
    <SoftWell className="p-5">
      <p className="text-sm" style={{ color: "var(--text-muted)" }}>
        {note ?? "There is nothing here yet."}
      </p>
    </SoftWell>
  );
}

/* ------------------------------------------------------------------------- */
/* One channel: the copy, and what it will cost to post it                    */
/* ------------------------------------------------------------------------- */

function ChannelCard({ channel }: { channel: ExportChannel }) {
  const label = channelLabel(channel.channel);

  return (
    <SoftCard className="p-5" size="md" as="article">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h4 className="text-sm font-semibold">{label}</h4>
        {/* "Copy for LinkedIn", never "Post to LinkedIn": this button puts text on the
            clipboard and that is all it does. */}
        <CopyButton
          text={channel.pasteText}
          label={`Copy the ${label} text`}
          caption={`Copy for ${label}`}
        />
      </div>

      <Measurements channel={channel} />

      <SoftWell className="mt-3 p-4">
        {/* The exact string the copy button carries, shown as characters and never as
            markup. Pasting is the whole deliverable, so what is on screen has to be it. */}
        <p className="text-sm leading-relaxed" style={{ whiteSpace: "pre-wrap" }}>
          {channel.pasteText}
        </p>
      </SoftWell>

      {channel.notes.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {channel.notes.map((note) => (
            <li key={note} className="text-xs" style={{ color: "var(--warn)" }}>
              {note}
            </li>
          ))}
        </ul>
      )}
    </SoftCard>
  );
}

/**
 * The numbers, each against the thing it has to fit inside.
 *
 * A bare "1,842 characters" answers nothing, so every figure carries its comparison. The
 * over-target and over-limit states are distinguished in WORDS as well as colour — one is
 * publishable and merely long, the other is refused by the platform, and a reader who
 * cannot tell two shades of orange apart still has to know which they have.
 */
function Measurements({ channel }: { channel: ExportChannel }) {
  const lengthTone = channel.overLimit
    ? "var(--err)"
    : channel.overTarget
      ? "var(--warn)"
      : "var(--text-muted)";

  const hashtagsOverCap =
    channel.hashtagLimit !== null && channel.hashtagCount > channel.hashtagLimit;

  return (
    <dl className="mt-2.5 flex flex-wrap gap-x-5 gap-y-1.5 text-[11px]">
      <div className="flex flex-wrap items-baseline gap-1.5">
        <dt style={{ color: "var(--text-faint)" }}>length</dt>
        <dd className="tabular font-semibold" style={{ color: lengthTone }}>
          {channel.pasteCharacters.toLocaleString()} characters
          <span style={{ color: "var(--text-faint)" }}>
            {channel.characterTarget !== null &&
              ` · target ${channel.characterTarget.toLocaleString()}`}
            {channel.characterLimit !== null &&
              ` · platform limit ${channel.characterLimit.toLocaleString()}`}
            {channel.characterTarget === null && " · no target published for this channel"}
          </span>
          {channel.overLimit && (
            <span style={{ color: "var(--err)" }}> · over the platform limit</span>
          )}
          {!channel.overLimit && channel.overTarget && (
            <span style={{ color: "var(--warn)" }}> · over target, still publishable</span>
          )}
        </dd>
      </div>

      <div className="flex flex-wrap items-baseline gap-1.5">
        <dt style={{ color: "var(--text-faint)" }}>hashtags</dt>
        <dd
          className="tabular font-semibold"
          style={{ color: hashtagsOverCap ? "var(--err)" : "var(--text-muted)" }}
        >
          {channel.hashtagCount}
          <span style={{ color: "var(--text-faint)" }}>
            {channel.hashtagLimit !== null
              ? ` · at most ${channel.hashtagLimit}`
              : " · no cap published for this channel"}
            {channel.hashtagMinimum ? ` · at least ${channel.hashtagMinimum}` : ""}
          </span>
        </dd>
      </div>

      <div className="flex flex-wrap items-baseline gap-1.5">
        <dt style={{ color: "var(--text-faint)" }}>link</dt>
        <dd className="font-semibold" style={{ color: "var(--text-muted)" }}>
          {channel.linkMechanism === "inline" && "clickable in the body"}
          {channel.linkMechanism === "bio_hub" && (
            <span style={{ color: "var(--warn)" }}>not clickable in the body — use the bio hub</span>
          )}
          {channel.linkMechanism === "unknown" && "unknown for this channel"}
        </dd>
      </div>
    </dl>
  );
}

/* ------------------------------------------------------------------------- */
/* The rest of the pack                                                       */
/* ------------------------------------------------------------------------- */

function LandingPageCard({ pack }: { pack: ExportPack }) {
  const page = pack.landingPage;

  return (
    <section aria-labelledby="export-landing">
      <h3 id="export-landing" className="text-sm font-semibold">
        Landing page
      </h3>
      {page === null ? (
        <div className="mt-3">
          <Missing note={pack.landingPageNote} />
        </div>
      ) : (
        <SoftCard className="mt-3 p-5" size="md" as="div">
          <p className="text-base font-semibold">{page.headline}</p>
          {page.subhead && (
            <p className="mt-1 text-sm" style={{ color: "var(--text-muted)" }}>
              {page.subhead}
            </p>
          )}
          <dl className="mt-3 space-y-2 text-sm">
            <div>
              <dt className="text-[11px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
                offer
              </dt>
              <dd>{page.offer}</dd>
            </div>
            <div>
              <dt className="text-[11px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
                button
              </dt>
              <dd>{page.primaryCta}</dd>
            </div>
            {page.consentText && (
              <div>
                <dt className="text-[11px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
                  consent line
                </dt>
                <dd>{page.consentText}</dd>
              </div>
            )}
          </dl>

          {page.proofPoints.length > 0 && (
            <div className="mt-4">
              <h4 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
                Proof points, with their sources
              </h4>
              {/* The source is rendered beside every claim, never dropped: an unsourced
                  proof point is an invented statement about the customer's business, and
                  this is a page they will publish under their own name. */}
              <ul className="mt-2 space-y-1.5 text-sm">
                {page.proofPoints.map((point) => (
                  <li key={point.text}>
                    {point.text}{" "}
                    <span style={{ color: "var(--text-muted)" }}>— source: {point.source}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {page.channelCtas.length > 0 && (
            <div className="mt-4">
              <h4 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
                The ask, per channel
              </h4>
              <ul className="mt-2 space-y-1.5 text-sm">
                {page.channelCtas.map((cta) => (
                  <li key={cta.channel}>
                    <span style={{ color: "var(--text-muted)" }}>
                      {channelLabel(cta.channel)}:{" "}
                    </span>
                    {cta.text}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </SoftCard>
      )}
    </section>
  );
}

function AnswerBlocksCard({ pack }: { pack: ExportPack }) {
  const blocks = pack.aiBlocks;
  const empty = blocks === null || blocks.blocks.length === 0;

  return (
    <section aria-labelledby="export-blocks">
      <h3 id="export-blocks" className="text-sm font-semibold">
        Answer blocks for AI engines
      </h3>
      {empty ? (
        <div className="mt-3">
          <Missing note={pack.aiBlocksNote} />
        </div>
      ) : (
        <SoftCard className="mt-3 p-5" size="md" as="div">
          {blocks.targetKeyword && (
            <p className="mb-3">
              <Pill tone="accent">{blocks.targetKeyword}</Pill>
            </p>
          )}
          <ol className="space-y-2 text-sm">
            {blocks.blocks.map((block) => (
              <li key={block}>{block}</li>
            ))}
          </ol>
          <p className="mt-3 text-xs" style={{ color: "var(--text-muted)" }}>
            Included in the download, and quotable as they stand — each one has to make
            sense when it is the only sentence someone sees.
          </p>
        </SoftCard>
      )}
    </section>
  );
}

function LinksCard({ pack }: { pack: ExportPack }) {
  return (
    <section aria-labelledby="export-links">
      <h3 id="export-links" className="text-sm font-semibold">
        Where the clicks go
      </h3>
      <SoftCard className="mt-3 p-5" size="md" as="div">
        {pack.hubUrl && (
          <p className="text-sm">
            <span className="text-[11px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
              bio-link hub{" "}
            </span>
            <a
              href={pack.hubUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="underline"
              style={{ color: "var(--text)" }}
            >
              {pack.hubUrl}
            </a>
          </p>
        )}
        {pack.hubNote && (
          <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
            {pack.hubNote}
          </p>
        )}
        {/* The absence, stated. A short link that does not exist must never be rendered as
            though it did — it would 404 from somebody's Instagram bio, and the failure
            would be invisible until the leads did not arrive. */}
        <p className="mt-3 text-sm" style={{ color: "var(--warn)" }}>
          {pack.trackedLinkNote}
        </p>
      </SoftCard>
    </section>
  );
}
