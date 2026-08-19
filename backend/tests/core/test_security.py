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

``verify_session`` now returns the token's issued-at as well as its user id,
because that timestamp is the only input the revocation check has -- without it
``users.sessions_valid_from`` would be a column nothing could act on. The round-trip
tests below assert both halves rather than only the id.

The revocation tests are mostly about ONE second. The token records whole seconds,
so a watermark with sub-second precision can refuse a replacement session minted in
the same second as the revocation -- logging a user out by logging them in. Both
halves of the fix are tested: the watermark rounds up, and the comparison truncates.
"""

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.app.core import rate_limit, security

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
    assert resolved is not None
    assert resolved.user_id == user_id


def test_verify_session_returns_the_issued_at_as_well_as_the_user() -> None:
    """The revocation check has no other input, so this is not a convenience."""
    signed_at = datetime.now(UTC) - timedelta(hours=3)
    resolved = security.verify_session(
        security.sign_session(uuid4(), issued_at=signed_at, secret=SECRET),
        secret=SECRET,
        max_age=timedelta(days=30),
    )

    assert resolved is not None
    # Whole seconds, in UTC: the resolution the token itself carries.
    assert resolved.issued_at == signed_at.replace(microsecond=0)
    assert resolved.issued_at.tzinfo is UTC


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
    resolved = security.verify_session(token, secret=SECRET, max_age=timedelta(days=30))
    assert resolved is not None
    assert resolved.user_id == user_id


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


# --------------------------------------------------------------------------- #
# Revocation -- the watermark, and the one second that makes it awkward
# --------------------------------------------------------------------------- #


def test_no_watermark_revokes_nothing() -> None:
    """``None`` is every user who has never logged out. It must be a no-op."""
    assert security.session_is_revoked(datetime.now(UTC), None) is False


def test_a_token_issued_before_the_watermark_is_refused() -> None:
    watermark = datetime.now(UTC)
    issued_at = watermark - timedelta(minutes=1)
    assert security.session_is_revoked(issued_at, watermark) is True


def test_a_token_issued_after_the_watermark_is_accepted() -> None:
    watermark = datetime.now(UTC)
    issued_at = watermark + timedelta(minutes=1)
    assert security.session_is_revoked(issued_at, watermark) is False


def test_a_long_lived_token_is_refused_by_a_much_later_watermark() -> None:
    """The case the column exists for: a stolen cookie, days into its 30."""
    issued_at = datetime.now(UTC) - timedelta(days=20)
    assert security.session_is_revoked(issued_at, datetime.now(UTC)) is True


def test_the_watermark_rounds_up_to_the_next_whole_second() -> None:
    """The token stores whole seconds, so the watermark must not sit inside one."""
    watermark = security.revocation_watermark(datetime(2026, 8, 19, 12, 0, 0, 400_000, tzinfo=UTC))
    assert watermark == datetime(2026, 8, 19, 12, 0, 1, tzinfo=UTC)


def test_a_watermark_already_on_a_second_boundary_is_left_alone() -> None:
    """Otherwise every revocation would drift a second further out than asked."""
    exact = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    assert security.revocation_watermark(exact) == exact


def test_a_naive_watermark_is_read_as_utc_not_as_local_time() -> None:
    """Guessing local time would move the watermark by the server's offset.

    One direction un-revokes stolen sessions; the other logs everyone out. Both
    are silent, and both depend on where the machine happens to be.
    """
    naive = datetime(2026, 8, 19, 12, 0, 0)  # deliberately naive: that IS the test
    aware = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)

    assert security.revocation_watermark(naive) == aware
    issued_at = aware - timedelta(seconds=1)
    assert security.session_is_revoked(issued_at, naive) is True


def test_a_token_issued_in_the_same_second_as_the_revocation_is_refused() -> None:
    """The half of the edge case that protects the ATTACKER's cookie from surviving.

    A watermark of a bare ``now()`` would truncate to the same second as a token
    minted a few hundred milliseconds earlier, so the stolen cookie would slip
    through. Rounding the watermark up closes that sliver.
    """
    revoked_at = datetime(2026, 8, 19, 12, 0, 0, 700_000, tzinfo=UTC)
    stolen_issued_at = datetime(2026, 8, 19, 12, 0, 0, 100_000, tzinfo=UTC)

    watermark = security.revocation_watermark(revoked_at)
    assert security.session_is_revoked(stolen_issued_at, watermark) is True


def test_a_replacement_signed_at_the_watermark_survives_its_own_revocation() -> None:
    """The other half: revoke and immediately re-issue, in the same second.

    This is the shape of a password change -- end every old session, then hand
    back a new one. Signing the replacement with the returned watermark is what
    keeps it alive; signing it with a fresh ``now()`` inside the same second is the
    bug, and the assertion below proves the returned value is the safe input.
    """
    revoked_at = datetime(2026, 8, 19, 12, 0, 0, 700_000, tzinfo=UTC)
    watermark = security.revocation_watermark(revoked_at)

    token = security.sign_session(uuid4(), issued_at=watermark, secret=SECRET)
    replacement = security.verify_session(token, secret=SECRET, max_age=timedelta(days=30))

    assert replacement is not None
    assert security.session_is_revoked(replacement.issued_at, watermark) is False


def test_a_new_session_is_stamped_at_the_watermark_when_that_is_still_ahead() -> None:
    """ "Never mint a token our own watermark would refuse", as one function.

    Because the watermark rounds up, it sits slightly in the future for up to a
    second, and a bare ``now()`` inside that sliver produces a token that is dead on
    arrival. Making the issue time ``max(now, watermark)`` removes the trap instead
    of asking every call site to remember it -- and logging in immediately after
    logging out is the common way to fall into it, not some exotic race.
    """
    now = datetime(2026, 8, 19, 12, 0, 0, 700_000, tzinfo=UTC)
    watermark = security.revocation_watermark(now)
    assert watermark > now

    issued_at = security.session_issued_at(watermark, now=now)
    assert issued_at == watermark
    assert security.session_is_revoked(issued_at, watermark) is False


def test_a_new_session_is_stamped_now_when_the_watermark_is_in_the_past() -> None:
    """The ordinary case must not be dragged forward to an old watermark."""
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    old = now - timedelta(days=3)
    assert security.session_issued_at(old, now=now) == now


def test_a_new_session_with_no_watermark_is_stamped_now() -> None:
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    assert security.session_issued_at(None, now=now) == now


def test_a_freshly_issued_session_is_never_immediately_revoked() -> None:
    """The property both helpers exist for, asserted across the whole second.

    Sampled at every microsecond offset that matters, because the bug only appears
    for a revocation whose sub-second part is non-zero -- which is almost all of
    them, and none of the ones a hand-written test would pick.
    """
    for microsecond in (0, 1, 499_999, 500_000, 999_999):
        now = datetime(2026, 8, 19, 12, 0, 0, microsecond, tzinfo=UTC)
        watermark = security.revocation_watermark(now)
        token = security.sign_session(
            uuid4(), issued_at=security.session_issued_at(watermark, now=now), secret=SECRET
        )
        verified = security.verify_session(token, secret=SECRET, max_age=timedelta(days=30))

        assert verified is not None, microsecond
        assert security.session_is_revoked(verified.issued_at, watermark) is False, microsecond


def test_the_comparison_ignores_sub_second_precision_the_token_cannot_carry() -> None:
    """Belt and braces for a watermark written by something that did not round up.

    A hand-run ``UPDATE ... = now()`` during an incident produces exactly that, and
    it must not lock the user out of the session they are about to create.
    """
    hand_written = datetime(2026, 8, 19, 12, 0, 0, 700_000, tzinfo=UTC)
    same_second = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    assert security.session_is_revoked(same_second, hand_written) is False


# --------------------------------------------------------------------------- #
# The argon2 concurrency gate
# --------------------------------------------------------------------------- #


async def test_bounded_hash_and_verify_round_trip() -> None:
    """The bounded wrappers must be drop-in: same answers, bounded resources."""
    hashed = await security.hash_password_bounded("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert await security.verify_password_bounded("correct horse battery staple", hashed) is True
    assert await security.verify_password_bounded("wrong entirely", hashed) is False


async def test_bounded_verify_is_false_for_garbage_rather_than_raising() -> None:
    assert await security.verify_password_bounded("anything", "not-a-hash") is False


def test_the_gate_is_sized_from_the_declared_concurrency() -> None:
    """Peak hashing memory is this number times 64 MiB, so it is a memory bound."""
    assert security.PASSWORD_HASH_GATE.limit == rate_limit.PASSWORD_HASH_CONCURRENCY


async def test_real_argon2_work_never_exceeds_the_gate() -> None:
    """Measured on the real hasher, not on a stand-in.

    Sixteen simultaneous verifications, four slots: without the gate that is
    sixteen times 64 MiB resident at once, which is the whole attack. The bound is
    asserted from the gate's own high-water mark, and ``>= 2`` alongside it because
    a gate that had accidentally serialised everything would satisfy the ceiling
    while giving up all the throughput.
    """
    hashed = security.hash_password("correct horse battery staple")
    gate = security.PASSWORD_HASH_GATE

    results = await asyncio.gather(
        *[
            security.verify_password_bounded("correct horse battery staple", hashed)
            for _ in range(16)
        ]
    )

    assert all(results)
    assert gate.in_flight == 0
    assert 2 <= gate.peak_in_flight <= gate.limit
