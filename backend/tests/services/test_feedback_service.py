"""Feedback, and the proposal it becomes — never the change it makes by itself.

The two assertions that carry this module:

* **``distil`` proposes at three occurrences and nothing at two.** One rejection is
  an opinion. If a single complaint could rewrite a brand's voice, one bad day
  would be carried for months.
* **``dna`` is unchanged until an approval.** Asserted by reading the column
  directly after a distil, not by trusting the return value. This is the consent
  boundary of the whole feature: an agent that silently applied what it inferred
  would change how a customer sounds without asking, and they would have no way to
  find out why.

The validation tests are hermetic, and that is a real assertion rather than a
convenience: ``record`` validates the rubric BEFORE it queries, so a session that
raises on use proves a malformed request never opens a transaction.

The cross-business test deliberately runs as the table OWNER, which is a superuser
locally and therefore bypasses row-level security. Under the restricted role RLS
would hide the other business's row and the test would pass even if the service had
no check at all — so it would be a test of Postgres, not of this module.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from backend.app.core.config import get_settings
from backend.app.services import feedback_service, memory_service
from backend.app.services.feedback_service import (
    AXIS_MAX,
    AXIS_MIN,
    ContentPieceNotFoundError,
    InvalidAxesError,
    InvalidVerdictError,
    ProposalNotFoundError,
    themes_in,
)

EMAIL_PREFIX = "fbsvc-test-"
_CONNECTION_FAILURES = (OperationalError, ConnectionRefusedError, OSError)

TOO_LONG = "Far too long and full of padding"
EXCLAIMS = "Please stop using exclamation marks"


# --------------------------------------------------------------------------- #
# Hermetic: validation happens before the database is touched
# --------------------------------------------------------------------------- #


class _NeverQueried:
    """A stand-in session that fails the test if anything reaches the database."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "the database was queried for a request that should have been refused "
            "on its own contents"
        )

    def add(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("a row was added for a request that should have been refused")

    async def flush(self) -> None:
        raise AssertionError("a flush happened for a request that should have been refused")


def _stub_session() -> AsyncSession:
    return cast(AsyncSession, _NeverQueried())


@pytest.mark.parametrize("rating", [0, -1, 6, 99, AXIS_MAX + 1, AXIS_MIN - 1])
async def test_a_rating_outside_the_scale_is_refused(rating: int) -> None:
    """Refused, not clamped. A clamped 9 would become a 5 and read as praise."""
    with pytest.raises(InvalidAxesError):
        await feedback_service.record(
            uuid4(),
            uuid4(),
            verdict="rejected",
            axes={"on_brand": rating},
            session=_stub_session(),
        )


async def test_a_non_integer_rating_is_refused() -> None:
    with pytest.raises(InvalidAxesError):
        await feedback_service.record(
            uuid4(),
            uuid4(),
            verdict="approved",
            axes={"seo": cast(int, "5")},
            session=_stub_session(),
        )


async def test_a_boolean_rating_is_refused() -> None:
    """``True`` is an ``int`` in Python, so without an explicit check a checkbox
    would be stored as the rating 1 — the worst possible score."""
    with pytest.raises(InvalidAxesError):
        await feedback_service.record(
            uuid4(),
            uuid4(),
            verdict="approved",
            axes={"accuracy": cast(int, True)},
            session=_stub_session(),
        )


async def test_an_unknown_axis_is_refused() -> None:
    with pytest.raises(InvalidAxesError):
        await feedback_service.record(
            uuid4(),
            uuid4(),
            verdict="approved",
            axes={"vibes": 5},
            session=_stub_session(),
        )


@pytest.mark.parametrize("verdict", ["", "APPROVED", "maybe", "ok"])
async def test_an_unknown_verdict_is_refused(verdict: str) -> None:
    with pytest.raises(InvalidVerdictError):
        await feedback_service.record(
            uuid4(), uuid4(), verdict=verdict, axes={}, session=_stub_session()
        )


# --------------------------------------------------------------------------- #
# Hermetic: the grouping logic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Too many exclamation marks", "exclamation"),
        ("bitte keine Ausrufezeichen", "exclamation"),
        ("Lose the emoji", "emoji"),
        ("Reads far too salesy", "hype"),
        ("Way too long", "length"),
        ("Full of jargon", "jargon"),
        ("Too casual for our customers", "formality"),
        ("This price is made up", "unsupported"),
        ("Obvious keyword stuffing", "stuffing"),
        ("Why is this in English", "language"),
    ],
)
def test_themes_are_recognised_in_both_languages(reason: str, expected: str) -> None:
    assert expected in themes_in(reason)


def test_an_unrecognised_complaint_matches_no_theme() -> None:
    """Guard the guard: a matcher that matched everything would make the recurrence
    threshold meaningless."""
    assert themes_in("the third paragraph should come first") == ()


