"""Signup, login, session resolution, and session revocation.

Four rules shape this module, and each exists because its opposite is a real
bug someone ships every year.

**Signup is one transaction.** A user row without a business row is a state every
later screen would have to check for. Creating both together removes the state
instead of handling it, so nothing downstream needs a "user has no business yet"
branch.

**Login reveals nothing about which accounts exist.** An unknown email and a wrong
password return the same value, and they cost the same work: when there is no user
to verify against, a throwaway hash is verified anyway. Skipping that would make
"no such account" roughly a hundred times faster than "wrong password", which is a
perfectly usable enumeration oracle over the network. Deactivated accounts join the
same bucket for the same reason.

**Password strength is length plus a denylist, and nothing else.** Character-class
rules ("one upper, one digit, one symbol") measurably push people toward
``Password1!`` -- they shrink the search space they claim to widen. Twelve
characters with the obvious candidates removed is the better trade.

**Every argon2 call goes through the bounded wrapper, and revocation is a real
operation.** Both hashing and verifying cost 64 MiB, so signup amplifies memory
exactly as much as login does and neither may run unbounded --
:func:`~backend.app.core.security.hash_password_bounded` and
``verify_password_bounded`` are the only versions this module calls. And because
the session token is stateless HMAC, ending a session needs
:func:`revoke_sessions`: without it, logout clears the browser's copy of a cookie
that stays valid for another thirty days in anyone else's hands.

One tension this module does NOT solve, and should not pretend to: signup must
tell the caller that an address is already registered, because it cannot create
the account and cannot silently do nothing. The 409 is therefore an enumeration
oracle on the signup route specifically. The message is kept neutral so it does not
*confirm* whose account it is, but a determined attacker still learns that the
address is in use. Closing it properly means not answering at signup at all --
accepting the request and sending either a "welcome" or a "someone tried to sign up
with your address" email, and telling the caller only "check your inbox". That
needs an email pipeline (Phase 8), so it is a deliberate deferral rather than an
oversight.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import (
    hash_password,
    hash_password_bounded,
    needs_rehash,
    revocation_watermark,
    verify_password_bounded,
)
from backend.app.db.models import Business, User

# --------------------------------------------------------------------------- #
# Errors -- typed, so the route layer maps them to status codes without guessing
# --------------------------------------------------------------------------- #


class AuthServiceError(Exception):
    """Base class for every refusal this module raises."""


class InvalidEmailError(AuthServiceError):
    """The address is not shaped like an email address."""


class WeakPasswordError(AuthServiceError):
    """The password fails the length or denylist policy."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmailTakenError(AuthServiceError):
    """An account already exists for this address."""


class InvalidBusinessNameError(AuthServiceError):
    """The business name is empty or unusable."""


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #

MIN_PASSWORD_LENGTH: Final = 12

# Not a DoS bound on hashing -- argon2's cost is independent of input length --
# but on everything before it: the request body, the copy in memory, the log line
# that must never contain it.
MAX_PASSWORD_LENGTH: Final = 256

# Fewer distinct characters than this and the length is theatre: "aaaaaaaaaaaa"
# is twelve characters and one guess.
MIN_DISTINCT_CHARACTERS: Final = 5

# Deliberately short. This is not a breach-corpus check (that is a service call,
# and a good later addition); it is the handful of things a person types when a
# form demands twelve characters and they do not want to think.
_DENYLIST: Final[frozenset[str]] = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456789012345",
        "1qaz2wsx3edc",
        "abcdefghijkl",
        "administrator",
        "asdfghjklzxc",
        "changeme1234",
        "footballfootball",
        "iloveyouiloveyou",
        "letmeinletmein",
        "monkeymonkey",
        "passw0rdpassw0rd",
        "password1234",
        "passwordpassword",
        "passwortpasswort",
        "qazwsxedcrfv",
        "qwertyuiop123",
        "qwertyuiopasd",
        "qwertzuiopas",
        "sommer2024!!",
        "superman1234",
        "trustno1trustno1",
        "welcome123456",
        "zaq12wsxcde3",
    }
)

