"""Has this password appeared in a known breach? Asked without disclosing it.

The offline denylist in ``auth_service`` holds 26 entries. That catches ``password``
and ``123456`` and nothing else, while the passwords that actually get accounts taken
over are the ones a real person reused on a site that was breached -- and there are
hundreds of millions of those. No list we can ship covers them.

**k-anonymity is what makes asking safe.** SHA-1 the password, send the first FIVE
hex characters of the digest, receive every suffix sharing that prefix (typically
~500-800 of them) with a breach count each, and do the comparison locally. The
password never leaves this process, and neither does its full hash: the service
learns only that somebody, somewhere, was interested in one of ~800 hashes. SHA-1 is
correct here and is not a security choice -- it is the wire format the range API
speaks, and the hash is a lookup key, never a stored credential.

Three deliberate design decisions, because each has an appealing wrong answer:

* **A seam with a fake, and the network is OFF by default.** Same posture as every
  other provider in this codebase: nothing calls out unless it is configured to. So
  CI cannot reach the internet by accident, a developer's `make dev` does not depend
  on a third party being up, and switching it on is one environment variable.
* **It fails OPEN.** If the service is unreachable or slow, the password is accepted
  and the failure is logged. A third party's outage must not stop new customers
  signing up, and the offline denylist plus the length and distinct-character rules
  still apply as a floor. Failing closed would convert their downtime into ours.
* **A short timeout.** This runs on the signup path, which already spends a 64 MiB
  argon2 hash; adding an unbounded wait on someone else's HTTP service would make
  signup latency theirs to determine.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Final, Protocol

import httpx

logger: Final = logging.getLogger(__name__)

#: The range API. Documented, free, no key, no rate limit for this endpoint.
RANGE_URL: Final = "https://api.pwnedpasswords.com/range"

#: The prefix length the API defines. Not a tunable: 5 is what the endpoint accepts,
#: and it is also what sets the anonymity set size (~800 hashes per prefix).
PREFIX_LENGTH: Final = 5

#: Short, because this sits in front of a user waiting for a signup to complete.
DEFAULT_TIMEOUT_SECONDS: Final = 2.0

#: How many breach appearances make a password unacceptable.
#:
#: One, deliberately. A password in the corpus even once is a password that appears
#: in a wordlist an attacker already has, so "how many times" does not change what it
#: costs to guess. A higher threshold only exists to be lenient, and being lenient
#: about a known-breached password is the whole thing this module is for.
BREACH_THRESHOLD: Final = 1

#: The environment variable that turns the network on. Any of these means yes.
ENV_FLAG: Final = "PWNED_PASSWORD_CHECK"
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


class PwnedChecker(Protocol):
    """How many known breaches contain this password. ``0`` means none found."""

    async def breach_count(self, password: str) -> int: ...


def password_digest(password: str) -> tuple[str, str]:
    """``(prefix, suffix)`` of the uppercase SHA-1 hex digest.

    Split here rather than at the call site so the invariant that only the PREFIX is
    ever transmitted lives in one place and can be asserted by a test.
    """
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    return digest[:PREFIX_LENGTH], digest[PREFIX_LENGTH:]


def parse_range_response(body: str, suffix: str) -> int:
    """Find our suffix in a range response and return its count.

    The response is ``SUFFIX:COUNT`` per line. Parsed leniently: an unparseable line
    is skipped rather than raising, because one malformed row must not turn a
    successful lookup into an error that then fails open and checks nothing.
    """
    wanted = suffix.upper()
    for line in body.splitlines():
        candidate, _, count = line.strip().partition(":")
        if candidate.upper() != wanted:
            continue
        try:
            return int(count)
        except ValueError:
            # Our suffix is present but its count is malformed. Present is the part
            # that matters, so report the minimum that means "breached" rather than
            # discarding a real hit.
            return BREACH_THRESHOLD
    return 0


class OfflinePwnedChecker:
    """Reports nothing breached, without touching the network.

    The default, and what every test gets. It is not a stub that pretends to work:
    it answers "I found nothing", which is exactly what the caller must treat as
    inconclusive-but-allowed.
    """

    async def breach_count(self, password: str) -> int:
        return 0


class HttpPwnedPasswordsChecker:
    """The real range-API client."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._timeout = timeout_seconds

    async def breach_count(self, password: str) -> int:
        """The breach count, or ``0`` when the lookup could not be completed.

        Fails open by returning 0 -- see the module docstring. The failure is logged
        at WARNING with no password material and no digest, because a log line
        carrying either would defeat the point of k-anonymity.
        """
        prefix, suffix = password_digest(password)
        try:
            if self._client is not None:
                response = await self._client.get(f"{RANGE_URL}/{prefix}", timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(f"{RANGE_URL}/{prefix}")
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning(
                "pwned-password lookup failed, allowing the password: %s", type(exc).__name__
            )
            return 0
        return parse_range_response(response.text, suffix)


def get_checker(env: dict[str, str] | None = None) -> PwnedChecker:
    """The configured checker. Offline unless the flag is explicitly on.

    Default-off rather than default-on, so that adding this module cannot make a test
    suite or a local run start talking to the internet -- the same rule the model
    providers follow, and for the same reason.
    """
    source = os.environ if env is None else env
    if source.get(ENV_FLAG, "").strip().lower() in _TRUTHY:
        return HttpPwnedPasswordsChecker()
    return OfflinePwnedChecker()
