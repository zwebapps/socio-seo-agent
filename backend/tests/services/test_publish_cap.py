"""The weekly volume cap's arithmetic, and the two things it must never get wrong.

`headroom` slices a list of approved pieces, so a negative one would slice from the wrong
end and publish the pieces the cap was supposed to refuse. And `sentence` is what an owner
reads: a cap message without the count and the window gives them nothing to act on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from backend.app.core.config import get_settings
from backend.app.services import publish_cap
from backend.app.services.publish_cap import (
    WEEKLY_WINDOW_DAYS,
    WeeklyPublishState,
    counter_for,
    weekly_publish_state,
    window_start,
)


def _state(published: int, cap: int = 10) -> WeeklyPublishState:
    return WeeklyPublishState(published=published, cap=cap)


# --------------------------------------------------------------------------- #
# headroom
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("published", "cap", "expected"),
    [(0, 10, 10), (4, 10, 6), (9, 10, 1), (10, 10, 0), (11, 10, 0), (0, 0, 0)],
)
def test_headroom_is_what_is_left(published: int, cap: int, expected: int) -> None:
    assert _state(published, cap).headroom == expected


def test_headroom_is_never_negative() -> None:
    """A cap LOWERED below what a business already published this week is an ordinary
    operator action, and it must not produce a negative allowance: `pieces[:-2]` publishes
    everything except the last two, which is the exact opposite of refusing."""
    assert _state(published=20, cap=6).headroom == 0
    assert _state(published=20, cap=6).exhausted is True


def test_a_zero_cap_refuses_everything() -> None:
    """`BUSINESS_WEEKLY_PUBLISH_CAP=0` is documented as the kill switch, so it has to be
    exhausted at a count of zero rather than allowing one last piece through."""
    assert _state(published=0, cap=0).exhausted is True


def test_exhausted_is_exactly_no_headroom() -> None:
    assert _state(published=9, cap=10).exhausted is False
    assert _state(published=10, cap=10).exhausted is True


# --------------------------------------------------------------------------- #
# the sentence
# --------------------------------------------------------------------------- #


def test_the_sentence_states_the_count_the_cap_and_the_window() -> None:
    sentence = _state(published=10, cap=10).sentence
    assert "10 of its 10" in sentence
    assert str(WEEKLY_WINDOW_DAYS) in sentence


def test_the_sentence_stops_before_the_consequence() -> None:
    """Two callers append different consequences — "these were not sent" and "this post
    stays queued" — so the shared half must not contain either, or one of them reads as a
    sentence about the wrong thing."""
    sentence = _state(published=10).sentence
    assert "so " not in sentence
    assert not sentence.endswith(".")


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #


def test_the_window_is_rolling_not_calendar() -> None:
    """A calendar week hands every business a full allowance at midnight on Monday and
    invites a burst against the reset. A rolling window has no reset to game."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    assert window_start(now=now) == now - timedelta(days=7)


# --------------------------------------------------------------------------- #
# reading the ceiling
# --------------------------------------------------------------------------- #


async def test_the_ceiling_comes_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """One place reads the setting, so two callers cannot enforce two different numbers —
    the defect that let a scheduled run past the monthly USD ceiling."""
    tight = get_settings().model_copy(update={"business_weekly_publish_cap": 3})
    monkeypatch.setattr(publish_cap, "get_settings", lambda: tight)

    async def count(_business: UUID) -> int:
        return 3

    state = await weekly_publish_state(uuid4(), count=count)

    assert state.cap == 3
    assert state.exhausted is True


async def test_an_explicit_cap_beats_the_setting() -> None:
    async def count(_business: UUID) -> int:
        return 2

    assert (await weekly_publish_state(uuid4(), count=count, cap=99)).headroom == 97


async def test_the_counter_asks_the_ledger_for_the_window_and_the_action_types() -> None:
    """`counter_for` is the only place that binds those two, so a caller cannot count the
    wrong action type or the wrong period by accident."""
    seen: dict[str, object] = {}

    class _Ledger:
        async def published_since(
            self, business_id: UUID, *, action_types: object, since: datetime
        ) -> int:
            seen["business_id"] = business_id
            seen["action_types"] = action_types
            seen["since"] = since
            return 7

    business = uuid4()
    count = counter_for(_Ledger(), action_types={"social.post"})

    assert await count(business) == 7
    assert seen["business_id"] == business
    assert seen["action_types"] == {"social.post"}
    since = seen["since"]
    assert isinstance(since, datetime)
    assert timedelta(days=7) - timedelta(seconds=5) < datetime.now(UTC) - since


def test_the_cap_is_an_int_and_the_money_cap_is_not() -> None:
    """A guard against the obvious copy-paste: this cap counts rows and the monthly one
    sums `Numeric(12, 8)`. An `int` ceiling compared against a `Decimal` sum, or the
    reverse, is the kind of type confusion that only shows up at the boundary."""
    settings = get_settings()
    assert isinstance(settings.business_weekly_publish_cap, int)
    assert isinstance(settings.business_monthly_cap_usd, Decimal)