# Intentionally permissive. Real deliverability is proven by sending mail, not by
# a regular expression, and every strict address regex on the internet rejects
# somebody's valid address. This only rules out input that cannot be an address.
_EMAIL_RE: Final = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")

MAX_EMAIL_LENGTH: Final = 320  # matches users.email (String(320))
MAX_BUSINESS_NAME_LENGTH: Final = 255  # matches businesses.name (String(255))

# Verified when no user row exists, so that path costs an argon2 verification too.
# Computed once at import, not lazily: a lazily built dummy would make the FIRST
# unknown-email login slower than every later one, which is its own small signal.
# The plaintext is random and discarded, so nothing can ever verify against it.
_dummy_hash: Final[str] = hash_password(secrets.token_urlsafe(32))


def normalise_email(raw: str) -> str:
    """Trim, lowercase, and sanity-check an address.

    Normalisation is what makes the unique index mean anything: without it,
    ``Foo@example.test`` would create a second account shadowing ``foo@example.test``
    and either could be used to sign in to the other's data. Applied identically on
    signup and on login, or the two would disagree about who someone is.

    Only the domain is genuinely case-insensitive per RFC 5321, but every mail
    provider a small business uses treats the local part that way too, and a
    customer who cannot log in because they capitalised their own name is a
    support ticket we would deserve.
    """
    candidate = raw.strip().lower()
    if len(candidate) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(candidate):
        raise InvalidEmailError("That does not look like an email address.")
    return candidate


