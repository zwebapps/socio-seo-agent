"""Signup, login, and session resolution.

Three rules shape this module, and each exists because its opposite is a real
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
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import hash_password, needs_rehash, verify_password
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

    user = User(id=uuid4(), email=normalised, password_hash=hash_password(password), is_active=True)
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
    result = await session.execute(select(User).where(User.email == email))
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
        verify_password(password, _dummy_hash)
        return None

    user = await _find_by_email(normalised, session)
    if user is None:
        verify_password(password, _dummy_hash)
        return None

    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await session.commit()

    return user


async def load_active_user(user_id: UUID, *, session: AsyncSession) -> User | None:
    """Resolve a session's user id to an active user, or ``None``.

    Read on every authenticated request, which is deliberate: deactivation takes
    effect on the next request rather than whenever the cookie happens to expire.
    """
    result = await session.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))
    return result.scalar_one_or_none()
