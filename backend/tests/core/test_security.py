"""Password hashing and session-token signing — the crypto layer, tested alone.

No database, no FastAPI, no settings. Everything here is a pure function, and
keeping it that way is the point: the two things most likely to be got wrong in
an auth system are the KDF choice and the signature comparison, and neither
needs a service to prove.

Three assertions in this file are about *how* rather than *what*, which is
unusual and deliberate:

* the stored hash must be argon2**id** — a bcrypt or sha256 hash would satisfy
  every round-trip test in this file while being the wrong answer;
* two hashes of the same password must differ, which is the only black-box
  evidence that a per-hash salt exists;
* ``verify_session`` must use ``hmac.compare_digest``. A ``==`` on a MAC is a
  timing oracle that lets an attacker forge a signature byte by byte. Timing
  cannot be measured reliably in a test process, so the code path is asserted
  from the source instead. That is weaker than a measurement and stronger than
  nothing.
"""

import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.app.core import security

SECRET = "test-secret-not-a-real-one"
OTHER_SECRET = "a-different-secret"


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #


def test_hash_and_verify_round_trip() -> None:
    hashed = security.hash_password("correct horse battery staple")
    assert security.verify_password("correct horse battery staple", hashed) is True


def test_wrong_password_is_rejected() -> None:
    hashed = security.hash_password("correct horse battery staple")
    assert security.verify_password("correct horse battery stapl", hashed) is False


def test_hash_is_argon2id() -> None:
    """Not bcrypt, not sha256, not a homemade KDF. The prefix is the proof."""
    assert security.hash_password("a-long-enough-password").startswith("$argon2id$")


def test_two_hashes_of_the_same_password_differ() -> None:
    """Black-box evidence of a per-hash salt: no salt means identical output."""
    first = security.hash_password("a-long-enough-password")
    second = security.hash_password("a-long-enough-password")
    assert first != second
    assert security.verify_password("a-long-enough-password", first) is True
    assert security.verify_password("a-long-enough-password", second) is True


def test_verify_password_is_false_for_garbage_rather_than_raising() -> None:
    """A corrupted column must fail the login, not 500 the endpoint."""
    assert security.verify_password("anything", "not-a-hash-at-all") is False
    assert security.verify_password("anything", "") is False


def test_needs_rehash_is_false_for_a_fresh_hash() -> None:
    assert security.needs_rehash(security.hash_password("a-long-enough-password")) is False


def test_needs_rehash_is_true_for_weaker_parameters() -> None:
    """Parameters can be raised later without locking anyone out.

    A hash produced with lower cost than the current policy must be reported as
    stale, so the next successful login can silently upgrade it.
    """
    from argon2 import PasswordHasher, Type

    weak = PasswordHasher(
        time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8, type=Type.ID
    ).hash("a-long-enough-password")

    assert security.needs_rehash(weak) is True
    # ...and it must still verify, or "upgrade on next login" is unreachable.
    assert security.verify_password("a-long-enough-password", weak) is True


def test_needs_rehash_is_true_for_an_unparseable_hash() -> None:
    """A hash we cannot read is definitionally not at current parameters."""
    assert security.needs_rehash("not-a-hash-at-all") is True


# --------------------------------------------------------------------------- #
# Session tokens
# --------------------------------------------------------------------------- #


def _token(user_id: UUID, *, age: timedelta = timedelta(0), secret: str = SECRET) -> str:
    return security.sign_session(user_id, issued_at=datetime.now(UTC) - age, secret=secret)


def test_token_round_trip() -> None:
    user_id = uuid4()
    resolved = security.verify_session(_token(user_id), secret=SECRET, max_age=timedelta(days=30))
    assert resolved == user_id


def test_token_shape_is_three_dotted_parts_and_not_a_jwt() -> None:
    """Deliberately not a JWT: no alg field means no alg-confusion attack."""
    token = _token(uuid4())
    parts = token.split(".")
    assert len(parts) == 3
    assert UUID(parts[0])
    assert int(parts[1]) > 0
    assert not token.startswith("ey")  # a JWT header always base64s to this


def test_tampered_signature_is_rejected() -> None:
    user_id, token = uuid4(), _token(uuid4())
    body, _, signature = token.rpartition(".")
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]

    assert (
        security.verify_session(f"{body}.{flipped}", secret=SECRET, max_age=timedelta(days=30))
        is None
    )
    # Swapping the payload for another user must not verify either.
    assert (
        security.verify_session(
            f"{user_id}.{token.split('.')[1]}.{signature}",
            secret=SECRET,
            max_age=timedelta(days=30),
        )
        is None
    )


def test_expired_token_is_rejected() -> None:
    token = _token(uuid4(), age=timedelta(days=31))
    assert security.verify_session(token, secret=SECRET, max_age=timedelta(days=30)) is None


def test_token_at_the_edge_of_the_window_is_still_accepted() -> None:
    user_id = uuid4()
    token = _token(user_id, age=timedelta(days=29, hours=23))
    assert security.verify_session(token, secret=SECRET, max_age=timedelta(days=30)) == user_id


def test_token_signed_with_a_different_secret_is_rejected() -> None:
    token = _token(uuid4(), secret=OTHER_SECRET)
    assert security.verify_session(token, secret=SECRET, max_age=timedelta(days=30)) is None


def test_token_issued_in_the_future_is_rejected() -> None:
    """A clock that ran backwards must not mint an unexpirable session."""
    token = security.sign_session(
        uuid4(), issued_at=datetime.now(UTC) + timedelta(hours=1), secret=SECRET
    )
    assert security.verify_session(token, secret=SECRET, max_age=timedelta(days=30)) is None


def test_malformed_tokens_are_rejected_rather_than_raising() -> None:
    """Every one of these arrives from a cookie an attacker controls."""
    malformed = [
        "",
        "...",
        "onepart",
        "two.parts",
        "a.b.c.d",
        "not-a-uuid.123.deadbeef",
        f"{uuid4()}.not-a-number.deadbeef",
        f"{uuid4()}.123",
        "\x00.\x00.\x00",
    ]
    for token in malformed:
        assert security.verify_session(token, secret=SECRET, max_age=timedelta(days=30)) is None


def test_signature_is_compared_in_constant_time() -> None:
    """Asserting the code path, because timing cannot be measured in-process.

    ``==`` on a MAC short-circuits on the first differing byte, which leaks how
    much of a forged signature was correct. ``hmac.compare_digest`` does not.
    """
    source = inspect.getsource(security.verify_session)
    assert "compare_digest" in source
    assert "==" not in source.replace("!=", "")


def test_signature_covers_the_timestamp_not_only_the_user() -> None:
    """Otherwise a captured token could be re-dated to never expire."""
    user_id = uuid4()
    fresh = _token(user_id)
    old = _token(user_id, age=timedelta(days=31))
    # Splice the old token's (expired) timestamp onto the fresh signature.
    spliced = f"{user_id}.{old.split('.')[1]}.{fresh.split('.')[2]}"
    assert security.verify_session(spliced, secret=SECRET, max_age=timedelta(days=30)) is None
