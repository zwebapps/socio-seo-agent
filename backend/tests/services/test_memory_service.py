"""Business memory — the store that makes run 2 obey what run 1 was told.

The claim being tested is narrow and worth stating: **run checkpointing is not
memory.** A checkpoint lets one run resume; it cannot change how next week's run
writes. So the tests that matter here are the ones that prove a preference
survives a *different* session and reaches the prompt text — not that a write
round-trips.

Three groups:

* **Pure projection and prompt lines.** No database. ``to_prompt_lines`` is the
  last hop before text reaches a model, so its output is asserted EXACTLY. A test
  that only checked "the preference is in there somewhere" would still pass if the
  list were rendered twice, and duplication is the specific failure this module is
  written to prevent.
* **Append, dedup, forget.** Database-backed, marked ``db``. Dedup is asserted
  across case AND whitespace, because those are the two ways the same rule comes
  back from a human typing it a second time.
* **The cross-run demo, mechanically.** State a preference through one session;
  read it back through a session opened afterwards; assert the exact prompt line.

The database fixtures use a function-scoped engine: an asyncpg pool belongs to the
event loop that created it, and pytest-asyncio gives every test its own loop.
"""

from collections.abc import AsyncIterator
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
from backend.app.services import memory_service
from backend.app.services.memory_service import (
    MAX_PREFERENCES,
    MAX_RULE_LENGTH,
    BusinessMemory,
    BusinessNotFoundError,
    EmptyRuleError,
    PreferenceLimitError,
    RuleTooLongError,
    memory_from_dna,
    to_prompt_lines,
)

EMAIL_PREFIX = "memsvc-test-"
_CONNECTION_FAILURES = (OperationalError, ConnectionRefusedError, OSError)

BUSINESS_ID = UUID("11111111-1111-4111-8111-111111111111")


# --------------------------------------------------------------------------- #
# Pure: projection out of dna
# --------------------------------------------------------------------------- #


def test_memory_from_dna_reads_the_four_things_it_owns() -> None:
    memory = memory_from_dna(
        BUSINESS_ID,
        {
            "name": "Klempner Koblenz",  # not memory's business; must survive untouched
            "tone": "professional",
            "audience": "Hausbesitzer in Koblenz",
            "banned_claims": ["cheapest in Germany"],
            "preferences": ["Never use exclamation marks"],
        },
    )

    assert memory.tone == "professional"
    assert memory.audience == "Hausbesitzer in Koblenz"
    assert memory.banned_claims == ("cheapest in Germany",)
    assert memory.preferences == ("Never use exclamation marks",)
    assert memory.remembered_count == 1


def test_memory_from_dna_survives_junk_rather_than_raising() -> None:
    """``dna`` is JSONB: it can hold whatever an earlier version or a hand-run SQL
    statement put there. One malformed entry must not make a business unreadable."""
    memory = memory_from_dna(
        BUSINESS_ID,
        {
            "tone": 42,
            "audience": "   ",
            "banned_claims": "not a list",
            "preferences": ["  keep it short  ", "", None, 7, "keep it short"],
        },
    )

    assert memory.tone is None
    assert memory.audience is None
    assert memory.banned_claims == ()
    # Whitespace collapsed, blanks and non-strings dropped, the repeat deduplicated.
    assert memory.preferences == ("keep it short",)


def test_memory_from_dna_on_an_empty_dna_is_empty_not_an_error() -> None:
    memory = memory_from_dna(BUSINESS_ID, {})
    assert memory == BusinessMemory(business_id=BUSINESS_ID)
    assert to_prompt_lines(memory) == []


# --------------------------------------------------------------------------- #
# Pure: the exact lines a node receives
# --------------------------------------------------------------------------- #


def test_to_prompt_lines_is_exactly_audience_then_preferences_in_order() -> None:
    """Asserted as a whole list, not with ``in``.

    The failure this pins is duplication and reordering, and both survive an
    ``in`` check.
    """
    memory = BusinessMemory(
        business_id=BUSINESS_ID,
        tone="professional",
        audience="Hausbesitzer in Koblenz",
        banned_claims=("cheapest in Germany",),
        preferences=(
            "Never use exclamation marks",
            "Always state the callout fee before the hourly rate",
        ),
    )

    assert to_prompt_lines(memory) == [
        "Write for this audience: Hausbesitzer in Koblenz.",
        "Never use exclamation marks",
        "Always state the callout fee before the hourly rate",
    ]


