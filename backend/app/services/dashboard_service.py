"""The owner's dashboard numbers, and the rule about which numbers exist.

Every figure here is read from a table this product writes. That constraint is the
module's whole design, because the obvious dashboard — visitors, conversions, revenue,
ROI, impressions, brand sentiment — is a dashboard of things we cannot see:

* **Visitors and impressions** need the platform's own analytics, which need the same
  App Review that publishing needs, or GSC/GA4, which ``CLAUDE.md`` records as
  deliberately cut ("two OAuth flows for a metric that cannot move inside a project
  timeline").
* **Revenue and ROI** need money, and this product touches none.
* **Ad spend** is ruled out permanently: ``docs/ROADMAP.md`` — "Explicitly not
  building, ever: paid-ads spend automation".
* **Conversions** stopped being ours to count when the founder ruled that we host no
  landing page. We measure the CLICK; the form on the customer's own site is theirs.

So the tiles are: tracked clicks (ours, because the short link is ours), runs and what
they reached, leads already captured, model spend, and — from the latest run — the SEO
problems found on the customer's own site and the AI share-of-voice sample.

**A metric with no data reads as `None`, never as zero.** "0 clicks" is a measurement
and "we have not measured this yet" is not, and a dashboard that renders the second as
the first is the exact failure the rest of this codebase is careful about: a run on the
fake provider reporting a score, a NAP audit of zero listings reporting consistency.
The API returns null and the screen says so in words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ChannelClicks",
    "DashboardSummary",
    "read_dashboard",
]


@dataclass(frozen=True, slots=True)
class ChannelClicks:
    """Clicks earned by one channel.

    ``channel`` is nullable in ``short_links`` -- the hub link carries no channel -- and
    a null becomes ``"link_hub"`` here rather than being dropped, because the hub is the
    ENTIRE conversion path for Instagram and TikTok and losing it would understate
    exactly the channels that have no other route.
    """

    channel: str
    clicks: int


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    """What the owner's dashboard can honestly show."""

    #: Total clicks across every tracked link. `None` when no link has ever been minted,
    #: which is different from "no clicks yet".
    clicks_total: int | None
    clicks_by_channel: tuple[ChannelClicks, ...] = ()
    #: Bot clicks, reported separately rather than silently included or silently
    #: dropped. `link_clicks.is_bot` exists precisely so the number can be honest.
    clicks_from_bots: int = 0

    runs_total: int = 0
    runs_awaiting_approval: int = 0
    #: Runs that ended short of the end. Named because a dashboard showing only a total
    #: would present "12 runs" while most of them stopped at the SEO gate.
    runs_partial: int = 0

    leads_total: int = 0

    #: Model spend across every run. `None` when no model call has ever been recorded,
    #: which on a fake-provider deployment is the ordinary state.
    spend_usd: Decimal | None = None

    #: Problems the audit found on the customer's OWN site, from the latest run that
    #: audited it. `None` when no run has audited one.
    seo_problems: int | None = None
    seo_pages_audited: int | None = None
    #: True when that audit hit its page cap, so "no duplicate titles" is a sample
    #: rather than a finding.
    seo_truncated: bool = False

    #: The AI share-of-voice sample from the latest run that probed. `None` when none
    #: has. A SAMPLE, never a census -- `CLAUDE.md` is explicit that it must not be
    #: described as one.
    share_of_voice: float | None = None

    #: Anything the dashboard cannot show and why, in the owner's words.
    gaps: tuple[str, ...] = field(default=())


#: Kept in one place so the wording is identical wherever it surfaces.
_NO_ANALYTICS_GAP = (
    "Visitors, impressions and audience breakdowns are not shown: those come from each "
    "platform's own analytics, which need the same App Review that publishing needs. "
    "What is measured here is the click on a link we own."
)


