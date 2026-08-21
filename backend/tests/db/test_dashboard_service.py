"""The dashboard's aggregates, against real rows and real row-level security.

The reason this file exists rather than a mocked-session test: every statement in
``dashboard_service`` is raw SQL with NO ``business_id`` predicate, because the
predicate is the database's job -- ``business_session`` sets the GUC the policies key
off. A fake session can prove the projection and cannot prove either half of that, so a
regression that made the read cross-tenant, or that made it return nothing at all, would
be invisible to it.

What is asserted, and the failure each one catches:

* **no links at all reads as `None`, links with no clicks read as `0`.** Both look like
  "nothing happened"; only the second is a measurement, and a dashboard that prints
  "0 clicks" to a business that has never had a tracked link is stating a fact it does
  not have;
* **a NULL channel becomes `link_hub` rather than being dropped.** `GROUP BY channel`
  would silently discard the hub link, which is the ENTIRE conversion path for Instagram
  and TikTok -- so the two channels with no other route would read as zero;
* **bot clicks are counted separately and are not in the click total.** `click_count` is
  incremented only for humans (see `db/adapters/lead_store.py`), so a total taken from
  `link_clicks` instead would inflate every figure by the link previewers;
* **share of voice is read from the key that is actually written.** It is
  `mention_share_pct`, under `facts.visibility` or `measurement.share_of_voice.baseline`;
  the earlier `facts.visibility.share_of_voice` matched nothing ever written, so the
  metric was pinned to "not measured" on every checkpoint in existence;
* **another business sees none of it.** The isolation is the database's, so this is also
  a test that the service is called on a scoped session.

Marked ``db`` and skipped when Postgres is unreachable, per the conftest note.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.db.session import business_session
from backend.app.services.dashboard_service import DashboardSummary, read_dashboard

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the app's session factory at THIS test's engine.

    Autouse because every test here reads through ``business_session``, which reaches the
    database via the process-wide factory. An asyncpg pool belongs to the event loop that
    created it and pytest-asyncio gives each test a fresh loop, so a shared factory fails
    the second test in the file with an error that looks like a database problem.
    """
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


async def _dashboard(business_id: UUID) -> DashboardSummary:
    """Read as the restricted role, inside the tenant-scoped transaction."""
    async with business_session(business_id) as session:
        return await read_dashboard(business_id, session=session)


async def _seed_link(
    session: AsyncSession,
    business_id: UUID,
    *,
    channel: str | None,
    clicks: int = 0,
) -> UUID:
    link_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO short_links (id, business_id, code, target_url, channel, "
            "click_count) VALUES (:id, :b, :code, 'https://example.test/x', :ch, :n)"
        ),
        {
            "id": link_id,
            "b": business_id,
            # 16 chars is the column's width, and `code` is globally unique.
            "code": str(link_id)[:12],
            "ch": channel,
            "n": clicks,
        },
    )
    return link_id


async def _seed_click(
    session: AsyncSession, business_id: UUID, link_id: UUID, *, is_bot: bool
) -> None:
    await session.execute(
        text(
            "INSERT INTO link_clicks (id, business_id, short_link_id, is_bot) "
            "VALUES (:id, :b, :l, :bot)"
        ),
        {"id": uuid4(), "b": business_id, "l": link_id, "bot": is_bot},
    )


async def _seed_run(
    session: AsyncSession,
    business_id: UUID,
    *,
    state: str = "done",
    checkpoint: str = "{}",
    created_at: datetime | None = None,
) -> UUID:
    run_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO runs (id, business_id, goal, state, checkpoint, created_at) "
            "VALUES (:id, :b, 'fixture goal', :state, (:cp)::jsonb, :at)"
        ),
        {
            "id": run_id,
            "b": business_id,
            "state": state,
            "cp": checkpoint,
            "at": created_at or datetime.now(UTC),
        },
    )
    return run_id


async def _seed_lead(session: AsyncSession, business_id: UUID) -> None:
    await session.execute(
        text("INSERT INTO leads (id, business_id, source) VALUES (:id, :b, 'form')"),
        {"id": uuid4(), "b": business_id},
    )


async def _seed_usage(session: AsyncSession, business_id: UUID, *, usd: str) -> None:
    await session.execute(
        text(
            "INSERT INTO model_usage (id, business_id, node, provider, model, tokens_in, "
            "tokens_out, usd, latency_ms) VALUES (:id, :b, 'GENERATE', 'openrouter', "
            "'openai/gpt-4.1', 10, 20, :usd, 5)"
        ),
        {"id": uuid4(), "b": business_id, "usd": Decimal(usd)},
    )


