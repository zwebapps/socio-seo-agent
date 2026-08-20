"""Encryption at rest for a platform credential — and the one thing it must never do.

An OAuth access token is not a password. A password is only ever *compared*, so it is
hashed and the plaintext is thrown away; a token has to be **presented to Facebook**
later, so it must be recoverable. Hashing it is not "more secure", it is a token that
can never be used. So this module is a cipher, not a hash, and the whole design is
arranged around the two consequences of that:

**The key lives in the environment, never in the database.** A key stored beside the
ciphertext it protects protects nothing — one `SELECT` and both halves are gone. And it
cannot be DB-driven config either, for the plainest of reasons: the credential rows are
in the database the key would have to be read from. That is the same
bootstrap-secret-versus-operational-config line ``core/config.py`` already draws for
``session_secret``.

**Plaintext leaves this module in exactly one shape: a :class:`Secret`.** Its ``repr``
and ``str`` are masked, so a credential cannot reach a log line, an exception message, a
traceback local, or an f-string by accident — the three usual ways this data escapes. A
caller that genuinely needs the characters has to say :meth:`Secret.reveal`, which is
grep-able, and there are two places in the codebase allowed to say it.

What is deliberately NOT here, and why
--------------------------------------

**There is no AES-256-GCM implementation in this build, because ``cryptography`` is not a
dependency of this project and adding one requires updating a lockfile this change is not
permitted to touch.** The alternatives were considered and rejected:

* *hand-roll a cipher from ``hashlib``* — a keystream from SHA-256 plus an HMAC is
  exactly the sort of thing that looks like encryption in a diff and is not. Rolling an
  AEAD is worse than admitting there is none.
* *store the token in plaintext for now* — the failure mode is unbounded and permanent:
  every credential written before the "temporary" is removed stays readable forever, in
  every backup taken meanwhile.

So :func:`select_token_cipher` returns :class:`NotConfiguredCipher` on any machine that
has a key but no AEAD backend, and a :class:`NotConfiguredCipher` REFUSES to encrypt.
Refusing is the point — the connect flow then cannot store a credential at all, which is
the safe direction. The envelope format, the key parsing (:func:`decode_key`) and the
associated-data binding are all specified and tested here, so landing the real cipher is
a small, reviewable diff against a fixed format rather than a new design:

    ``v1.aesgcm:<base64(12-byte nonce)>:<base64(ciphertext||tag)>``

``aad`` is not decoration. AES-GCM authenticates it, so binding the envelope to
``business:{id}|platform:{name}`` means a ciphertext lifted out of one row and pasted
into another fails to open rather than silently authorising a post to somebody else's
account. :class:`EphemeralVaultCipher` enforces the same binding, so the property is
covered by tests today even though the AEAD is not implemented.

:class:`EphemeralVaultCipher` is what makes the connect/expire/refresh lifecycle
testable and locally usable without either of the rejected options: the secret stays in
process memory and the database holds only an opaque handle to it. It is not durable
storage and says so — restart the process and every credential is gone. That is a
correct dev/test seam and a useless production one, which is the honest shape for it.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, final
from uuid import UUID

__all__ = [
    "AES_GCM_SCHEME",
    "CREDENTIAL_KEY_ENV",
    "EPHEMERAL_KEY_VALUE",
    "EPHEMERAL_SCHEME",
    "CipherNotConfiguredError",
    "CipherStatus",
    "CredentialUnreadableError",
    "EphemeralVaultCipher",
    "NotConfiguredCipher",
    "Secret",
    "TokenCipher",
    "TokenCipherError",
    "cipher_status",
    "credential_aad",
    "decode_key",
    "mask_secret",
    "select_token_cipher",
]

#: Where the key comes from. Read straight from the environment rather than through
#: ``Settings``, and that is deliberate: a value that never enters the settings object
#: cannot be leaked by anything that serialises it — a debug endpoint, a startup log
#: line, an exception rendering `repr(settings)`.
CREDENTIAL_KEY_ENV: Final = "PLATFORM_CREDENTIAL_KEY"

#: The explicit opt-in to the in-process vault. A *value*, not a separate variable, so
#: there is one switch to read and no way to configure a key and a mode that disagree.
EPHEMERAL_KEY_VALUE: Final = "ephemeral"

#: Envelope scheme tags. Versioned from the start: the day a key is rotated or an
#: algorithm is replaced, the reader has to be able to tell which rows are which
#: without guessing from the length of a base64 blob.
AES_GCM_SCHEME: Final = "v1.aesgcm"
EPHEMERAL_SCHEME: Final = "v1.ephemeral"

#: AES-256 needs 32 bytes. Enforced when the key is parsed rather than when it is first
#: used, so a mistyped key is a startup-shaped failure and not a 3am one.
KEY_BYTES: Final = 32

#: Below this length a secret is short enough that a prefix and a suffix would be most
#: of it, so nothing is revealed at all. Real OAuth tokens are far longer; the branch
#: exists for the short values that turn up in tests and fixtures.
_MIN_MASKABLE_LENGTH: Final = 12
_MASK_EDGE: Final = 4


class TokenCipherError(Exception):
    """Base class for every failure in this module."""


class CipherNotConfiguredError(TokenCipherError):
    """No usable cipher, so no credential may be stored or read.

    Raised rather than returned because there is no sane fallback value: encrypting
    would have to mean "store it in the clear" and decrypting would have to mean
    "return something wrong". The caller's job is to refuse the operation and say why,
    which is what ``connection_service`` does with it.
    """


class CredentialUnreadableError(TokenCipherError):
    """The envelope exists but will not open.

    A wrong key, a truncated column, a row written by a scheme this build no longer has,
    or — the case worth naming — an envelope moved between rows, which fails the
    associated-data check. All of them mean the same thing operationally: this
    connection has to be re-established, and nothing may be published on it meanwhile.
    """


@final
class Secret:
    """A credential's plaintext, wrapped so it cannot print itself.

    ``__repr__`` and ``__str__`` both return the masked form, which covers the accidental
    disclosure paths that actually happen: ``logger.info("token=%s", token)``, an
    f-string in an error message, a dataclass ``repr`` that includes this field, and a
    traceback rendering local variables. ``reveal()`` is the only way out and reads like
    what it is at the call site.

    Not a dataclass on purpose — a generated ``repr`` is precisely the thing this class
    exists to prevent, and ``eq``/``hash`` are left off so a secret cannot be used as a
    dict key or compared with ``==`` (a plain ``==`` on credential material is also a
    timing oracle).
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The characters themselves. Two callers in this codebase; keep it that way."""
        return self._value

    def masked(self) -> str:
        """The prefix/suffix form that is safe to store, log and render."""
        return mask_secret(self._value)

    def __repr__(self) -> str:
        return f"Secret({self.masked()})"

    def __str__(self) -> str:
        return self.masked()


def mask_secret(value: str) -> str:
    """A hint that identifies a credential without disclosing it.

    Four leading and four trailing characters is enough for a human to match a row
    against a token they are holding, which is the only thing a hint is for. Anything
    shorter than :data:`_MIN_MASKABLE_LENGTH` reveals nothing, because on a short value
    eight characters is not a hint, it is the secret.
    """
    if len(value) < _MIN_MASKABLE_LENGTH:
        return "•" * 8
    return f"{value[:_MASK_EDGE]}…{value[-_MASK_EDGE:]}"


def credential_aad(*, business_id: UUID, platform: str) -> str:
    """The associated data an envelope is bound to.

    One function so the write and the read cannot drift: a mismatch here does not fail
    loudly at the point of the mistake, it fails as :class:`CredentialUnreadableError`
    on a row that looked fine yesterday.
    """
    return f"business:{business_id}|platform:{platform}"


class TokenCipher(Protocol):
    """Reversible protection for one credential.

    Narrow on purpose: no key management, no rotation, no storage. Rotation is a
    migration that reads with the old cipher and writes with the new one, and
    ``credential_scheme`` on the row is what makes that query possible.
    """

    @property
    def scheme(self) -> str:
        """The tag written into every envelope this cipher produces."""
        ...

    @property
    def protects_at_rest(self) -> bool:
        """Whether an attacker holding the database is denied the plaintext."""
        ...

    def encrypt(self, plaintext: Secret, *, aad: str) -> str:
        """Return an envelope safe to store. Raise on a cipher that cannot protect it."""
        ...

    def decrypt(self, envelope: str, *, aad: str) -> Secret:
        """Return the plaintext, or raise :class:`CredentialUnreadableError`."""
        ...


@final
class NotConfiguredCipher:
    """The default. Refuses both directions, and explains itself.

    The same posture as the model router's fake provider — a missing credential produces
    a stated, visible degradation rather than a crash or a silent unsafe path — with one
    difference that matters: a fake *model* can return canned text, whereas there is no
    such thing as a fake encryption that is safe to write to a real column. So this one
    refuses instead of substituting.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    @property
    def scheme(self) -> str:
        return "unconfigured"

    @property
    def protects_at_rest(self) -> bool:
        return False

    @property
    def reason(self) -> str:
        return self._reason

    def encrypt(self, plaintext: Secret, *, aad: str) -> str:
        raise CipherNotConfiguredError(self._reason)

    def decrypt(self, envelope: str, *, aad: str) -> Secret:
        raise CipherNotConfiguredError(self._reason)


