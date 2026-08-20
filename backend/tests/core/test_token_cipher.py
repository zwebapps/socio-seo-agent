"""The credential cipher, and the property the rest of the feature rests on.

The claim under test is narrow and worth stating: **a platform credential is never
readable from anything except a deliberate decrypt.** Not from a stored column, not from
a ``repr``, not from an f-string, not from a log line, and not from an envelope moved to
another business's row.

The AES-256-GCM implementation is deliberately absent (``cryptography`` is not a
dependency of this project), so what is asserted here is the seam: the key parsing, the
masking, the associated-data binding, and the fact that an unconfigured cipher REFUSES
rather than storing anything in the clear.
"""

import base64
import logging
import secrets
from uuid import UUID, uuid4

import pytest

from backend.app.core.token_cipher import (
    AES_GCM_SCHEME,
    CREDENTIAL_KEY_ENV,
    AesGcmCipher,
    CipherNotConfiguredError,
    CredentialUnreadableError,
    EphemeralVaultCipher,
    NotConfiguredCipher,
    Secret,
    cipher_status,
    credential_aad,
    decode_key,
    mask_secret,
    select_token_cipher,
)

TOKEN = "EAAGm0PX4ZCpsBA-a-very-long-page-access-token-value"


def test_a_secret_never_prints_itself() -> None:
    """repr, str and f-string interpolation must all be masked.

    All three, because they are three different code paths in CPython and only one of
    them is what a careless log line uses. A class that masked `repr` and inherited
    `object.__str__` would leak through `logger.info("%s", secret)`.
    """
    secret = Secret(TOKEN)

    assert TOKEN not in repr(secret)
    assert TOKEN not in str(secret)
    assert TOKEN not in f"{secret}"
    assert TOKEN not in f"{secret!r}"
    assert secret.reveal() == TOKEN


def test_a_secret_inside_a_dataclass_repr_is_masked() -> None:
    """The path that actually leaks: a generated repr on the object holding the secret."""
    from backend.app.services.platform_oauth import TokenGrant

    grant = TokenGrant(external_account_id="acct", access_token=Secret(TOKEN))
    assert TOKEN not in repr(grant)


def test_a_secret_does_not_reach_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """The realistic accident, asserted through the logging machinery rather than by eye."""
    logger = logging.getLogger("test.token_cipher")
    with caplog.at_level(logging.INFO):
        logger.info("credential=%s", Secret(TOKEN))

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN not in rendered
    assert "EAAG…alue" in rendered


def test_mask_reveals_the_edges_of_a_long_secret_and_nothing_of_a_short_one() -> None:
    assert mask_secret(TOKEN) == "EAAG…alue"
    # Short enough that eight characters would be most of the secret, so nothing is shown.
    assert mask_secret("short") == "••••••••"
    assert "short" not in mask_secret("short")


def test_the_ephemeral_vault_persists_no_secret_material() -> None:
    """The envelope is the whole of what a database row would hold."""
    cipher = EphemeralVaultCipher()
    aad = credential_aad(business_id=uuid4(), platform="linkedin")

    envelope = cipher.encrypt(Secret(TOKEN), aad=aad)

    assert TOKEN not in envelope
    assert envelope.startswith("v1.ephemeral:")
    assert cipher.decrypt(envelope, aad=aad).reveal() == TOKEN


def test_an_envelope_will_not_open_under_different_associated_data() -> None:
    """A ciphertext lifted into another business's row must fail, not authorise a post.

    This is the property AES-GCM's authenticated associated data provides and the reason
    `credential_aad` exists at all. Asserting it against the ephemeral vault means the
    behaviour is covered today, before the real AEAD lands.
    """
    cipher = EphemeralVaultCipher()
    business_a, business_b = uuid4(), uuid4()
    envelope = cipher.encrypt(
        Secret(TOKEN), aad=credential_aad(business_id=business_a, platform="linkedin")
    )

    with pytest.raises(CredentialUnreadableError):
        cipher.decrypt(envelope, aad=credential_aad(business_id=business_b, platform="linkedin"))

    # Same business, different platform: also a different binding.
    with pytest.raises(CredentialUnreadableError):
        cipher.decrypt(envelope, aad=credential_aad(business_id=business_a, platform="facebook"))


def test_a_forged_or_stale_envelope_is_unreadable() -> None:
    cipher = EphemeralVaultCipher()
    aad = credential_aad(business_id=uuid4(), platform="linkedin")

    with pytest.raises(CredentialUnreadableError):
        cipher.decrypt("v1.ephemeral:handle-nobody-issued", aad=aad)

    with pytest.raises(CredentialUnreadableError):
        cipher.decrypt("not-an-envelope", aad=aad)

    # A scheme this cipher does not speak. Refusing beats guessing.
    with pytest.raises(CredentialUnreadableError):
        cipher.decrypt("v1.aesgcm:AAAA:BBBB", aad=aad)


