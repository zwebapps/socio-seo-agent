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
from uuid import uuid4

import pytest

from backend.app.core.token_cipher import (
    CREDENTIAL_KEY_ENV,
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


def test_a_real_key_is_reported_as_having_no_backend_rather_than_as_missing() -> None:
    """The operator has to be able to tell "you forgot the key" from "this build cannot use it".

    Both currently produce a refusing cipher, and the fix for each is different: one is a
    deployment variable, the other is adding `cryptography` and the AES implementation.
    A single opaque message would send someone to check the wrong thing.
    """
    key = base64.b64encode(bytes(range(32))).decode()
    cipher = select_token_cipher({CREDENTIAL_KEY_ENV: key})

    assert isinstance(cipher, NotConfiguredCipher)
    assert "cryptography" in cipher.reason
    assert key not in cipher.reason, "the key itself must never appear in a message"


def test_a_malformed_key_says_so_rather_than_reporting_nothing_configured() -> None:
    cipher = select_token_cipher({CREDENTIAL_KEY_ENV: "obviously-not-a-key"})
    assert isinstance(cipher, NotConfiguredCipher)
    assert "unusable" in cipher.reason