def test_to_prompt_lines_omits_tone_and_banned_claims_deliberately() -> None:
    """``prompts.system`` already renders both from ``dna``.

    Emitting them here too would put the same instruction in the prompt twice, and
    a duplicated instruction reads as emphasis — so the tone would quietly start
    outweighing the task. If this test is ever "fixed" by adding them, check
    ``prompts.system`` first.
    """
    memory = BusinessMemory(
        business_id=BUSINESS_ID,
        tone="concise",
        banned_claims=("100% guaranteed",),
    )

    assert to_prompt_lines(memory) == []


def test_to_prompt_lines_of_an_empty_memory_is_empty() -> None:
    assert to_prompt_lines(BusinessMemory(business_id=BUSINESS_ID)) == []


# --------------------------------------------------------------------------- #
# Pure: dedup identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Never use exclamation marks", "never use exclamation marks"),
        ("Never use exclamation marks", "  Never   use exclamation marks  "),
        ("Never use exclamation marks", "NEVER USE EXCLAMATION MARKS"),
        ("Immer siezen", "immer  SIEZEN"),
    ],
)
def test_rule_key_treats_case_and_whitespace_variants_as_the_same_rule(
    left: str, right: str
) -> None:
    assert memory_service.rule_key(left) == memory_service.rule_key(right)


def test_rule_key_does_not_collapse_genuinely_different_rules() -> None:
    """Guard the guard: a key that collapsed everything would make dedup look
    perfect and silently swallow real preferences."""
    assert memory_service.rule_key("Never use exclamation marks") != memory_service.rule_key(
        "Never use emoji"
    )


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


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """The RESTRICTED runtime role — what production connects as.

    ``businesses`` carries no ``business_id`` and therefore no RLS policy, but the
    role still matters: it is the one whose grants must actually allow the UPDATE.
    """
    engine = await _engine(get_settings().app_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def business(app_engine: AsyncEngine) -> AsyncIterator[UUID]:
    """One business, seeded and torn down. ``dna`` starts as ``{}``."""
    business_id = uuid4()
    user_id = uuid4()
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active) "
                "VALUES (:id, :email, 'x', true)"
            ),
            {"id": user_id, "email": f"{EMAIL_PREFIX}{user_id.hex}@example.test"},
        )
        await s.execute(
            text("INSERT INTO businesses (id, owner_id, name, locale) VALUES (:id, :o, :n, 'de')"),
            {"id": business_id, "o": user_id, "n": "memsvc business"},
        )
        await s.commit()

    yield business_id

    async with factory() as s:
        await s.execute(
            text("DELETE FROM users WHERE email LIKE :prefix"), {"prefix": f"{EMAIL_PREFIX}%"}
        )
        await s.commit()


