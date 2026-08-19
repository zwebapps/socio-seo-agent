"""Signup and login — the two operations that decide who anyone is.

Most of this file is about the failure paths, because that is where auth is
actually attacked:

* **Atomicity.** A user with no business is a state every later screen would have
  to handle. One transaction removes the state rather than handling it, and the
  test forces the second insert to fail to prove the first is rolled back.
* **Enumeration.** Wrong password and unknown email must be indistinguishable in
  the answer AND in the work done. The answer is asserted exactly; the work is
  asserted coarsely, by timing, because the whole point is that an absent user
  still costs an argon2 verification.
* **Normalisation.** ``  Foo@Example.test  `` and ``foo@example.test`` are the
  same account. If they were not, uniqueness would be decorative and a second
  signup could shadow the first.

The database tests are marked ``db``: they use a function-scoped engine because
an asyncpg pool is bound to the event loop that created it, and pytest-asyncio
gives every test its own loop.
"""

import inspect
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.core import security
from backend.app.core.config import get_settings
from backend.app.services import auth_service

# Every row this module creates carries this prefix, so teardown can remove its
# own rows and nothing else. Businesses go with them: the FK is ON DELETE CASCADE.
EMAIL_PREFIX = "authsvc-test-"

_CONNECTION_FAILURES = (OperationalError, ConnectionRefusedError, OSError)


def _email() -> str:
    return f"{EMAIL_PREFIX}{uuid4().hex}@example.test"


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session as the RESTRICTED runtime role, which is what production uses."""
    engine = create_async_engine(get_settings().app_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except _CONNECTION_FAILURES as exc:
        await engine.dispose()
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")
    except InterfaceError:
        await engine.dispose()
        raise

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(
                text("DELETE FROM users WHERE email LIKE :prefix"),
                {"prefix": f"{EMAIL_PREFIX}%"},
            )
            await session.commit()
    await engine.dispose()


async def _count(session: AsyncSession, sql: str, **params: object) -> int:
    result = await session.execute(text(sql), params)
    return int(result.scalar_one())


# --------------------------------------------------------------------------- #
# Password policy -- pure, no database
# --------------------------------------------------------------------------- #


def test_password_shorter_than_twelve_is_rejected() -> None:
    with pytest.raises(auth_service.WeakPasswordError):
        auth_service.validate_password("elevenchars")


def test_password_of_exactly_twelve_is_accepted() -> None:
    auth_service.validate_password("twelvechars!")


def test_obvious_passwords_are_rejected_even_when_long_enough() -> None:
    """Length alone lets ``passwordpassword`` through, and it must not."""
    for candidate in ("passwordpassword", "PasswordPassword", "123456789012", "qwertyuiopasd"):
        with pytest.raises(auth_service.WeakPasswordError):
            auth_service.validate_password(candidate)


def test_a_long_run_of_one_character_is_rejected() -> None:
    with pytest.raises(auth_service.WeakPasswordError):
        auth_service.validate_password("aaaaaaaaaaaaaaaa")


def test_no_character_class_rules_are_imposed() -> None:
    """A long all-lowercase passphrase is strong and must be allowed through.

    Character-class rules push people toward ``Password1!``; length does not.
    """
    auth_service.validate_password("correct horse battery staple")


def test_absurdly_long_passwords_are_rejected() -> None:
    """Bounded input: argon2's cost does not grow with length, but memory does."""
    with pytest.raises(auth_service.WeakPasswordError):
        auth_service.validate_password("x" * 5000)


def test_email_normalisation_is_trim_and_lowercase() -> None:
    assert auth_service.normalise_email("  Foo@Example.TEST ") == "foo@example.test"


def test_obviously_invalid_emails_are_rejected() -> None:
    for candidate in ("", "   ", "no-at-sign", "@example.test", "foo@", "a b@example.test"):
        with pytest.raises(auth_service.InvalidEmailError):
            auth_service.normalise_email(candidate)


