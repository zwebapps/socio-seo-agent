"""Address shape and address redaction, shared by every mail-shaped actuator.

Two actuators send mail — `email.py` (`notify.email`, marketing, consent-bearing) and
`owner_notice.py` (`notify.owner`, transactional, sent to the account holder) — and the
rules that differ between them are exactly the interesting part. What must NOT differ is
this: what counts as a usable address, and how an address is turned into something safe
to put in a log.

That is why these three functions live here rather than being written twice. A second
copy of `looks_like_address` would drift, and the drift would be invisible: the two
modules would then disagree about whether a sender identity is present, so
"`notify.owner` is refused when the sender is missing" and the same sentence about
`notify.email` would be two different claims. One function, one answer, one place to fix.

`email.py` re-exports `recipient_fingerprint` over `address_fingerprint` so its own
public surface is unchanged.
"""

from __future__ import annotations

import hashlib

__all__ = [
    "address_fingerprint",
    "address_handle",
    "identity_address",
    "looks_like_address",
]


def looks_like_address(value: str) -> bool:
    """Deliberately shallow: exactly one `@`, and something either side of it.

    Not an RFC 5322 parser and not trying to be. Full validation is a famous rabbit hole
    whose reward is rejecting addresses that work, and the provider is the real authority
    -- a 422 from Resend is mapped non-retryable precisely so this function does not have
    to be right about the hard cases. It only has to catch an obviously empty or
    malformed field before we pay for a round trip.
    """
    local, _, domain = value.partition("@")
    if not local or not domain or "@" in domain:
        return False
    return "." in domain and not domain.startswith(".") and not domain.endswith(".")


def identity_address(sender: str) -> str:
    """The address inside a sender identity, accepting `Name <addr@domain>`.

    A display name is part of identifying yourself rather than an obstacle to it, so it
    is unwrapped rather than refused. Returns the input unchanged when there is no
    `<...>` to unwrap, which leaves the shape decision to `looks_like_address`.
    """
    if "<" in sender and sender.rstrip().endswith(">"):
        return sender[sender.index("<") + 1 : sender.rstrip().rindex(">")].strip()
    return sender


def address_fingerprint(address: str) -> str:
    """A short, stable handle for one address, for logs, metrics and targets.

    **Honest about what this is.** It is a CORRELATION handle, not anonymisation: an
    address has far too little entropy for a bare digest to resist a dictionary attack,
    exactly as `core/rate_limit.py` says of the same trick. What it buys is that a log
    line contains no address, so a shipped log is not an address book -- while two sends
    to the same person can still be tied together when debugging.

    `rate_limit` can key its HMAC on the session secret because it is already holding
    settings; the actuators deliberately hold no configuration beyond one API key, so the
    digest is unkeyed and this docstring says so rather than implying more.
    """
    normalised = address.strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]


def address_handle(prefix: str, address: str) -> str:
    """An `Actuation.target` for one address: a prefixed handle, never the address.

    `actuate()` LOGS `target` and `Outcome.summary()` interpolates it into a line meant
    for a timeline, and that line is persisted into `runs.checkpoint`. So an address in
    `target` cannot be kept out of a log from inside an actuator, and the handle is the
    fix rather than a preference.

    Prefixed so a human reading the `actions` table can tell at a glance that this is a
    derived handle and not a truncated address -- and so a raw address in the column is
    obvious on sight. Being a pure function of the address, it keeps every idempotency
    property `contract.py` asks of a target: two recipients are two keys, and the same
    body to the same person is a replay.
    """
    return f"{prefix}{address_fingerprint(address)}"