def _checkpoint(
    *,
    problems: int = 3,
    pages: int = 4,
    truncated: bool = False,
    sov: float | None = 22.5,
    fake_provider: bool = False,
) -> dict[str, Any]:
    """A checkpoint shaped like the one the graph actually writes.

    The paths are the real ones: ``run_executor.summarise_crawl`` puts the audit under
    ``facts.site.seo_audit`` and ``_build_geo_probe``'s result under
    ``facts.visibility``, whose percentage field is ``mention_share_pct``.
    """
    visibility: dict[str, Any] = {
        "headline": "mentioned in 9 of 41 usable answers",
        "mention_share_pct": sov,
        "usable_answers": 41 if sov is not None else 0,
        "no_answer_count": 2,
        "using_fake_provider": fake_provider,
        "caveats": [],
    }
    return {
        "facts": {
            "site": {
                "start_url": "https://example.test",
                "seo_audit": {
                    "start_url": "https://example.test",
                    "pages_crawled": pages,
                    "pages_audited": pages,
                    "truncated": truncated,
                    "problem_count": problems,
                    "worst_severity": "warn",
                },
            },
            "visibility": visibility,
        }
    }


# --------------------------------------------------------------------------- #
# Clicks: the null-vs-zero distinction, the hub, and the bot split
# --------------------------------------------------------------------------- #


async def test_a_business_with_no_links_reports_clicks_as_unmeasured_not_as_zero(
    business_a: UUID,
) -> None:
    """No link has ever been minted, so there is nothing that COULD have been clicked.
    `0` here would be the product reporting a measurement it never took."""
    summary = await _dashboard(business_a)

    assert summary.clicks_total is None
    assert summary.clicks_by_channel == ()
    assert summary.clicks_from_bots == 0


