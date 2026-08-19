"""Password hashing and session-token signing.

Pure functions only: no database, no request, no settings lookup. Everything a
caller needs is passed in, which is what makes this the one module in the auth
stack that can be reasoned about on its own.

Two choices are load-bearing and are not up for casual revision.

**Argon2id, via argon2-cffi.** It is the current OWASP first choice, it is
memory-hard, and it resists GPU cracking in a way bcrypt does not. The cost
parameters are written out below rather than left implicit so that raising them
is a visible, reviewable diff -- and :func:`needs_rehash` exists so that raising
them upgrades existing users on their next successful login instead of locking
them out.

**An HMAC-signed opaque token, NOT a JWT.** The token is
``{user_id}.{issued_at_epoch}.{hmac_sha256}``. A JWT would carry a caller-visible
``alg`` header, which is an attack surface (``alg: none``, HS/RS confusion) bought
in exchange for a self-description nobody here needs: this token is read by
exactly one server, which already knows how it was made. The signature covers the
timestamp as well as the user id, so a captured cookie cannot be re-dated to
extend its own life, and it is compared with :func:`hmac.compare_digest` -- a
``==`` on a MAC short-circuits on the first wrong byte and leaks, one byte at a
time, how much of a forged signature was right.

**Stateless does not have to mean irrevocable.** :func:`verify_session` returns the
token's issued-at alongside the user id, and :func:`session_is_revoked` compares it
with a per-user watermark (``users.sessions_valid_from``). Bumping that column
invalidates every token minted before it, which is what turns logout from "clear
the browser's copy" into an actual revocation and gives a password change something
to do about the sessions it should end. The whole cost is one column and one
comparison; the alternative -- a server-side session table -- buys per-device
revocation we have no UI for, in exchange for a database read that cannot be
cached and a row per login.

The one sharp edge is resolution, and it is handled here rather than left to
callers: see :func:`revocation_watermark`.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Final
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import HashingError, InvalidHashError, VerificationError

from backend.app.core.rate_limit import PASSWORD_HASH_CONCURRENCY, ConcurrencyGate

# RFC 9106's "second recommended" profile: 64 MiB, 3 passes, 4 lanes. Written
# out rather than relying on the library default so that a future bump is a
# reviewable change here, not a silent consequence of a dependency upgrade.
#
# Output is 97 characters, which fits users.password_hash (String(255)) with room
# for a longer salt or a raised hash length later.
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Tolerated forward clock skew between the signer and the verifier. A token
# stamped further ahead than this is refused: without the check, a clock that
# jumped forward once would mint sessions that outlive the max age.
_CLOCK_SKEW = timedelta(minutes=5)

#: The session token carries whole seconds (``int(timestamp())``), so every
#: comparison against it is only meaningful to one second. Named because the
#: revocation watermark has to round to it -- see :func:`revocation_watermark`.
TOKEN_TIMESTAMP_RESOLUTION: Final = timedelta(seconds=1)

#: Bounds how many argon2 hashes run at once, process-wide, and moves each one off
#: the event loop. 64 MiB per hash means this constant is a memory ceiling, which
#: is the only real defence against the login DoS -- a rate limit still admits a
#: whole window's budget in a single burst. See ``core.rate_limit``.
PASSWORD_HASH_GATE: Final = ConcurrencyGate(PASSWORD_HASH_CONCURRENCY, name="argon2")


def hash_password(plain: str) -> str:
    """Return an argon2id hash of ``plain``, salted per call."""
    return _HASHER.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return whether ``plain`` matches ``hashed``.

    Never raises. A corrupted or truncated ``password_hash`` column is a failed
    login, not a 500 -- and an exception here would additionally be an oracle,
    since "malformed hash" and "wrong password" would become distinguishable from
    the outside.
    """
    try:
        return _HASHER.verify(hashed, plain)
    except (VerificationError, InvalidHashError, HashingError, TypeError, ValueError):
        return False


def needs_rehash(hashed: str) -> bool:
    """Return whether ``hashed`` was produced with weaker parameters than current.

    Call this after a *successful* verification and, if true, replace the stored
    hash with a fresh one. That is what allows the cost parameters above to be
    raised without a forced password reset for everyone.
    """
    try:
        return _HASHER.check_needs_rehash(hashed)
    except (InvalidHashError, ValueError):
        # Unparseable: it is certainly not at current parameters. Saying True is
        # the safe direction -- the worst case is one redundant rehash.
        return True


async def hash_password_bounded(plain: str) -> str:
    """:func:`hash_password`, under the process-wide argon2 gate.

    Every request path must use this rather than the synchronous version. Hashing
    costs the same 64 MiB as verifying, so signup is as much of a memory
    amplifier as login is, and an unbounded blocking call also stalls the whole
    event loop for its duration.
    """
    return await PASSWORD_HASH_GATE.run(lambda: hash_password(plain))


async def verify_password_bounded(plain: str, hashed: str) -> bool:
    """:func:`verify_password`, under the process-wide argon2 gate.

    The gate is what makes peak memory a function of
    ``PASSWORD_HASH_CONCURRENCY`` instead of a function of how many requests an
    attacker chose to send at once.
    """
    return await PASSWORD_HASH_GATE.run(lambda: verify_password(plain, hashed))