def test_an_unconfigured_cipher_refuses_both_directions() -> None:
    """Refusal, not a plaintext fallback: with no key there is nowhere safe to put a token."""
    cipher = NotConfiguredCipher("no key configured")
    aad = credential_aad(business_id=uuid4(), platform="linkedin")

    with pytest.raises(CipherNotConfiguredError):
        cipher.encrypt(Secret(TOKEN), aad=aad)
    with pytest.raises(CipherNotConfiguredError):
        cipher.decrypt("v1.aesgcm:AAAA:BBBB", aad=aad)

    status = cipher_status(cipher)
    assert status.can_store_credentials is False
    assert status.protects_at_rest is False


def test_decode_key_accepts_both_encodings_openssl_produces() -> None:
    raw = bytes(range(32))

    assert decode_key(base64.b64encode(raw).decode()) == raw
    # A 64-character hex key is ALSO valid base64, so a first-decode-wins implementation
    # reads this as 48 bytes and rejects a perfectly good key. That regression is what
    # this line exists for.
    assert decode_key(raw.hex()) == raw
    assert decode_key(f"  {raw.hex()}  ") == raw


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not base64 or hex!!",
        base64.b64encode(b"too-short").decode(),
        (b"x" * 33).hex(),
    ],
)
def test_decode_key_rejects_anything_that_is_not_a_32_byte_key(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_key(bad)


def test_no_key_selects_the_cipher_that_refuses() -> None:
    cipher = select_token_cipher({})
    assert isinstance(cipher, NotConfiguredCipher)
    assert CREDENTIAL_KEY_ENV in cipher.reason


def test_the_ephemeral_vault_is_explicit_opt_in_only() -> None:
    """It has to be asked for by name: nothing should get a non-durable store by accident."""
    assert isinstance(select_token_cipher({CREDENTIAL_KEY_ENV: "ephemeral"}), EphemeralVaultCipher)


def test_a_real_key_produces_a_real_cipher_and_never_echoes_the_key() -> None:
    """Rewritten, not deleted: this used to assert that a valid key produced a REFUSING
    cipher, because `cryptography` was not a dependency and there was no AEAD to hand it
    to. That refusal was correct and it was not a feature — it meant no platform
    credential could be stored at all.

    What survives from the old test is the half that is still a rule: a key must never
    appear in a message, whatever the outcome. `NotConfiguredCipher` remains reachable
    for a missing or malformed key (the two tests below) and for an environment where
    `cryptography` is somehow not importable.
    """
    key = base64.b64encode(bytes(range(32))).decode()
    cipher = select_token_cipher({CREDENTIAL_KEY_ENV: key})

    assert isinstance(cipher, AesGcmCipher)
    assert cipher.protects_at_rest is True
    assert key not in repr(cipher), "the key itself must never appear in a message"
    assert key not in cipher_status(cipher).message


def test_a_malformed_key_says_so_rather_than_reporting_nothing_configured() -> None:
    cipher = select_token_cipher({CREDENTIAL_KEY_ENV: "obviously-not-a-key"})
    assert isinstance(cipher, NotConfiguredCipher)
    assert "unusable" in cipher.reason


# --------------------------------------------------------------------------- #
# AES-256-GCM: the real cipher, now that `cryptography` is a dependency
# --------------------------------------------------------------------------- #


class TestAesGcmCipher:
    """The cipher that actually protects a credential at rest.

    Until `cryptography` was added, `select_token_cipher` returned
    `NotConfiguredCipher` for a valid key and refused to store anything — which was the
    right refusal and not a feature. These tests are what make the replacement
    trustworthy rather than merely present.
    """

    @staticmethod
    def _cipher() -> AesGcmCipher:
        return AesGcmCipher(secrets.token_bytes(32))

    def test_a_real_key_now_selects_the_real_cipher(self) -> None:
        """The whole point of the change: a configured key no longer refuses."""
        cipher = select_token_cipher({CREDENTIAL_KEY_ENV: base64.b64encode(b"k" * 32).decode()})

        assert isinstance(cipher, AesGcmCipher)
        assert cipher.protects_at_rest is True
        assert cipher.scheme == AES_GCM_SCHEME

    def test_the_column_never_holds_the_credential(self) -> None:
        """The property every other guarantee here rests on. A dump of the table has to
        be useless to whoever reads it."""
        cipher = self._cipher()
        token = "ya29.a-very-distinctive-access-token"

        envelope = cipher.encrypt(Secret(token), aad="business:x|platform:linkedin")

        assert token not in envelope
        assert envelope.startswith(f"{AES_GCM_SCHEME}:")

    def test_it_round_trips(self) -> None:
        cipher = self._cipher()
        aad = "business:x|platform:linkedin"

        envelope = cipher.encrypt(Secret("token-value"), aad=aad)

        assert cipher.decrypt(envelope, aad=aad).reveal() == "token-value"

    def test_every_encryption_uses_a_fresh_nonce(self) -> None:
        """Not a nice-to-have. GCM does not degrade on nonce reuse under one key, it
        COLLAPSES — reusing one leaks the plaintext of both messages. A counter would be
        the standard alternative and is wrong for this shape: these rows are written by
        several processes with no shared state to count in."""
        cipher = self._cipher()
        aad = "business:x|platform:linkedin"

        envelopes = {cipher.encrypt(Secret("same-token"), aad=aad) for _ in range(25)}

        assert len(envelopes) == 25, "a repeated nonce would show up as a repeated envelope"

    def test_an_envelope_moved_to_another_business_will_not_open(self) -> None:
        """The reason the aad binding exists. Without it, a ciphertext lifted out of one
        row and pasted into another would silently authorise a post to somebody else's
        account."""
        cipher = self._cipher()
        envelope = cipher.encrypt(
            Secret("token"), aad=credential_aad(business_id=UUID(int=1), platform="linkedin")
        )

        with pytest.raises(CredentialUnreadableError):
            cipher.decrypt(
                envelope, aad=credential_aad(business_id=UUID(int=2), platform="linkedin")
            )

    def test_an_envelope_moved_to_another_platform_will_not_open(self) -> None:
        """Same binding, the other half: a LinkedIn token must not be usable as the
        Facebook one for the same business."""
        cipher = self._cipher()
        business = UUID(int=1)
        envelope = cipher.encrypt(
            Secret("token"), aad=credential_aad(business_id=business, platform="linkedin")
        )

        with pytest.raises(CredentialUnreadableError):
            cipher.decrypt(envelope, aad=credential_aad(business_id=business, platform="facebook"))

    def test_a_tampered_ciphertext_is_refused_rather_than_returning_rubbish(self) -> None:
        """This is what the authenticated part of AEAD buys: a modified ciphertext fails
        to open instead of decrypting to something an attacker chose."""
        cipher = self._cipher()
        aad = "business:x|platform:linkedin"
        envelope = cipher.encrypt(Secret("token"), aad=aad)

        with pytest.raises(CredentialUnreadableError):
            cipher.decrypt(envelope[:-6] + "AAAAAA", aad=aad)

    def test_a_rotated_key_cannot_read_the_old_rows(self) -> None:
        """Stated as a test because it is an operational fact somebody will meet: rotating
        the key invalidates every stored credential, and the answer is to re-connect the
        accounts. The error says exactly that."""
        aad = "business:x|platform:linkedin"
        envelope = self._cipher().encrypt(Secret("token"), aad=aad)

        with pytest.raises(CredentialUnreadableError, match="Re-connect"):
            self._cipher().decrypt(envelope, aad=aad)

    def test_every_failure_gives_the_same_message(self) -> None:
        """Deliberate. A wrong key, a tampered blob and a mismatched aad have the same
        fix — re-connect — and distinguishing them for the caller would distinguish them
        for an attacker too."""
        cipher = self._cipher()
        aad = "business:x|platform:linkedin"
        envelope = cipher.encrypt(Secret("token"), aad=aad)

        messages = set()
        for broken_aad, broken_envelope in (
            ("business:other|platform:linkedin", envelope),
            (aad, envelope[:-6] + "AAAAAA"),
            (aad, f"{AES_GCM_SCHEME}:AAAA:AAAA"),
        ):
            with pytest.raises(CredentialUnreadableError) as caught:
                cipher.decrypt(broken_envelope, aad=broken_aad)
            messages.add(str(caught.value))

        assert len(messages) == 1, f"failures must be indistinguishable, got {messages}"

    def test_a_non_aesgcm_envelope_is_named_rather_than_mis_decoded(self) -> None:
        """An ephemeral-scheme row read by the real cipher after a config change. Saying
        which scheme it is beats a generic decrypt failure, because the fix is different:
        that credential was never durable."""
        cipher = self._cipher()

        with pytest.raises(CredentialUnreadableError, match=AES_GCM_SCHEME):
            cipher.decrypt("v1.ephemeral:some-handle", aad="business:x|platform:linkedin")
