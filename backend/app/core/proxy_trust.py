"""Whether this process can tell one client from another behind a proxy.

The rate limiter and the abuse controls key on the client's address. Behind a
reverse proxy the socket peer IS the proxy, so every request in the world arrives
from one address unless the proxy's ``X-Forwarded-For`` is trusted -- at which
point 20,000 diners share a single 30-per-5-minutes bucket and the first one to
misbehave locks out everybody.

Two facts about the installed server decide the shape of this module, and both were
checked rather than remembered (uvicorn 0.52.3):

* **``--proxy-headers`` is already ON by default.** So the flag is not the thing
  that needs adding, and a deployment note telling someone to pass it would be
  cargo cult.
* **``--forwarded-allow-ips`` defaults to ``FORWARDED_ALLOW_IPS`` in the
  environment, or ``127.0.0.1``.** That default is the actual trap. It is right for
  a proxy sharing the network namespace and WRONG for the ordinary container
  topology, where the proxy is a separate service with its own address -- so
  ``X-Forwarded-For`` arrives, is not trusted, and is silently discarded.

The failure is invisible: nothing errors, the rate limiter works, and it works
against the wrong subject. Hence :func:`forwarded_trust_warning`, which detects the
exact fingerprint at runtime -- a forwarded header present, and a client address
that is still the proxy's.

**Never set ``FORWARDED_ALLOW_IPS=*``.** It is the fix that looks easiest and is
worse than the bug: trusting a forwarded header from anyone lets any client claim
any address, so the rate limit is not merely shared, it is trivially evaded by
sending a different ``X-Forwarded-For`` each time. Name the proxy.
"""

from __future__ import annotations

from typing import Final

#: The wildcard nobody should deploy. Checked as a value, not as advice in a
#: comment, so :func:`forwarded_trust_warning` can say so out loud.
WILDCARD: Final = "*"

_SHARED_BUCKET_WARNING: Final = (
    "Client addresses are not being resolved from X-Forwarded-For: the header is "
    "present but the peer address (%s) is not in FORWARDED_ALLOW_IPS (%s). Every "
    "client is therefore sharing one rate-limit bucket, so one abusive caller locks "
    "out all of them. Set FORWARDED_ALLOW_IPS to the proxy's address."
)

_WILDCARD_WARNING: Final = (
    "FORWARDED_ALLOW_IPS is '*', which trusts X-Forwarded-For from any source. A "
    "client can then claim any address it likes and evade the rate limit entirely by "
    "varying the header -- which is worse than sharing one bucket, not better. Name "
    "the proxy's address instead."
)


def forwarded_trust_warning(
    *,
    client_host: str | None,
    forwarded_for: str | None,
    forwarded_allow_ips: str | None,
) -> str | None:
    """The misconfiguration this deployment has, or ``None`` if it has neither.

    Pure, so it is testable without a server, a proxy, or a socket. The caller
    supplies what it observed; this decides what that means.

    ``client_host`` is the address the ASGI server reports AFTER it has applied
    whatever forwarded headers it trusts. That is the whole trick: if the header was
    trusted, this is the real client and there is nothing to warn about. If it still
    equals the proxy, the header was discarded.
    """
    trusted = (forwarded_allow_ips or "").strip()

    if trusted == WILDCARD:
        return _WILDCARD_WARNING

    if not forwarded_for:
        # No proxy in front, or a proxy that sets nothing. Either way there is no
        # forwarded address being ignored, so there is nothing to report. A warning
        # here would fire on every direct-to-uvicorn local run.
        return None

    if client_host is None:
        # No peer address at all -- a test transport, or a unix socket. Nothing to
        # compare, and guessing would produce a warning nobody can act on.
        return None

    claimed = forwarded_for.split(",")[0].strip()
    if claimed and claimed != client_host:
        return _SHARED_BUCKET_WARNING % (client_host, trusted or "127.0.0.1")

    # The forwarded address and the peer agree, which is what it looks like when the
    # header WAS applied: the server has already rewritten the peer to the client.
    return None
