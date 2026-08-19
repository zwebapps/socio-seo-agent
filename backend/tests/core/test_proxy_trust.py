"""``core/proxy_trust``: can this deployment tell one client from another?"""

from __future__ import annotations

from backend.app.core.proxy_trust import WILDCARD, forwarded_trust_warning


def test_a_forwarded_header_that_was_discarded_is_reported() -> None:
    """The real bug: the proxy said who the client is and the server ignored it.

    ``FORWARDED_ALLOW_IPS`` defaults to ``127.0.0.1``, which is wrong for the
    ordinary container topology where the proxy is a separate service. The header
    arrives, is not trusted, and the peer stays the proxy -- so every client shares
    one rate-limit bucket and nothing errors.
    """
    warning = forwarded_trust_warning(
        client_host="172.18.0.5",  # the proxy container
        forwarded_for="203.0.113.9",  # the actual diner
        forwarded_allow_ips="127.0.0.1",
    )

    assert warning is not None
    assert "sharing one rate-limit bucket" in warning
    assert "172.18.0.5" in warning, "the message must name the peer, or it is unactionable"
    assert "FORWARDED_ALLOW_IPS" in warning


def test_a_trusted_forwarded_header_produces_no_warning() -> None:
    """Correctly configured: the server has already rewritten the peer to the client."""
    assert (
        forwarded_trust_warning(
            client_host="203.0.113.9",
            forwarded_for="203.0.113.9",
            forwarded_allow_ips="172.18.0.5",
        )
        is None
    )


def test_a_chain_of_proxies_compares_the_originating_address() -> None:
    """``X-Forwarded-For`` is a list, and the client is the FIRST entry."""
    assert (
        forwarded_trust_warning(
            client_host="203.0.113.9",
            forwarded_for="203.0.113.9, 172.18.0.5, 10.0.0.2",
            forwarded_allow_ips="172.18.0.5",
        )
        is None
    )


def test_no_proxy_at_all_is_not_a_misconfiguration() -> None:
    """Otherwise every local `make dev` run would emit a warning nobody can fix."""
    assert (
        forwarded_trust_warning(
            client_host="127.0.0.1", forwarded_for=None, forwarded_allow_ips=None
        )
        is None
    )


def test_the_wildcard_is_reported_even_when_everything_looks_fine() -> None:
    """The trap in the obvious fix, and it is worse than the bug it fixes.

    Trusting X-Forwarded-For from anyone means a client can claim any address, so the
    rate limit is not shared -- it is evaded, by varying one header. Reported even
    though the peer and the forwarded address agree, because on this setting they
    always will.
    """
    warning = forwarded_trust_warning(
        client_host="203.0.113.9",
        forwarded_for="203.0.113.9",
        forwarded_allow_ips=WILDCARD,
    )

    assert warning is not None
    assert "evade the rate limit" in warning


def test_the_wildcard_is_reported_with_no_forwarded_header_present() -> None:
    """It is a configuration fact, not a per-request observation."""
    assert (
        forwarded_trust_warning(
            client_host="127.0.0.1", forwarded_for=None, forwarded_allow_ips=" * "
        )
        is not None
    )


def test_a_missing_peer_address_is_not_guessed_about() -> None:
    """A test transport or a unix socket has no peer. A warning here is noise."""
    assert (
        forwarded_trust_warning(
            client_host=None, forwarded_for="203.0.113.9", forwarded_allow_ips="127.0.0.1"
        )
        is None
    )


def test_an_empty_forwarded_header_is_not_a_claim() -> None:
    """Some proxies set the header and leave it blank."""
    assert (
        forwarded_trust_warning(
            client_host="172.18.0.5", forwarded_for="   ", forwarded_allow_ips="127.0.0.1"
        )
        is None
    )