@pytest.fixture
def sessions(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A factory, not a session: the interesting tests need a *second* session."""
    return async_sessionmaker(app_engine, expire_on_commit=False)


# --------------------------------------------------------------------------- #
# Database-backed behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_load_memory_for_an_unknown_business_raises(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Not an empty memory. An empty memory is a real state — a business that has
    confirmed nothing — and conflating the two would let a run proceed for a
    tenant that does not exist."""
    async with sessions() as s:
        with pytest.raises(BusinessNotFoundError):
            await memory_service.load_memory(uuid4(), session=s)


@pytest.mark.db
async def test_a_preference_stated_in_one_session_is_obeyed_by_the_next(
    business: UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The cross-run demo, mechanically.

    Run 1 states the preference and ends. Run 2 is a different session that is
    never told anything — and the exact line it will put in the system prompt is
    asserted, because "it is in the database" is not the same claim as "it reaches
    the model".
    """
    async with sessions() as run_one:
        await memory_service.remember(business, rule="Never use exclamation marks", session=run_one)
        await run_one.commit()

    async with sessions() as run_two:
        memory = await memory_service.load_memory(business, session=run_two)

    assert to_prompt_lines(memory) == ["Never use exclamation marks"]
    assert memory.remembered_count == 1


@pytest.mark.db
@pytest.mark.parametrize(
    "restated",
    [
        "never use exclamation marks",
        "  Never   use   exclamation   marks ",
        "NEVER USE EXCLAMATION MARKS",
    ],
)
async def test_appending_the_same_rule_again_does_not_duplicate_it(
    business: UUID, sessions: async_sessionmaker[AsyncSession], restated: str
) -> None:
    """A duplicated instruction reads as emphasis, so this is a behaviour bug and
    not a tidiness one."""
    async with sessions() as s:
        await memory_service.remember(business, rule="Never use exclamation marks", session=s)
        await memory_service.remember(business, rule=restated, session=s)
        await s.commit()
        memory = await memory_service.load_memory(business, session=s)

    assert memory.preferences == ("Never use exclamation marks",)
    assert to_prompt_lines(memory) == ["Never use exclamation marks"]


@pytest.mark.db
async def test_appending_keeps_the_order_it_was_confirmed_in(
    business: UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with sessions() as s:
        for rule in ("Never use exclamation marks", "Immer siezen", "Keep paragraphs short"):
            await memory_service.remember(business, rule=rule, session=s)
        await s.commit()
        memory = await memory_service.load_memory(business, session=s)

    assert memory.preferences == (
        "Never use exclamation marks",
        "Immer siezen",
        "Keep paragraphs short",
    )


@pytest.mark.db
async def test_forget_removes_only_the_named_rule(
    business: UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with sessions() as s:
        for rule in ("Never use exclamation marks", "Immer siezen", "Keep paragraphs short"):
            await memory_service.remember(business, rule=rule, session=s)
        await s.commit()

        # Deliberately a case and whitespace variant: an owner should not have to
        # reproduce their own capitalisation to delete something.
        await memory_service.forget(business, rule="  immer SIEZEN ", session=s)
        await s.commit()

        memory = await memory_service.load_memory(business, session=s)

    assert memory.preferences == ("Never use exclamation marks", "Keep paragraphs short")


@pytest.mark.db
async def test_forget_of_something_absent_is_a_no_op(
    business: UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The caller's intent — "this must not be in force" — is already satisfied, so
    raising would make a double-clicked delete an error."""
    async with sessions() as s:
        await memory_service.remember(business, rule="Immer siezen", session=s)
        await s.commit()

        await memory_service.forget(business, rule="never heard of it", session=s)
        await s.commit()

        memory = await memory_service.load_memory(business, session=s)

    assert memory.preferences == ("Immer siezen",)


@pytest.mark.db
async def test_remember_leaves_the_rest_of_dna_alone(
    business: UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """A partial write must never drop a field it does not understand: ``dna`` also
    holds onboarding's output, and losing it would silently un-onboard a customer."""
    factory = sessions
    async with factory() as s:
        await s.execute(
            text("UPDATE businesses SET dna = CAST(:dna AS jsonb) WHERE id = :id"),
            {
                "dna": '{"name": "Klempner Koblenz", "city": "Koblenz", "tone": "professional"}',
                "id": business,
            },
        )
        await s.commit()

        await memory_service.remember(business, rule="Immer siezen", session=s)
        await s.commit()

        stored = (
            await s.execute(text("SELECT dna FROM businesses WHERE id = :id"), {"id": business})
        ).scalar_one()

    assert stored["name"] == "Klempner Koblenz"
    assert stored["city"] == "Koblenz"
    assert stored["tone"] == "professional"
    assert stored["preferences"] == ["Immer siezen"]


@pytest.mark.db
@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
async def test_a_blank_rule_is_refused(
    business: UUID, sessions: async_sessionmaker[AsyncSession], blank: str
) -> None:
    async with sessions() as s:
        with pytest.raises(EmptyRuleError):
            await memory_service.remember(business, rule=blank, session=s)


@pytest.mark.db
async def test_an_essay_is_refused_rather_than_carried_in_every_prompt(
    business: UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with sessions() as s:
        with pytest.raises(RuleTooLongError):
            await memory_service.remember(business, rule="x" * (MAX_RULE_LENGTH + 1), session=s)


@pytest.mark.db
async def test_the_preference_ceiling_refuses_rather_than_silently_dropping(
    business: UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """A dropped rule would leave the owner believing something is in force when it
    is not — which is exactly the drift this store exists to prevent."""
    async with sessions() as s:
        for index in range(MAX_PREFERENCES):
            await memory_service.remember(business, rule=f"rule number {index}", session=s)
        await s.commit()

        with pytest.raises(PreferenceLimitError):
            await memory_service.remember(business, rule="one rule too many", session=s)

        memory = await memory_service.load_memory(business, session=s)

    assert memory.remembered_count == MAX_PREFERENCES