def test_one_reason_can_carry_two_complaints() -> None:
    """ "Far too long and full of exclamation marks" is genuinely two complaints.
    Counting it once would make a real pattern need six rejections, not three."""
    matched = themes_in("far too long and full of exclamation marks")
    assert set(matched) == {"length", "exclamation"}


# --------------------------------------------------------------------------- #
# Database fixtures
# --------------------------------------------------------------------------- #


async def _engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except _CONNECTION_FAILURES as exc:
        await engine.dispose()
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")
    except InterfaceError:
        await engine.dispose()
        raise
    return engine


@dataclass(frozen=True)
class World:
    """Two businesses under two users, each with one content piece."""

    business_a: UUID
    business_b: UUID
    piece_a: UUID
    piece_b: UUID


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = await _engine(get_settings().database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """The restricted runtime role, which is what production connects as."""
    engine = await _engine(get_settings().app_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> AsyncIterator[World]:
    ids = World(uuid4(), uuid4(), uuid4(), uuid4())
    factory = async_sessionmaker(owner_engine, expire_on_commit=False)

    async with factory() as s:
        for business_id, piece_id, label in (
            (ids.business_a, ids.piece_a, "a"),
            (ids.business_b, ids.piece_b, "b"),
        ):
            user_id = uuid4()
            await s.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, is_active) "
                    "VALUES (:id, :email, 'x', true)"
                ),
                {"id": user_id, "email": f"{EMAIL_PREFIX}{user_id.hex}@example.test"},
            )
            await s.execute(
                text(
                    "INSERT INTO businesses (id, owner_id, name, slug, locale) VALUES "
                    "(:id, :o, :n, 'fixture-' || "
                    "left(replace(cast(gen_random_uuid() AS text), '-', ''), 12), 'de')"
                ),
                {"id": business_id, "o": user_id, "n": f"fbsvc {label}"},
            )
            await s.execute(
                text(
                    "INSERT INTO content_pieces "
                    "(id, business_id, surface, title, body_md, status) "
                    "VALUES (:id, :b, 'google', :t, 'body', 'draft')"
                ),
                {"id": piece_id, "b": business_id, "t": f"piece {label}"},
            )
        await s.commit()

    yield ids

    async with factory() as s:
        await s.execute(
            text("DELETE FROM users WHERE email LIKE :prefix"), {"prefix": f"{EMAIL_PREFIX}%"}
        )
        await s.commit()


@asynccontextmanager
async def _tx(engine: AsyncEngine, business_id: UUID) -> AsyncIterator[AsyncSession]:
    """ONE transaction, scoped exactly the way production scopes one.

    This mirrors ``backend.app.db.session.business_session``: a transaction-local
    GUC (``set_config(..., true)``), committed on the way out. Each step of a test
    therefore gets its own transaction on its own connection, which is what makes
    the assertions meaningful -- a preference that only survives inside the session
    that wrote it has not been remembered at all.

    Two shapes that look equivalent and are not, recorded so they are not
    reintroduced: a long-lived session from a ``NullPool`` factory loses the GUC at
    the first ``commit`` (it releases the connection and the next statement gets a
    fresh one, so every row silently becomes invisible); and a session bound to an
    explicit ``AsyncConnection`` keeps the GUC but its ``commit`` does not commit
    the connection's own transaction, so nothing is ever visible to anyone else.
    """
    async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_business_id', :bid, true)"),
            {"bid": str(business_id)},
        )
        yield session