async def test_links_with_no_clicks_report_zero_which_is_a_real_measurement(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """The other side of the same coin: the links exist and nobody has clicked them. That
    is a fact, and `None` would hide a working link that nothing has reached."""
    await _seed_link(owner_session, business_a, channel="linkedin", clicks=0)
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.clicks_total == 0
    assert [(row.channel, row.clicks) for row in summary.clicks_by_channel] == [("linkedin", 0)]
    assert any("No clicks recorded yet" in gap for gap in summary.gaps), (
        "zero clicks with links present must be explained, or the screen reads as broken"
    )


async def test_clicks_group_by_channel_and_a_null_channel_becomes_the_link_hub(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """The hub link carries no channel. `GROUP BY channel` would drop it, and with it the
    only conversion path Instagram and TikTok have."""
    await _seed_link(owner_session, business_a, channel="linkedin", clicks=5)
    await _seed_link(owner_session, business_a, channel="facebook", clicks=2)
    await _seed_link(owner_session, business_a, channel=None, clicks=8)
    await _seed_link(owner_session, business_a, channel=None, clicks=1)
    await owner_session.commit()

    summary = await _dashboard(business_a)

    by_channel = {row.channel: row.clicks for row in summary.clicks_by_channel}
    assert by_channel == {"link_hub": 9, "linkedin": 5, "facebook": 2}, (
        "the two channel-less links must sum into one link_hub row, not vanish"
    )
    assert summary.clicks_total == 16
    # Ordered by clicks so the screen can render the table as it arrives.
    assert [row.channel for row in summary.clicks_by_channel] == [
        "link_hub",
        "linkedin",
        "facebook",
    ]


async def test_bot_clicks_are_reported_separately_and_are_not_in_the_total(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """`short_links.click_count` counts humans only -- a link previewer is not a person --
    so the bot rows must come from `link_clicks` and must stay out of the total. A total
    taken from `link_clicks` instead would report 5 clicks for 2 people."""
    link = await _seed_link(owner_session, business_a, channel="linkedin", clicks=2)
    for is_bot in (False, False, True, True, True):
        await _seed_click(owner_session, business_a, link, is_bot=is_bot)
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.clicks_total == 2
    assert summary.clicks_from_bots == 3


# --------------------------------------------------------------------------- #
# Runs, leads, spend
# --------------------------------------------------------------------------- #


async def test_runs_are_counted_by_state_and_leads_and_spend_are_summed(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """A total alone would present "4 runs" while two of them stopped short."""
    await _seed_run(owner_session, business_a, state="done")
    await _seed_run(owner_session, business_a, state="partial")
    await _seed_run(owner_session, business_a, state="partial")
    await _seed_run(owner_session, business_a, state="awaiting_approval")
    await _seed_lead(owner_session, business_a)
    await _seed_lead(owner_session, business_a)
    await _seed_usage(owner_session, business_a, usd="0.10000000")
    await _seed_usage(owner_session, business_a, usd="0.20000000")
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert (summary.runs_total, summary.runs_partial, summary.runs_awaiting_approval) == (4, 2, 1)
    assert summary.leads_total == 2
    assert summary.spend_usd == Decimal("0.3"), (
        f"spend came back {summary.spend_usd!r}; a float in the path gives 0.30000000000000004"
    )
    assert isinstance(summary.spend_usd, Decimal), "money is Decimal from the database out"


async def test_an_unwritten_ledger_reads_as_unrecorded_rather_than_as_zero_dollars(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """`SUM` over no rows is SQL NULL. `$0.00` would be a false statement about spend, and
    on a fake-provider deployment it would be the permanent one."""
    await _seed_run(owner_session, business_a)
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.runs_total == 1
    assert summary.spend_usd is None


# --------------------------------------------------------------------------- #
# The checkpoint reads
# --------------------------------------------------------------------------- #


async def test_the_seo_audit_and_share_of_voice_come_off_a_real_checkpoint(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """The regression this file was written for.

    `share_of_voice` was read from `facts.visibility.share_of_voice`, a key nothing has
    ever written -- the figure is `mention_share_pct`. So the dashboard reported "no
    sample yet" for a business whose run had measured one, permanently and silently.
    """
    await _seed_run(
        owner_session,
        business_a,
        checkpoint=json.dumps(_checkpoint(problems=3, pages=4, sov=22.5)),
    )
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.seo_problems == 3
    assert summary.seo_pages_audited == 4
    assert summary.seo_truncated is False
    assert summary.share_of_voice == 22.5
    assert not any("share-of-voice" in gap for gap in summary.gaps)


async def test_the_audit_comes_from_the_latest_run_that_produced_one(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """A run that stopped at INTAKE writes a checkpoint with no facts in it. It must not
    blank the numbers -- and with 25 such runs it used to, because the query filtered on
    `checkpoint IS NOT NULL` and that column is NOT NULL DEFAULT '{}'.
    """
    now = datetime.now(UTC)
    await _seed_run(
        owner_session,
        business_a,
        checkpoint=json.dumps(_checkpoint(problems=7, pages=2)),
        created_at=now - timedelta(hours=1),
    )
    for minutes in range(30):
        await _seed_run(
            owner_session,
            business_a,
            state="failed",
            checkpoint="{}",
            created_at=now - timedelta(minutes=minutes),
        )
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.seo_problems == 7, (
        "30 factless runs crowded out the only run that audited anything"
    )
    assert summary.runs_total == 31


async def test_a_share_recorded_only_by_measure_is_still_found(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """MEASURE writes its own view of the same probe under `measurement.share_of_voice`,
    a TOP-LEVEL state key rather than one inside `facts`. A run that reached MEASURE after
    a resume can carry that and nothing under `facts.visibility`, so a reader that looked
    only inside `facts` would report "no sample" for the run that measured most recently.
    """
    await _seed_run(
        owner_session,
        business_a,
        checkpoint=json.dumps(
            {
                "facts": {},
                "measurement": {
                    "published_refs": 0,
                    "share_of_voice": {
                        "source": "harvest",
                        "baseline": {
                            "measured": True,
                            "usable_answers": 41,
                            "using_fake_provider": False,
                            "mention_share_pct": 31.25,
                        },
                    },
                },
            }
        ),
    )
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.share_of_voice == 31.25
    assert summary.seo_problems is None, "this run audited no site, and must not claim to"


async def test_a_share_measured_against_the_fake_provider_is_refused_and_named(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """The fake provider answers deterministically, so the share it produces measures us
    and not the market. Reporting `0` or the number itself would both be fabrications; the
    gap says which one it is so the owner can act on it."""
    await _seed_run(
        owner_session,
        business_a,
        checkpoint=json.dumps(_checkpoint(sov=60.0, fake_provider=True)),
    )
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.share_of_voice is None
    assert any("fake model provider" in gap for gap in summary.gaps)


async def test_an_unusable_probe_reports_no_share_rather_than_zero_percent(
    owner_session: AsyncSession, business_a: UUID
) -> None:
    """Every probe came back `no_answer`, so `mention_share_pct` is NULL: `no_answer` is
    excluded from the denominator and a share of zero usable answers is not zero. A model
    outage must not read as the brand being absent."""
    await _seed_run(owner_session, business_a, checkpoint=json.dumps(_checkpoint(sov=None)))
    await owner_session.commit()

    summary = await _dashboard(business_a)

    assert summary.share_of_voice is None
    assert summary.seo_problems == 3, "the audit on the same checkpoint is still readable"


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_another_business_reads_none_of_it(
    owner_session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    """Row-level security is what excludes these rows -- no statement in the service
    carries a `business_id` predicate -- so this is also a test that the read happens on a
    scoped session. Business A is asserted too: a query that returned nothing to ANYONE
    would otherwise pass this test.
    """
    link = await _seed_link(owner_session, business_a, channel="linkedin", clicks=9)
    await _seed_click(owner_session, business_a, link, is_bot=True)
    await _seed_run(owner_session, business_a, checkpoint=json.dumps(_checkpoint()))
    await _seed_lead(owner_session, business_a)
    await _seed_usage(owner_session, business_a, usd="1.00000000")
    await owner_session.commit()

    mine = await _dashboard(business_a)
    theirs = await _dashboard(business_b)

    assert (mine.clicks_total, mine.leads_total, mine.runs_total) == (9, 1, 1)
    assert mine.share_of_voice == 22.5

    assert theirs.clicks_total is None, "another business's links are visible"
    assert theirs.clicks_by_channel == ()
    assert theirs.clicks_from_bots == 0
    assert (theirs.runs_total, theirs.leads_total) == (0, 0)
    assert theirs.spend_usd is None, "another business's spend is visible"
    assert theirs.seo_problems is None
    assert theirs.share_of_voice is None