@final
class EphemeralVaultCipher:
    """Keeps the plaintext in this process and stores only a handle to it.

    For development and tests. The database column holds
    ``v1.ephemeral:<random handle>`` and nothing else, so a dump of the table contains
    no credential material at all — which is exactly the property the real cipher is
    for, obtained here without a dependency and without hand-rolling an AEAD.

    Its cost is stated rather than hidden: **secrets do not survive a restart**, and
    they are not shared between processes, so a connection made under this cipher is
    unusable to a second worker and gone after a deploy. That makes it useless in
    production and honest in a test.

    The handle is generated with ``secrets``, not ``random``, so one handle cannot be
    guessed from another — a predictable handle would make the vault readable by anyone
    who can call ``decrypt`` with a made-up envelope.
    """

    def __init__(self) -> None:
        self._vault: dict[str, tuple[str, str]] = {}

    @property
    def scheme(self) -> str:
        return EPHEMERAL_SCHEME

    @property
    def protects_at_rest(self) -> bool:
        # True in the narrow sense that matters: the persisted column carries no secret.
        return True

    def encrypt(self, plaintext: Secret, *, aad: str) -> str:
        handle = secrets.token_urlsafe(24)
        self._vault[handle] = (aad, plaintext.reveal())
        return f"{EPHEMERAL_SCHEME}:{handle}"

    def decrypt(self, envelope: str, *, aad: str) -> Secret:
        scheme, _, handle = envelope.partition(":")
        if scheme != EPHEMERAL_SCHEME or not handle:
            raise CredentialUnreadableError(
                f"not an {EPHEMERAL_SCHEME} envelope: {envelope[:16]!r}..."
            )
        entry = self._vault.get(handle)
        if entry is None:
            raise CredentialUnreadableError(
                "this credential was held in process memory and is gone (the vault does "
                "not survive a restart). The connection has to be re-established."
            )
        stored_aad, plaintext = entry
        if stored_aad != aad:
            # The same check AES-GCM's authenticated associated data would make: an
            # envelope pasted into another business's row must not open.
            raise CredentialUnreadableError(
                "this credential is bound to a different business or platform"
            )
        return Secret(plaintext)