async def _dna(engine: AsyncEngine, business_id: UUID) -> dict[str, Any]:
    """``businesses`` carries no ``business_id`` and therefore no policy, so this
    read needs no scoping -- and reading it on a separate connection is the point:
    it proves the write was committed, not merely flushed."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        stored = (
            await s.execute(text("SELECT dna FROM businesses WHERE id = :id"), {"id": business_id})
        ).scalar_one()
    return dict(stored or {})


async def _reject(session: AsyncSession, piece: UUID, business: UUID, reason: str) -> None:
    await feedback_service.record(
        piece,
        business,
        verdict="rejected",
        axes={"on_brand": 2, "accuracy": 5},
        reject_reason=reason,
        session=session,
    )


async def _reject_times(engine: AsyncEngine, world: "World", reason: str, times: int) -> None:
    async with _tx(engine, world.business_a) as s:
        for _ in range(times):
            await _reject(s, world.piece_a, world.business_a, reason)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_record_stores_the_rubric_and_the_reason(
    app_engine: AsyncEngine, world: World
) -> None:
    async with _tx(app_engine, world.business_a) as s:
        await _reject(s, world.piece_a, world.business_a, EXCLAIMS)

    async with _tx(app_engine, world.business_a) as s:
        row = (
            await s.execute(
                text(
                    "SELECT verdict, axes, reject_reason FROM feedback WHERE content_piece_id = :id"
                ),
                {"id": world.piece_a},
            )
        ).one()

    assert row.verdict == "rejected"
    assert row.axes == {"on_brand": 2, "accuracy": 5}
    assert row.reject_reason == EXCLAIMS


@pytest.mark.db
async def test_feedback_on_another_businesss_piece_is_refused(
    owner_engine: AsyncEngine, world: World
) -> None:
    """Run as the OWNER on purpose -- a superuser bypasses RLS, so this proves the
    service's own predicate rather than Postgres's policy."""
    factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with factory() as s:
        with pytest.raises(ContentPieceNotFoundError):
            await feedback_service.record(
                world.piece_a,
                world.business_b,
                verdict="rejected",
                axes={"on_brand": 1},
                reject_reason="not mine",
                session=s,
            )
        await s.rollback()

        count = (
            await s.execute(
                text("SELECT count(*) FROM feedback WHERE business_id = :b"),
                {"b": world.business_b},
            )
        ).scalar_one()

    assert count == 0


@pytest.mark.db
async def test_a_rejection_with_no_reason_is_still_recorded(
    app_engine: AsyncEngine, world: World
) -> None:
    """Refusing it would also throw away the axis scores, and the axes are the part
    that says which half of the prompt was wrong."""
    async with _tx(app_engine, world.business_a) as s:
        await feedback_service.record(
            world.piece_a,
            world.business_a,
            verdict="rejected",
            axes={"on_brand": 1},
            reject_reason=None,
            session=s,
        )

    async with _tx(app_engine, world.business_a) as s:
        stored = (
            await s.execute(
                text("SELECT count(*) FROM feedback WHERE business_id = :b"),
                {"b": world.business_a},
            )
        ).scalar_one()

    assert stored == 1


# --------------------------------------------------------------------------- #
# Distilling
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_two_rejections_propose_nothing(app_engine: AsyncEngine, world: World) -> None:
    """One rejection is an opinion; two is still not a pattern."""
    await _reject_times(app_engine, world, EXCLAIMS, 2)

    async with _tx(app_engine, world.business_a) as s:
        proposed = await feedback_service.distil(world.business_a, session=s)

    async with _tx(app_engine, world.business_a) as s:
        pending = await feedback_service.list_proposals(world.business_a, session=s)

    assert proposed == []
    assert pending == []


@pytest.mark.db
async def test_three_rejections_of_the_same_theme_propose_one_rule(
    app_engine: AsyncEngine, world: World
) -> None:
    """Three differently worded complaints about the same thing: the theme is what
    recurs, not the sentence."""
    async with _tx(app_engine, world.business_a) as s:
        for reason in (
            "Too many exclamation marks",
            "please stop with the exclamation marks",
            "bitte keine Ausrufezeichen mehr",
        ):
            await _reject(s, world.piece_a, world.business_a, reason)

    async with _tx(app_engine, world.business_a) as s:
        proposed = await feedback_service.distil(world.business_a, session=s)

    async with _tx(app_engine, world.business_a) as s:
        pending = await feedback_service.list_proposals(world.business_a, session=s)

    assert proposed == ["Never use exclamation marks."]
    assert [p.rule for p in pending] == ["Never use exclamation marks."]
    assert pending[0].status == "proposed"
    # The evidence is what makes the proposal reviewable rather than an assertion.
    assert len(pending[0].derived_from) == 3


@pytest.mark.db
async def test_a_proposal_is_not_applied_until_it_is_approved(
    app_engine: AsyncEngine, world: World
) -> None:
    """The consent boundary, and the whole feature's honesty.

    ``dna`` is read on a different connection, so this asserts the absence of a
    COMMITTED write rather than trusting the return value of ``distil``.
    """
    await _reject_times(app_engine, world, EXCLAIMS, 3)

    async with _tx(app_engine, world.business_a) as s:
        await feedback_service.distil(world.business_a, session=s)

    before = await _dna(app_engine, world.business_a)
    assert before.get("preferences", []) == []

    async with _tx(app_engine, world.business_a) as s:
        memory_before = await memory_service.load_memory(world.business_a, session=s)
        pending = await feedback_service.list_proposals(world.business_a, session=s)
    # A pending proposal must reach no prompt.
    assert memory_service.to_prompt_lines(memory_before) == []

    async with _tx(app_engine, world.business_a) as s:
        applied = await feedback_service.approve_proposal(
            pending[0].id, world.business_a, session=s
        )

    after = await _dna(app_engine, world.business_a)

    async with _tx(app_engine, world.business_a) as s:
        memory_after = await memory_service.load_memory(world.business_a, session=s)
        approved = await feedback_service.list_proposals(
            world.business_a, session=s, status="approved"
        )

    assert applied == "Never use exclamation marks."
    assert after["preferences"] == ["Never use exclamation marks."]
    assert memory_service.to_prompt_lines(memory_after) == ["Never use exclamation marks."]
    assert [p.rule for p in approved] == ["Never use exclamation marks."]


@pytest.mark.db
async def test_an_approved_rule_is_not_proposed_again(
    app_engine: AsyncEngine, world: World
) -> None:
    await _reject_times(app_engine, world, EXCLAIMS, 3)

    async with _tx(app_engine, world.business_a) as s:
        await feedback_service.distil(world.business_a, session=s)

    async with _tx(app_engine, world.business_a) as s:
        pending = await feedback_service.list_proposals(world.business_a, session=s)
        await feedback_service.approve_proposal(pending[0].id, world.business_a, session=s)

    # The same rejections are still on file, so a second distil sees them again.
    async with _tx(app_engine, world.business_a) as s:
        again = await feedback_service.distil(world.business_a, session=s)

    assert again == []


@pytest.mark.db
async def test_a_rejected_proposal_does_not_come_back(
    app_engine: AsyncEngine, world: World
) -> None:
    """A declined proposal returning on every run is nagging, and a product that
    nags gets its notifications switched off."""
    await _reject_times(app_engine, world, EXCLAIMS, 3)

    async with _tx(app_engine, world.business_a) as s:
        await feedback_service.distil(world.business_a, session=s)

    async with _tx(app_engine, world.business_a) as s:
        pending = await feedback_service.list_proposals(world.business_a, session=s)
        await s.execute(
            text("UPDATE learned_style SET status = 'rejected' WHERE id = :id"),
            {"id": pending[0].id},
        )

    async with _tx(app_engine, world.business_a) as s:
        again = await feedback_service.distil(world.business_a, session=s)

    async with _tx(app_engine, world.business_a) as s:
        with pytest.raises(ProposalNotFoundError):
            await feedback_service.approve_proposal(pending[0].id, world.business_a, session=s)

    assert again == []
    assert (await _dna(app_engine, world.business_a)).get("preferences", []) == []


@pytest.mark.db
async def test_min_occurrences_is_the_only_knob(app_engine: AsyncEngine, world: World) -> None:
    """Two rejections DO become a pattern if the caller says two is enough -- which
    proves the threshold is doing the work and the theme match is not the gate."""
    await _reject_times(app_engine, world, TOO_LONG, 2)

    async with _tx(app_engine, world.business_a) as s:
        proposed = await feedback_service.distil(world.business_a, session=s, min_occurrences=2)

    assert proposed == ["Keep it short: no filler, no repetition, no restating the heading."]


@pytest.mark.db
async def test_a_repeated_complaint_no_theme_recognises_is_proposed_verbatim(
    app_engine: AsyncEngine, world: World
) -> None:
    """Otherwise the loop could only ever learn the nine things the theme table
    already knows about. The owner's own sentence is proposed, and they approve it."""
    async with _tx(app_engine, world.business_a) as s:
        for reason in (
            "The third paragraph should come first",
            "the third   paragraph should come first",
            "THE THIRD PARAGRAPH SHOULD COME FIRST",
        ):
            await _reject(s, world.piece_a, world.business_a, reason)

    async with _tx(app_engine, world.business_a) as s:
        proposed = await feedback_service.distil(world.business_a, session=s)

    assert proposed == ["The third paragraph should come first"]