async def read_dashboard(business_id: UUID, *, session: AsyncSession) -> DashboardSummary:
    """One read per tile, all scoped by RLS through the caller's session.

    Raw SQL rather than the ORM, and for one reason: these are six aggregates over five
    tables, and expressing them as ORM queries would fetch rows in order to count them.
    Every statement is parameterised; ``business_id`` is bound, never formatted in.
    """
    gaps: list[str] = [_NO_ANALYTICS_GAP]

    # Clicks come from `short_links.click_count`, the denormalised counter whose own
    # comment says it exists "so a list view needs no aggregate over link_clicks" --
    # this is that list view. `link_clicks` is only touched for the bot split.
    links = (
        await session.execute(
            text(
                """
                SELECT coalesce(channel, 'link_hub') AS channel,
                       sum(click_count)::bigint AS clicks
                FROM short_links
                GROUP BY coalesce(channel, 'link_hub')
                ORDER BY clicks DESC, channel ASC
                """
            )
        )
    ).all()

    by_channel = tuple(
        ChannelClicks(channel=row.channel, clicks=int(row.clicks or 0)) for row in links
    )
    # `None` when no link exists at all; 0 when links exist and nobody clicked. The
    # distinction is the point: the second is a measurement, the first is not.
    clicks_total = sum(c.clicks for c in by_channel) if by_channel else None
    if by_channel and clicks_total == 0:
        gaps.append(
            "No clicks recorded yet. Tracked links only earn clicks once the posts "
            "carrying them are published or pasted."
        )

    bots = (
        await session.execute(
            text("SELECT count(*)::bigint AS n FROM link_clicks WHERE is_bot = true")
        )
    ).scalar_one()

    runs = (
        await session.execute(
            text(
                """
                SELECT count(*)::bigint AS total,
                       count(*) FILTER (WHERE state = 'awaiting_approval')::bigint AS awaiting,
                       count(*) FILTER (WHERE state = 'partial')::bigint AS partial
                FROM runs
                """
            )
        )
    ).one()

    leads = (await session.execute(text("SELECT count(*)::bigint AS n FROM leads"))).scalar_one()

    spend = (await session.execute(text("SELECT sum(usd) AS total FROM model_usage"))).scalar_one()

    audit, sov, sov_gap = await _from_latest_checkpoint(session)

    if audit is None:
        gaps.append(
            "Your site has not been audited yet. The audit runs as part of a run, so "
            "start one to see what it finds."
        )
    if sov_gap is not None:
        gaps.append(sov_gap)

    return DashboardSummary(
        clicks_total=clicks_total,
        clicks_by_channel=by_channel,
        clicks_from_bots=int(bots or 0),
        runs_total=int(runs.total or 0),
        runs_awaiting_approval=int(runs.awaiting or 0),
        runs_partial=int(runs.partial or 0),
        leads_total=int(leads or 0),
        # `sum()` over no rows is NULL, which is the honest "never measured" rather
        # than a zero anybody would read as "this cost nothing".
        spend_usd=Decimal(str(spend)) if spend is not None else None,
        seo_problems=None if audit is None else int(audit.get("problem_count") or 0),
        seo_pages_audited=None if audit is None else int(audit.get("pages_crawled") or 0),
        seo_truncated=bool(audit.get("truncated")) if audit is not None else False,
        share_of_voice=sov,
        gaps=tuple(gaps),
    )


#: Said when no run has produced a share of voice at all.
_NO_SOV_GAP = (
    "No AI share-of-voice sample yet. It is a sample of what a few models say "
    "about your business, never a census."
)

#: Said when a run DID produce one but against the deterministic fake provider. The
#: number exists and is still not shown, because a share of voice measured against
#: canned answers measures us and not the market -- the same rule that makes a missing
#: credential a fake provider PLUS a status rather than a quiet paid call.
_FAKE_SOV_GAP = (
    "No AI share-of-voice sample yet: the last run that probed used the built-in fake "
    "model provider, so its answers are canned and the share they produce is not a "
    "measurement of anything. Configure a real provider to get one."
)