def _signature(body: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_session(user_id: UUID, *, issued_at: datetime, secret: str) -> str:
    """Return a signed session token for ``user_id``.

    ``issued_at`` should be timezone-aware; a naive value is read as UTC rather
    than as local time, because a server whose timezone changes must not
    invalidate or extend every live session.
    """
    stamped = issued_at if issued_at.tzinfo is not None else issued_at.replace(tzinfo=UTC)
    body = f"{user_id}.{int(stamped.timestamp())}"
    return f"{body}.{_signature(body, secret)}"


@dataclass(frozen=True, slots=True)
class VerifiedSession:
    """What a good token proves: who, and when it was minted.

    ``issued_at`` is returned rather than discarded because it is the only input
    the revocation check has. Without it, ``users.sessions_valid_from`` would be a
    column nothing could act on.
    """

    user_id: UUID
    #: UTC, whole seconds -- the resolution the token itself carries.
    issued_at: datetime


def verify_session(token: str, *, secret: str, max_age: timedelta) -> VerifiedSession | None:
    """Return the session carried by ``token``, or ``None`` if it is not usable.

    ``None`` covers every failure -- malformed, forged, re-dated, expired -- and
    the caller is expected to answer all of them with the same 401. Distinguishing
    them for the caller would tell an attacker which half of a guess was right.

    The signature is checked BEFORE anything in the token is parsed or believed,
    and it is checked with :func:`hmac.compare_digest`.

    Revocation is deliberately NOT checked here: this module has no database, and
    the watermark lives on the user row. The caller pairs this with
    :func:`session_is_revoked` once it has loaded that row.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None

    raw_user_id, raw_issued_at, signature = parts
    expected = _signature(f"{raw_user_id}.{raw_issued_at}", secret)
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        return None

    # Reached only for a token this server signed, so the parsing below is
    # defensive rather than adversarial -- but it still must not raise.
    try:
        user_id = UUID(raw_user_id)
        issued_at = datetime.fromtimestamp(int(raw_issued_at), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None

    now = datetime.now(UTC)
    if issued_at - now > _CLOCK_SKEW:
        return None
    if now - issued_at > max_age:
        return None
    return VerifiedSession(user_id=user_id, issued_at=issued_at)


def _as_utc(moment: datetime) -> datetime:
    """Read a naive datetime as UTC, never as local time.

    ``users.sessions_valid_from`` is ``timestamptz`` so asyncpg hands back an aware
    value, but a naive one can still arrive from a test, a fixture, or a driver
    change. Guessing local time would silently move the watermark by the server's
    offset -- in one direction that un-revokes stolen sessions, and in the other it
    logs everybody out.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def revocation_watermark(at: datetime) -> datetime:
    """The value to store in ``sessions_valid_from`` to revoke everything issued now.

    Rounded UP to the next whole second, and that is the entire fix for the
    same-second edge case.

    The token carries ``int(issued_at.timestamp())``, so its timestamp is truncated
    to a whole second. A watermark of a bare ``now()`` therefore has sub-second
    precision the token cannot express, and a replacement session minted in the
    same second as the revocation truncates to *before* the watermark -- so the new
    token is refused by its own revocation, and the user is logged out by logging
    in.

    Rounding the watermark up instead of rounding the comparison down is the
    stronger of the two available fixes, because it closes the window in both
    directions. Every token minted anywhere in the second of the revocation --
    including the attacker's, which may have been minted a few hundred
    milliseconds *before* the bump -- is refused, and a replacement signed with the
    returned instant (or later) is accepted. Comparing at second resolution alone
    would leave that sub-second sliver open for the stolen cookie.

    The cost is that a legitimate login racing the revocation inside the same
    second is also refused and has to be repeated. That is a sub-second window,
    and it errs toward refusing a session rather than honouring a revoked one.

    Callers that issue a replacement session immediately -- a password change, say
    -- must sign it with this returned value, not with a fresh ``now()``.
    """
    aware = _as_utc(at)
    resolution = TOKEN_TIMESTAMP_RESOLUTION.total_seconds()
    return datetime.fromtimestamp(ceil(aware.timestamp() / resolution) * resolution, tz=UTC)


def session_issued_at(valid_from: datetime | None, *, now: datetime | None = None) -> datetime:
    """The instant a NEW session must be stamped with, given the user's watermark.

    Normally ``now()``. But :func:`revocation_watermark` rounds up, so for up to
    one second after a revocation the watermark sits slightly in the *future* --
    and a token minted with a bare ``now()`` in that sliver truncates to before it
    and is refused on the very next request. That is the same-second edge case, and
    it is not hypothetical: logging in immediately after logging out lands in it,
    not just a password change.

    Rather than ask every caller to remember, the rule is "never mint a token our
    own watermark would refuse". Returning ``max(now, watermark)`` makes that
    structural: the only way to be refused is to be issued before a revocation that
    has actually happened.

    The returned value can be up to one second ahead of the real clock, which is
    three hundred times inside the five-minute skew :func:`verify_session` already
    tolerates, and it extends the session's life by that same sub-second.
    """
    moment = now if now is not None else datetime.now(UTC)
    if valid_from is None:
        return moment
    return max(moment, _as_utc(valid_from))


def session_is_revoked(issued_at: datetime, valid_from: datetime | None) -> bool:
    """Whether a token minted at ``issued_at`` falls before the user's watermark.

    ``valid_from`` of ``None`` -- the default for every user who has never logged
    out or changed a password -- revokes nothing.

    The comparison is made at the token's own resolution: both sides are truncated
    to whole seconds, because that is all the token records and a finer comparison
    would be comparing against precision that does not exist. Belt and braces with
    :func:`revocation_watermark`, which rounds the stored value up -- this half
    keeps the check safe even for a watermark written by something that did not,
    such as a hand-run ``UPDATE`` during an incident.
    """
    if valid_from is None:
        return False
    return int(issued_at.timestamp()) < int(_as_utc(valid_from).timestamp())
