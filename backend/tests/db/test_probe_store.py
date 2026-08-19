"""The geo probe store, against a real Postgres with RLS switched on.

What matters here is not that rows land -- it is that the number reconstructed
from them is the *same number* the engine computed at probe time:

* a **retried** run must not double-count. ``geo_results`` carries no run id, so
  the store defines a run by its ``probed_at`` stamp and folds a retry back onto
  the run it is retrying. Without that, a worker that crashed after writing half
  a run and then re-ran it would report a share of voice built from twelve
  answers to six questions;
* ``no_answer`` must stay out of the denominator on the way back in, exactly as
  it stays out on the way out. The arithmetic is not re-implemented here: the
  store rebuilds ``ProbeOutcome`` rows and hands them to the engine's own
  ``share_of_voice``, which is what makes "the same rule" literally true;
* a business that has never been probed returns ``None``. A first run is not an
  error, and a zero would be a measurement nobody took.

Tenancy is asserted positively throughout: RLS makes an unscoped read return zero
rows *silently* (docs/ARCHITECTURE.md section 6), so "business B sees nothing" is
only meaningful next to "business A still sees its own run".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.db.adapters import PostgresProbeStore
from backend.app.db.session import business_session
from backend.app.engines.geo import GeoPrompt, ProbeOutcome, build_prompt_set, share_of_voice

pytestmark = [pytest.mark.db]

MODEL = ("openrouter", "openai/gpt-4.1-mini")
OTHER_MODEL = ("anthropic", "claude-haiku-4-5")


@pytest.fixture
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the process-wide session factory at this test's engine.

    The adapter offers no session seam of its own -- every method goes through
    ``business_session`` so it cannot be called without the tenant GUC -- so this
    is the one place a test can hook in, and it leaves the real scoping under
    test rather than substituting a copy of it.
    """
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


@pytest.fixture
def store(scoped_sessions: None) -> PostgresProbeStore:
    return PostgresProbeStore()