# --------------------------------------------------------------------------- #
# Signup
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_signup_creates_exactly_one_user_and_one_business(db: AsyncSession) -> None:
    email = _email()

    result = await auth_service.signup(email, "correct horse battery", "Müller GmbH", session=db)

    assert await _count(db, "SELECT count(*) FROM users WHERE email = :e", e=email) == 1
    assert (
        await _count(db, "SELECT count(*) FROM businesses WHERE owner_id = :o", o=result.user_id)
        == 1
    )
    name = await db.execute(
        text("SELECT name FROM businesses WHERE id = :b"), {"b": result.business_id}
    )
    assert name.scalar_one() == "Müller GmbH"


@pytest.mark.db
async def test_signup_stores_an_argon2id_hash_and_never_the_password(db: AsyncSession) -> None:
    email = _email()
    await auth_service.signup(email, "correct horse battery", "Biz", session=db)

    stored = await db.execute(
        text("SELECT password_hash FROM users WHERE email = :e"), {"e": email}
    )
    hashed = stored.scalar_one()
    assert hashed.startswith("$argon2id$")
    assert "correct horse battery" not in hashed


@pytest.mark.db
async def test_signup_normalises_the_email(db: AsyncSession) -> None:
    raw = f"  {EMAIL_PREFIX}MiXeD-{uuid4().hex}@Example.TEST  "
    result = await auth_service.signup(raw, "correct horse battery", "Biz", session=db)

    assert result.email == raw.strip().lower()
    assert await _count(db, "SELECT count(*) FROM users WHERE email = :e", e=result.email) == 1


@pytest.mark.db
async def test_duplicate_email_raises_and_creates_nothing(db: AsyncSession) -> None:
    email = _email()
    await auth_service.signup(email, "correct horse battery", "First", session=db)

    with pytest.raises(auth_service.EmailTakenError):
        await auth_service.signup(email, "another good passphrase", "Second", session=db)

    assert await _count(db, "SELECT count(*) FROM users WHERE email = :e", e=email) == 1
    assert await _count(db, "SELECT count(*) FROM businesses WHERE name = :n", n="Second") == 0


@pytest.mark.db
async def test_duplicate_is_detected_across_case_and_whitespace(db: AsyncSession) -> None:
    """Normalisation is what makes the unique index mean anything."""
    email = _email()
    await auth_service.signup(email, "correct horse battery", "First", session=db)

    with pytest.raises(auth_service.EmailTakenError):
        await auth_service.signup(f"  {email.upper()} ", "correct horse battery", "X", session=db)


@pytest.mark.db
async def test_signup_is_atomic_when_the_business_insert_fails(db: AsyncSession) -> None:
    """A user with no business must be impossible, not merely unlikely.

    The 300-character name overflows ``businesses.name`` (String(255)), so the
    second insert of the pair fails after the first has been written.
    """
    email = _email()

    with pytest.raises(Exception):  # noqa: B017 -- the driver's error type is not the point
        await auth_service.signup(email, "correct horse battery", "N" * 300, session=db)

    await db.rollback()
    assert await _count(db, "SELECT count(*) FROM users WHERE email = :e", e=email) == 0


@pytest.mark.db
async def test_signup_rejects_a_weak_password_before_touching_the_database(
    db: AsyncSession,
) -> None:
    email = _email()
    with pytest.raises(auth_service.WeakPasswordError):
        await auth_service.signup(email, "short", "Biz", session=db)

    assert await _count(db, "SELECT count(*) FROM users WHERE email = :e", e=email) == 0


@pytest.mark.db
async def test_signup_rejects_a_blank_business_name(db: AsyncSession) -> None:
    with pytest.raises(auth_service.InvalidBusinessNameError):
        await auth_service.signup(_email(), "correct horse battery", "   ", session=db)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_authenticate_returns_the_user_for_correct_credentials(db: AsyncSession) -> None:
    email = _email()
    created = await auth_service.signup(email, "correct horse battery", "Biz", session=db)

    user = await auth_service.authenticate(email, "correct horse battery", session=db)

    assert user is not None
    assert user.id == created.user_id


@pytest.mark.db
async def test_authenticate_accepts_a_differently_cased_email(db: AsyncSession) -> None:
    email = _email()
    await auth_service.signup(email, "correct horse battery", "Biz", session=db)

    assert (
        await auth_service.authenticate(f" {email.upper()} ", "correct horse battery", session=db)
        is not None
    )