def decode_key(raw: str) -> bytes:
    """Parse a 32-byte key from base64 or hex. Raises ``ValueError`` on anything else.

    Both encodings are accepted because both are what people actually produce —
    ``openssl rand -base64 32`` and ``openssl rand -hex 32`` — and rejecting one of them
    for tidiness means an operator "fixing" the format by hand.

    Implemented and tested now even though the cipher that consumes it is not, so the
    key-handling half of this module is real: length and encoding are the two things a
    misconfigured deployment gets wrong, and both should fail at parse time.

    Both decoders are tried and the results FILTERED by length, rather than the first
    successful decode winning. That is not fussiness: a 64-character hex key is also
    valid base64 (the hex alphabet is a subset, and 64 is divisible by four), so
    first-decode-wins reads a perfectly good ``openssl rand -hex 32`` key as 48 base64
    bytes and rejects it for being the wrong length.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("the key is empty")

    decoded = [key for key in (_from_base64(candidate), _from_hex(candidate)) if key is not None]
    if not decoded:
        raise ValueError("the key is neither valid base64 nor valid hex")

    for key in decoded:
        if len(key) == KEY_BYTES:
            return key

    raise ValueError(
        f"a {KEY_BYTES}-byte key is required for AES-256; this one decodes to "
        f"{', '.join(str(len(key)) for key in decoded)} bytes"
    )


def _from_base64(candidate: str) -> bytes | None:
    try:
        return base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None


def _from_hex(candidate: str) -> bytes | None:
    try:
        return bytes.fromhex(candidate)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class CipherStatus:
    """What protection is actually in force, for the surface that has to say so.

    Exists for the same reason ``llm.router.config_status`` does: a screen that cannot
    distinguish "credentials are encrypted" from "credentials cannot be stored at all"
    leaves an operator guessing, and this is not a thing to guess about.
    """

    scheme: str
    protects_at_rest: bool
    can_store_credentials: bool
    message: str


def select_token_cipher(env: Mapping[str, str] | None = None) -> TokenCipher:
    """The cipher this process will use, decided from the environment alone.

    Three outcomes, and the default is the strict one:

    * ``PLATFORM_CREDENTIAL_KEY=ephemeral`` — the in-process vault. Explicit, because
      nothing should get a non-durable credential store by accident.
    * a real key — currently :class:`NotConfiguredCipher`, because this build has no
      AEAD implementation to hand the key to (see the module docstring). The key is
      still parsed, so a malformed one is reported as malformed rather than as "not
      configured", which are different problems with different fixes.
    * nothing set — :class:`NotConfiguredCipher`. Connecting a platform will refuse, and
      that is the intended behaviour: with no key there is nowhere safe to put a token.
    """
    environ = env if env is not None else os.environ
    raw = environ.get(CREDENTIAL_KEY_ENV, "").strip()

    if raw == EPHEMERAL_KEY_VALUE:
        return EphemeralVaultCipher()

    if not raw:
        return NotConfiguredCipher(
            f"{CREDENTIAL_KEY_ENV} is not set, so a platform credential cannot be "
            "encrypted and will not be stored. Set it to a 32-byte base64 or hex key "
            f"in staging and production, or to {EPHEMERAL_KEY_VALUE!r} for local "
            "development (in-process, not durable)."
        )

    try:
        decode_key(raw)
    except ValueError as exc:
        return NotConfiguredCipher(
            f"{CREDENTIAL_KEY_ENV} is set but unusable: {exc}. Generate one with "
            "`openssl rand -base64 32`."
        )

    return NotConfiguredCipher(
        f"{CREDENTIAL_KEY_ENV} is a valid key, but this build has no AES-256-GCM "
        "backend: `cryptography` is not a dependency of this project. Until it is "
        "added and AesGcmCipher lands, a platform credential cannot be protected at "
        "rest and will not be stored."
    )


def cipher_status(cipher: TokenCipher) -> CipherStatus:
    """Describe ``cipher`` for a status endpoint or an admin screen."""
    if isinstance(cipher, NotConfiguredCipher):
        return CipherStatus(
            scheme=cipher.scheme,
            protects_at_rest=False,
            can_store_credentials=False,
            message=cipher.reason,
        )
    if isinstance(cipher, EphemeralVaultCipher):
        return CipherStatus(
            scheme=cipher.scheme,
            protects_at_rest=True,
            can_store_credentials=True,
            message=(
                "Platform credentials are held in this process and the database stores "
                "only a handle. Nothing survives a restart -- development only."
            ),
        )
    return CipherStatus(
        scheme=cipher.scheme,
        protects_at_rest=cipher.protects_at_rest,
        can_store_credentials=True,
        message=f"Platform credentials are encrypted at rest with {cipher.scheme}.",
    )