@pytest.fixture
async def business_a(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[0]


@pytest.fixture
async def business_b(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[1]


@pytest.fixture
def prompts() -> list[GeoPrompt]:
    """A real prompt set, so ``prompt_id`` values are the genuine content hashes.

    The businesses seeded by ``two_businesses`` carry locale ``de``, which is what
    the store uses to rebuild those ids -- so an authentic set is what proves the
    reconstructed fingerprint matches the live one.
    """
    return build_prompt_set(
        business_name="Mueller Sanitaer",
        city="Koblenz",
        services=["Badsanierung", "Heizungswartung"],
        competitors=["Schmitz Haustechnik"],
        locale="de",
    )


def answered(
    prompt: GeoPrompt,
    *,
    model: tuple[str, str] = MODEL,
    mentioned: bool = False,
    cited: bool = False,
    competitors: Sequence[str] = (),
) -> ProbeOutcome:
    provider, name = model
    return ProbeOutcome(
        prompt_id=prompt.prompt_id,
        prompt_text=prompt.text,
        category=prompt.category,
        set_version=prompt.set_version,
        prompt_contains_brand=prompt.contains_brand,
        provider=provider,
        model=name,
        status="answered",
        mentioned=mentioned,
        cited=cited,
        competitors_mentioned=list(competitors),
        answer_excerpt="In Koblenz sind mehrere Betriebe zu empfehlen ...",
        usd=Decimal("0.00012"),
        latency_ms=740,
    )


def no_answer(prompt: GeoPrompt, *, model: tuple[str, str] = MODEL) -> ProbeOutcome:
    provider, name = model
    return ProbeOutcome(
        prompt_id=prompt.prompt_id,
        prompt_text=prompt.text,
        category=prompt.category,
        set_version=prompt.set_version,
        prompt_contains_brand=prompt.contains_brand,
        provider=provider,
        model=name,
        status="no_answer",
        error="ProviderRateLimitError: 429",
    )


async def count_rows(business_id: UUID, table: str) -> int:
    async with business_session(business_id) as db:
        result = await db.execute(text(f"SELECT count(*) FROM {table}"))
        return int(result.scalar_one())


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


async def test_a_saved_run_reads_back_as_the_score_the_engine_computed(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    outcomes = [
        answered(prompts[0], mentioned=True, cited=True, competitors=["Schmitz Haustechnik"]),
        answered(prompts[1]),
        answered(prompts[2], mentioned=True),
        no_answer(prompts[3]),
    ]
    live = share_of_voice(outcomes)

    written = await store.save_outcomes(business_a, outcomes)
    assert written == 4

    restored = await store.latest_share_of_voice(business_a)

    assert restored is not None
    assert restored.probes_total == live.probes_total
    assert restored.usable_answers == live.usable_answers
    assert restored.no_answer_count == live.no_answer_count
    assert restored.mentions == live.mentions
    assert restored.citations == live.citations
    assert restored.mention_share_pct == live.mention_share_pct
    assert restored.unprompted_usable_answers == live.unprompted_usable_answers
    assert restored.prompts_probed == live.prompts_probed
    assert restored.set_version == live.set_version
    # The fingerprint is what `diff_share_of_voice` compares. If reconstruction
    # cannot reproduce it, every trend silently reports "not comparable".
    assert restored.set_fingerprint == live.set_fingerprint
    assert [(share.provider, share.model) for share in restored.models] == [MODEL]


async def test_a_business_that_was_never_probed_has_no_previous_score(
    store: PostgresProbeStore, business_a: UUID
) -> None:
    assert await store.latest_share_of_voice(business_a) is None


async def test_prompts_are_reused_rather_than_duplicated_across_runs(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    """The prompt set is the instrument; a second run asks the same questions."""
    monday = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    tuesday = monday + timedelta(days=1)

    await store.save_outcomes(business_a, [answered(prompts[0])], probed_at=monday)
    await store.save_outcomes(business_a, [answered(prompts[0])], probed_at=tuesday)

    assert await count_rows(business_a, "geo_prompts") == 1
    assert await count_rows(business_a, "geo_results") == 2


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


async def test_a_retried_run_does_not_double_count(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    """A worker that dies mid-save and retries must not inflate share of voice.

    The retry writes the same (prompt, model) pairs for the same run, so it lands
    on the same rows. Nothing is appended, and the reconstructed score is
    identical rather than doubled.
    """
    outcomes = [
        answered(prompts[0], mentioned=True),
        answered(prompts[1]),
        no_answer(prompts[2]),
    ]

    await store.save_outcomes(business_a, outcomes)
    first = await store.latest_share_of_voice(business_a)

    await store.save_outcomes(business_a, outcomes)
    second = await store.latest_share_of_voice(business_a)

    assert await count_rows(business_a, "geo_results") == 3
    assert first is not None and second is not None
    assert second.probes_total == 3
    assert second.usable_answers == first.usable_answers == 2
    assert second.mentions == first.mentions == 1
    assert second.mention_share_pct == first.mention_share_pct


async def test_a_partial_run_completed_by_a_retry_stays_one_run(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    """The crash case: half a run was written, then the whole run was re-sent.

    The rows that already exist are updated in place and the missing ones are
    added to the *same* run, rather than the retry becoming a second, smaller
    run that then reads back as "the latest".
    """
    full = [
        answered(prompts[0], mentioned=True),
        answered(prompts[1], mentioned=True),
        answered(prompts[2]),
    ]

    await store.save_outcomes(business_a, full[:1])
    await store.save_outcomes(business_a, full)

    restored = await store.latest_share_of_voice(business_a)
    assert restored is not None
    assert restored.probes_total == 3
    assert restored.mentions == 2
    assert await count_rows(business_a, "geo_results") == 3


async def test_a_later_run_replaces_the_earlier_one_as_latest(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    last_week = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    this_week = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)

    await store.save_outcomes(
        business_a,
        [answered(prompts[0]), answered(prompts[1])],
        probed_at=last_week,
    )
    await store.save_outcomes(
        business_a,
        [answered(prompts[0], mentioned=True), answered(prompts[1], mentioned=True)],
        probed_at=this_week,
    )

    restored = await store.latest_share_of_voice(business_a)
    assert restored is not None
    assert restored.probes_total == 2
    assert restored.mentions == 2, "the older run must not be pooled into the latest"
    assert await count_rows(business_a, "geo_results") == 4


# --------------------------------------------------------------------------- #
# The denominator
# --------------------------------------------------------------------------- #


async def test_no_answer_rows_are_excluded_from_the_reconstructed_denominator(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    """One mention in three usable answers is 33.3%, not 20% across five probes.

    Counting the two failures as absence is the one arithmetic mistake that turns
    this metric from a measurement into a fabrication, so it is asserted on the
    read path as well as the write path.
    """
    outcomes = [
        answered(prompts[0], mentioned=True),
        answered(prompts[1]),
        answered(prompts[2]),
        no_answer(prompts[3]),
        no_answer(prompts[4]),
    ]
    await store.save_outcomes(business_a, outcomes)

    restored = await store.latest_share_of_voice(business_a)

    assert restored is not None
    assert restored.probes_total == 5
    assert restored.usable_answers == 3
    assert restored.no_answer_count == 2
    assert restored.mention_share_pct == pytest.approx(33.3)


async def test_a_run_in_which_every_probe_failed_is_unknown_not_zero(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    await store.save_outcomes(business_a, [no_answer(prompts[0]), no_answer(prompts[1])])

    restored = await store.latest_share_of_voice(business_a)

    assert restored is not None
    assert restored.usable_answers == 0
    assert restored.mention_share_pct is None
    assert "unknown, not zero" in restored.headline


async def test_per_model_breakdown_survives_the_round_trip(
    store: PostgresProbeStore, business_a: UUID, prompts: list[GeoPrompt]
) -> None:
    """A pooled percentage that is really "one model says yes, one says no"."""
    await store.save_outcomes(
        business_a,
        [
            answered(prompts[0], model=MODEL, mentioned=True),
            answered(prompts[0], model=OTHER_MODEL),
            answered(prompts[1], model=MODEL, mentioned=True),
            no_answer(prompts[1], model=OTHER_MODEL),
        ],
    )

    restored = await store.latest_share_of_voice(business_a)

    assert restored is not None
    assert restored.models_probed == 2
    by_model = {(share.provider, share.model): share for share in restored.models}
    assert by_model[MODEL].mentions == 2
    assert by_model[MODEL].usable_answers == 2
    assert by_model[OTHER_MODEL].mentions == 0
    assert by_model[OTHER_MODEL].usable_answers == 1
    assert by_model[OTHER_MODEL].no_answer_count == 1


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_one_business_never_reads_another_businesss_run(
    store: PostgresProbeStore, business_a: UUID, business_b: UUID, prompts: list[GeoPrompt]
) -> None:
    """Zero rows is also what a broken store returns, so both halves are asserted."""
    await store.save_outcomes(business_a, [answered(prompts[0], mentioned=True)])

    assert await store.latest_share_of_voice(business_b) is None

    restored = await store.latest_share_of_voice(business_a)
    assert restored is not None
    assert restored.mentions == 1
    assert restored.usable_answers == 1