def validate_password(password: str) -> None:
    """Raise :class:`WeakPasswordError` if the password fails policy."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Please use at least {MIN_PASSWORD_LENGTH} characters. "
            "A short phrase you will remember beats a short password you will not."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Please use at most {MAX_PASSWORD_LENGTH} characters.")
    if len(set(password)) < MIN_DISTINCT_CHARACTERS:
        raise WeakPasswordError("That password repeats too few characters to be a real one.")
    if password.strip().lower() in _DENYLIST:
        raise WeakPasswordError("That password is one of the most commonly guessed ones.")


def _clean_business_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise InvalidBusinessNameError("Please give the business a name.")
    if len(name) > MAX_BUSINESS_NAME_LENGTH:
        raise InvalidBusinessNameError(f"Please use at most {MAX_BUSINESS_NAME_LENGTH} characters.")
    return name


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SignupResult:
    """Plain values, not ORM objects.

    Returning detached instances invites a lazy load after the session closes,
    which fails at render time rather than here.
    """

    user_id: UUID
    business_id: UUID
    email: str


async def signup(
    email: str, password: str, business_name: str, *, session: AsyncSession
) -> SignupResult:
    """Create a user and their first business in one transaction.

    Validation runs before anything is written, so a weak password never costs a
    round trip. The duplicate-address check is the unique index itself rather than
    a prior ``SELECT``: a check-then-insert has a window between the two in which a
    concurrent signup wins, and the index has no window.

    Commits on success. On any failure the transaction is rolled back, so the
    "user with no business" state cannot exist even for the length of a request.
    """
    normalised = normalise_email(email)
    validate_password(password)
    name = _clean_business_name(business_name)

    # Bounded: hashing costs the same 64 MiB as verifying, so an unthrottled
    # signup route is the same memory-amplification DoS as an unthrottled login.
    password_hash = await hash_password_bounded(password)
    user = User(id=uuid4(), email=normalised, password_hash=password_hash, is_active=True)
    business = Business(id=uuid4(), owner_id=user.id, name=name)

    session.add(user)
    session.add(business)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "23505":
            # Neutral on purpose: it does not name the address, and it does not
            # say whose account it is. See the module docstring -- this still
            # leaks existence, and that is not fixable without an email step.
            raise EmailTakenError("That account could not be created.") from exc
        raise
    except Exception:
        await session.rollback()
        raise

    return SignupResult(user_id=user.id, business_id=business.id, email=normalised)


async def _find_by_email(email: str, session: AsyncSession) -> User | None:
    # `populate_existing` for the same reason as `load_active_user`: a row already
    # in the identity map would otherwise hand back cached attributes, and a stale
    # `sessions_valid_from` here means minting a session token that the very next
    # request refuses.
    result = await session.execute(
        select(User).where(User.email == email).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def authenticate(email: str, password: str, *, session: AsyncSession) -> User | None:
    """Return the user for valid credentials, or ``None``.

    ``None`` covers unknown address, wrong password, and deactivated account
    alike. The caller must answer all three with the same status and the same
    message -- and this function makes them cost the same work, which is the half
    that a response body cannot fix.

    A successful login with an out-of-date hash silently re-hashes at current
    parameters. That is what makes the argon2 cost in ``core.security`` raisable
    later without a forced reset for everyone.
    """
    try:
        normalised = normalise_email(email)
    except InvalidEmailError:
        # Cannot match any row, but must not return faster than a real miss.
        await verify_password_bounded(password, _dummy_hash)
        return None

    user = await _find_by_email(normalised, session)
    if user is None:
        await verify_password_bounded(password, _dummy_hash)
        return None

    if not await verify_password_bounded(password, user.password_hash):
        return None
    if not user.is_active:
        return None

    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password_bounded(password)
        await session.commit()

    return user


async def load_active_user(user_id: UUID, *, session: AsyncSession) -> User | None:
    """Resolve a session's user id to an active user, or ``None``.

    Read on every authenticated request, which is deliberate: deactivation takes
    effect on the next request rather than whenever the cookie happens to expire.
    Do not "optimise" that round trip into a cache -- switching an account off
    would then take up to thirty days to mean anything.

    ``populate_existing`` is load-bearing for the same reason. A ``select`` that
    finds a row already in the session's identity map returns the *cached*
    attribute values by default, and the factory runs with
    ``expire_on_commit=False``. So on any session that outlives one request, a
    ``sessions_valid_from`` bumped by :func:`revoke_sessions` would be invisible
    here and the revocation check would read a stale ``None`` -- a revoked session
    silently honoured. Forcing the refresh makes the docstring above true instead
    of aspirational.
    """
    result = await session.execute(
        select(User)
        .where(User.id == user_id, User.is_active.is_(True))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def revoke_sessions(user_id: UUID, *, session: AsyncSession) -> datetime:
    """Invalidate every session token issued to ``user_id`` before now.

    This is the revocation the stateless token could not have on its own: the
    signed cookie is not stored anywhere, so there is nothing to delete, and
    ``users.sessions_valid_from`` is the one bit of server state that can refuse
    it. Logout calls this -- which is what makes logout mean something rather than
    only clearing the browser's copy -- and a password change must call it too,
    because the whole point of changing a password is ending the sessions of
    whoever knew the old one.

    Returns the watermark actually stored. **A caller that immediately issues a
    replacement session must sign it with this value**, not with a fresh
    ``now()``: see :func:`~backend.app.core.security.revocation_watermark` for the
    same-second problem that solves.

    The stored value never moves backwards. ``greatest`` rather than a plain
    assignment because a clock that steps back -- an NTP correction, a VM
    migration -- would otherwise *lower* the watermark and un-revoke sessions that
    had already been refused. ``RETURNING`` is what lets this function report the
    value that won.

    Unknown or already-deleted user: the ``UPDATE`` matches no row and the
    computed watermark is returned. Deliberately not an error, because logout is
    reachable with a validly signed cookie for a user who has since been removed,
    and that must still be a quiet 204.
    """
    watermark = revocation_watermark(datetime.now(UTC))
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            sessions_valid_from=func.greatest(
                func.coalesce(User.sessions_valid_from, watermark), watermark
            )
        )
        .returning(User.sessions_valid_from)
    )
    stored = result.scalar_one_or_none()
    await session.commit()
    return stored if stored is not None else watermark