@pytest.mark.db
async def test_another_businesss_rejections_do_not_leak_into_a_proposal(
    owner_engine: AsyncEngine, world: World
) -> None:
    """Run as the owner so RLS is not what is being credited: two rejections in A
    and one in B must not add up to a pattern in either."""
    factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with factory() as s:
        for _ in range(2):
            await _reject(s, world.piece_a, world.business_a, EXCLAIMS)
        await _reject(s, world.piece_b, world.business_b, EXCLAIMS)
        await s.commit()

        in_a = await feedback_service.distil(world.business_a, session=s)
        in_b = await feedback_service.distil(world.business_b, session=s)
        await s.commit()

    assert in_a == []
    assert in_b == []


@pytest.mark.db
async def test_approving_another_businesss_proposal_is_refused(
    owner_engine: AsyncEngine, world: World
) -> None:
    factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    async with factory() as s:
        for _ in range(3):
            await _reject(s, world.piece_a, world.business_a, EXCLAIMS)
        await s.commit()
        await feedback_service.distil(world.business_a, session=s)
        await s.commit()
        pending = await feedback_service.list_proposals(world.business_a, session=s)

        with pytest.raises(ProposalNotFoundError):
            await feedback_service.approve_proposal(pending[0].id, world.business_b, session=s)
        await s.rollback()

    assert (await _dna(owner_engine, world.business_b)).get("preferences", []) == []