async def _from_latest_checkpoint(
    session: AsyncSession,
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    """The SEO audit and share-of-voice from the most recent run that produced them.

    Read from the newest run that HAS each one rather than from the newest run outright:
    a run that stopped at INTAKE has neither, and letting it blank the dashboard would
    make the numbers vanish every time a run failed early. So each is taken from the
    last run that actually measured it, which is what "your latest audit" means to the
    person reading it.

    That is also why the WHERE clause names the three JSON paths instead of testing the
    column for NULL: ``runs.checkpoint`` is ``NOT NULL DEFAULT '{}'``, so
    ``checkpoint IS NOT NULL`` matched every row ever written and the ``LIMIT`` could be
    filled entirely by queued runs -- the newest audit would then be invisible on a
    business whose last 25 runs never got past INTAKE, which is the exact failure the
    paragraph above claims to prevent.

    The checkpoint is JSONB written by our own code, but a reader does not get to assume
    its own version wrote it, so every access is defensive and a malformed shape reads
    as absent rather than raising on the dashboard.

    Returns the audit payload, the share of voice, and the gap sentence to print when
    there is no share to print -- the caller cannot derive that sentence from a bare
    ``None``, because "nothing probed" and "probed against a fake" are different facts.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT checkpoint
                FROM runs
                WHERE checkpoint #> '{facts,site,seo_audit}' IS NOT NULL
                   OR checkpoint #> '{facts,visibility}' IS NOT NULL
                   OR checkpoint #> '{measurement,share_of_voice}' IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 25
                """
            )
        )
    ).all()

    audit: dict[str, Any] | None = None
    sov: float | None = None
    #: Whether any run in the window probed with the fake provider. Remembered rather
    #: than returned immediately, because an older REAL sample beats a newer canned one
    #: and the reader deserves the specific reason when there is no real one at all.
    saw_fake = False

    for row in rows:
        checkpoint = row.checkpoint if isinstance(row.checkpoint, dict) else {}
        facts = checkpoint.get("facts")
        facts = facts if isinstance(facts, dict) else {}

        if audit is None:
            site = facts.get("site")
            candidate = site.get("seo_audit") if isinstance(site, dict) else None
            if isinstance(candidate, dict):
                audit = candidate

        if sov is None:
            sov, gap = _sov_from(checkpoint, facts)
            saw_fake = saw_fake or gap is _FAKE_SOV_GAP

        if audit is not None and sov is not None:
            break

    if sov is not None:
        return audit, sov, None
    return audit, None, _FAKE_SOV_GAP if saw_fake else _NO_SOV_GAP


def _sov_from(checkpoint: dict[str, Any], facts: dict[str, Any]) -> tuple[float | None, str | None]:
    """One run's share of voice, read from wherever that run recorded it.

    **The figure is ``mention_share_pct``, and there is no ``share_of_voice`` key to
    read.** This function replaces a lookup of ``facts.visibility.share_of_voice``,
    which nothing has ever written: HARVEST stores the probe dict returned by
    ``run_executor._build_geo_probe`` under ``facts.visibility`` and its percentage
    field is ``mention_share_pct``, while MEASURE stores
    ``measurement.share_of_voice.baseline``, a ``_sov_view`` of the same probe. The old
    path therefore read ``None`` on every checkpoint ever written and the dashboard
    reported "no sample yet" for a business that had one -- a metric silently pinned to
    absent, which is the same class of lie as reporting a zero.

    ``measurement`` is preferred over ``facts`` because MEASURE carries HARVEST's
    baseline forward unchanged, so it is the run's own final word on the number.

    A share is refused, with the reason named, when the probe ran on the fake provider
    or when nothing usable came back -- ``mention_share_pct`` is already ``None`` in the
    second case, because ``no_answer`` is excluded from the denominator and a share of
    zero usable answers is not zero.
    """
    baseline: object = None
    measurement = checkpoint.get("measurement")
    if isinstance(measurement, dict):
        recorded = measurement.get("share_of_voice")
        if isinstance(recorded, dict):
            baseline = recorded.get("baseline")
    if not isinstance(baseline, dict):
        baseline = facts.get("visibility")
    if not isinstance(baseline, dict):
        return None, _NO_SOV_GAP

    if baseline.get("using_fake_provider"):
        return None, _FAKE_SOV_GAP

    raw = baseline.get("mention_share_pct")
    # `bool` is an `int`, and a stray `True` here would render as 100%.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, _NO_SOV_GAP
    return float(raw), None