@pytest.mark.db
async def test_authenticate_rejects_a_wrong_password(db: AsyncSession) -> None:
    email = _email()
    await auth_service.signup(email, "correct horse battery", "Biz", session=db)

    assert await auth_service.authenticate(email, "correct horse batter", session=db) is None


@pytest.mark.db
async def test_authenticate_rejects_an_unknown_email(db: AsyncSession) -> None:
    assert await auth_service.authenticate(_email(), "correct horse battery", session=db) is None


@pytest.mark.db
async def test_authenticate_rejects_a_malformed_email_without_raising(db: AsyncSession) -> None:
    """A login form is a public endpoint; garbage in it is a 401, not a 422."""
    assert (
        await auth_service.authenticate("not-an-email", "correct horse battery", session=db) is None
    )


@pytest.mark.db
async def test_authenticate_rejects_a_deactivated_user(db: AsyncSession) -> None:
    email = _email()
    await auth_service.signup(email, "correct horse battery", "Biz", session=db)
    await db.execute(text("UPDATE users SET is_active = false WHERE email = :e"), {"e": email})
    await db.commit()

    assert await auth_service.authenticate(email, "correct horse battery", session=db) is None


def test_absent_user_path_verifies_a_dummy_hash() -> None:
    """Asserted from the source as well as by timing below.

    Skipping the verification when the row is absent would make "no such account"
    roughly two orders of magnitude faster than "wrong password", which is a
    perfectly usable account-enumeration oracle over the network.
    """
    source = inspect.getsource(auth_service.authenticate)
    assert "_dummy_hash" in source


@pytest.mark.db
async def test_unknown_email_and_wrong_password_cost_similar_work(db: AsyncSession) -> None:
    """Coarse by design: argon2 dominates, so the two paths should be close.

    The bound is deliberately loose (a factor of four) because this runs on
    shared CI hardware. It would still catch the real mistake, which is skipping
    the hash entirely -- that shows up as a factor of fifty or more.
    """
    email = _email()
    await auth_service.signup(email, "correct horse battery", "Biz", session=db)

    async def elapsed(target_email: str) -> float:
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            await auth_service.authenticate(target_email, "wrong password entirely", session=db)
            best = min(best, time.perf_counter() - started)
        return best

    wrong_password = await elapsed(email)
    unknown_email = await elapsed(_email())

    assert 0.25 < unknown_email / wrong_password < 4.0


@pytest.mark.db
async def test_a_successful_login_upgrades_a_stale_hash(db: AsyncSession) -> None:
    """This is what makes raising the argon2 cost parameters possible later."""
    from argon2 import PasswordHasher, Type

    email = _email()
    await auth_service.signup(email, "correct horse battery", "Biz", session=db)
    stale = PasswordHasher(
        time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8, type=Type.ID
    ).hash("correct horse battery")
    await db.execute(
        text("UPDATE users SET password_hash = :h WHERE email = :e"), {"h": stale, "e": email}
    )
    await db.commit()

    assert await auth_service.authenticate(email, "correct horse battery", session=db) is not None

    stored = await db.execute(
        text("SELECT password_hash FROM users WHERE email = :e"), {"e": email}
    )
    upgraded = stored.scalar_one()
    assert upgraded != stale
    assert security.needs_rehash(upgraded) is False


# --------------------------------------------------------------------------- #
# Session resolution
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_load_active_user_returns_the_user(db: AsyncSession) -> None:
    email = _email()
    created = await auth_service.signup(email, "correct horse battery", "Biz", session=db)

    user = await auth_service.load_active_user(created.user_id, session=db)
    assert user is not None
    assert user.email == email


@pytest.mark.db
async def test_load_active_user_is_none_for_an_unknown_id(db: AsyncSession) -> None:
    assert await auth_service.load_active_user(uuid4(), session=db) is None


@pytest.mark.db
async def test_load_active_user_is_none_for_a_deactivated_user(db: AsyncSession) -> None:
    """Deactivation must take effect on the next request, not at cookie expiry."""
    email = _email()
    created = await auth_service.signup(email, "correct horse battery", "Biz", session=db)
    await db.execute(
        text("UPDATE users SET is_active = false WHERE id = :i"), {"i": created.user_id}
    )
    await db.commit()

    assert await auth_service.load_active_user(created.user_id, session=db) is None
