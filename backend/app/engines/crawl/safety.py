"""SSRF guard and robots.txt parsing. Pure, synchronous, injectable.

Crawled URLs are attacker-controllable in the most direct way possible: a
customer types their website address, and a competitor or an attacker can put any
`href` on any page we then follow. So this module is the boundary control named
in docs/ARCHITECTURE.md section 9, and it is written to survive the three ways
that control is normally bypassed:

1.  **Blocklisting hostname strings.** Useless on its own: `localtest.me`
    resolves to `127.0.0.1`, and anyone can point a public DNS name at any
    address they like. We therefore resolve the hostname and judge the *resolved
    IP*, via the `ipaddress` module, and reject if **any** returned address is
    unsafe (a multi-record answer must not be a way through).
2.  **Redirects.** A safe first hop can 302 to `http://169.254.169.254/`. The
    fetcher never lets httpx follow redirects; it re-runs this guard on every
    hop, bounded by `MAX_REDIRECTS`.
3.  **Non-HTTP schemes.** `file:///etc/passwd`, `gopher://`, `ftp://` and the
    rest are refused before anything else happens.

DNS is injected (`Resolver`) rather than called directly, which is what makes the
test suite hermetic — no test in this repo performs real name resolution.

Known limitation, recorded rather than hidden: between our resolution and httpx's
own resolution there is a TOCTOU window (classic DNS rebinding). Closing it
properly means pinning the connection to the validated IP, which breaks TLS SNI
and certificate validation unless the transport is customised. Until that is
worth doing, the mitigations are that *every* answer must be safe and *every* hop
is re-checked, which removes the cheap version of the attack.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .contract import UnsafeUrlError

#: Resolve a hostname to a list of IP strings. Anything with this shape can be
#: injected, which is how tests avoid real DNS.
Resolver = Callable[[str], Sequence[str]]

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Belt-and-braces: every one of these is already caught by the range checks
#: below (they are link-local or CGNAT), but naming them makes the refusal
#: message unambiguous and makes the intent greppable during a security review.
CLOUD_METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / GCP / DigitalOcean / Oracle
        "fd00:ec2::254",  # AWS IMDS over IPv6
        "100.100.100.200",  # Alibaba Cloud
    }
)

MAX_REDIRECTS = 5

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def system_resolver(host: str) -> list[str]:
    """Default resolver: the operating system's, via `socket.getaddrinfo`.

    Blocking. `fetch.py` calls the guard in a worker thread so a slow lookup
    cannot stall the event loop.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:  # pragma: no cover - requires real DNS to exercise
        raise UnsafeUrlError(f"cannot resolve host {host!r}: {exc}") from exc
    return [str(info[4][0]) for info in infos]


def classify_blocked_ip(ip: _IpAddress) -> str | None:
    """Return why `ip` is off limits, or `None` if it is a fine public address.

    A string reason rather than a bool so refusals are self-explaining in logs
    and in the UI ("refused: loopback address" beats "refused").
    """
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) is just an IPv4 address
    # wearing a hat; judge the address it actually reaches.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if str(ip) in CLOUD_METADATA_ADDRESSES:
        return "cloud metadata address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    return None


def resolve_safe_ips(host: str, *, resolver: Resolver | None = None) -> list[_IpAddress]:
    """Resolve `host` and return its addresses, or raise if any of them is unsafe.

    Rejecting on *any* unsafe answer (rather than requiring all of them to be
    unsafe, or picking the first) is deliberate: a hostname answering
    `[93.184.216.34, 127.0.0.1]` is an attack, not a fallback.
    """
    resolve = resolver if resolver is not None else system_resolver

    try:
        raw = list(resolve(host))
    except UnsafeUrlError:
        raise
    except Exception as exc:  # an injected or system resolver may fail any way
        raise UnsafeUrlError(f"cannot resolve host {host!r}: {exc}") from exc

    if not raw:
        raise UnsafeUrlError(f"host {host!r} did not resolve to any address")

    addresses: list[_IpAddress] = []
    for candidate in raw:
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise UnsafeUrlError(f"host {host!r} resolved to a non-address {candidate!r}") from exc

        reason = classify_blocked_ip(ip)
        if reason is not None:
            raise UnsafeUrlError(f"host {host!r} resolves to {ip} ({reason})")
        addresses.append(ip)

    return addresses


def assert_safe_url(url: str, *, resolver: Resolver | None = None) -> None:
    """Raise `UnsafeUrlError` unless `url` is safe for us to request.

    Called once per redirect hop, never once per request.
    """
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"scheme {parts.scheme or '(none)'!r} is not allowed; use http or https", url=url
        )

    # Credentials in a URL are never something we want to transmit, and they are
    # a common way to make a hostile host look like a familiar one.
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in the URL are not allowed", url=url)

    try:
        host = parts.hostname
    except ValueError as exc:  # malformed IPv6 literal, e.g. http://[::1
        raise UnsafeUrlError(f"malformed host in URL: {exc}", url=url) from exc

    if not host:
        raise UnsafeUrlError("URL has no host", url=url)

    try:
        parts.port  # noqa: B018 - property access is the validation
    except ValueError as exc:
        raise UnsafeUrlError(f"invalid port in URL: {exc}", url=url) from exc

    # An IP literal is judged directly; resolving it would be theatre.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        reason = classify_blocked_ip(literal)
        if reason is not None:
            raise UnsafeUrlError(f"{host} is a {reason}", url=url)
        return

    try:
        resolve_safe_ips(host, resolver=resolver)
    except UnsafeUrlError as exc:
        raise UnsafeUrlError(exc.message, url=url) from exc


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


def parse_robots(text: str) -> RobotFileParser:
    """Parse robots.txt content. Pure: the caller does the fetching.

    `urllib.robotparser` can fetch on its own, but its fetcher is
    `urllib.request` — blocking, un-mockable by respx, and outside the SSRF
    guard. Feeding it lines keeps the network in exactly one module.
    """
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser


def robots_allows(parser: RobotFileParser | None, user_agent: str, url: str) -> bool:
    """Whether `user_agent` may fetch `url`.

    `parser is None` means "no usable robots.txt" (absent, or unreachable) and
    resolves to allowed. robots.txt is a politeness protocol, not a security
    control — the security control is `assert_safe_url`, which is never
    fail-open.
    """
    if parser is None:
        return True
    return parser.can_fetch(user_agent, url)


def robots_url_for(url: str) -> str:
    """The robots.txt URL governing `url` (one per scheme+host+port)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"
