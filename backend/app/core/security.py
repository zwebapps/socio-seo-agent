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
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import HashingError, InvalidHashError, VerificationError

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


def verify_session(token: str, *, secret: str, max_age: timedelta) -> UUID | None:
    """Return the user id carried by ``token``, or ``None`` if it is not usable.

    ``None`` covers every failure -- malformed, forged, re-dated, expired -- and
    the caller is expected to answer all of them with the same 401. Distinguishing
    them for the caller would tell an attacker which half of a guess was right.

    The signature is checked BEFORE anything in the token is parsed or believed,
    and it is checked with :func:`hmac.compare_digest`.
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
    return user_id
