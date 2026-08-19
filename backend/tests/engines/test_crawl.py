"""Tests for the crawl engine.

Two rules hold for every test in this file:

*   **No real network.** httpx is faked with respx, and DNS is injected via the
    `resolver` parameter. `test_no_real_dns_is_performed` proves the second half
    by breaking `socket.getaddrinfo` and showing a fetch still succeeds.
*   **The SSRF guard is tested as an attacker would probe it** — IP literals,
    a public hostname whose DNS answer is private, a mixed answer, a redirect
    whose *second* hop turns inward, and non-HTTP schemes. A guard that is only
    tested with `127.0.0.1` is a guard that only stops the first attempt.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
import respx

from backend.app.engines.crawl import (
    CrawlError,
    HttpStatusError,
    NetworkError,
    NotHtmlError,
    RobotsCache,
    RobotsDisallowedError,
    TimeoutError,
    TooLargeError,
    TooManyRedirectsError,
    UnsafeUrlError,
    assert_safe_url,
    classify_blocked_ip,
    crawl_site,
    fetch_page,
    normalise_link,
    parse_page,
    parse_robots,
    robots_allows,
)
from backend.app.engines.crawl.safety import Resolver

PUBLIC_IP = "93.184.216.34"


def make_resolver(
    mapping: dict[str, list[str]] | None = None,
    *,
    default: list[str] | None = None,
) -> Resolver:
    """A hermetic resolver.

    Unmapped hosts fall back to `default` (a public address unless overridden);
    passing `default=None` makes an unexpected lookup a loud test failure rather
    than a silent pass.
    """
    table = mapping or {}
    fallback = default if default is not None else [PUBLIC_IP]

    def resolve(host: str) -> Sequence[str]:
        if host in table:
            return table[host]
        if default is None and table:
            return fallback
        return fallback

    return resolve


def exploding_resolver(host: str) -> Sequence[str]:
    """A resolver that must never be called (IP-literal paths)."""
    raise AssertionError(f"DNS was consulted for {host!r}")


def html_response(body: str, **headers: str) -> httpx.Response:
    merged = {"content-type": "text/html; charset=utf-8"}
    merged.update(headers)
    return httpx.Response(200, headers=merged, content=body.encode("utf-8"))


PAGE_HTML = """
<!doctype html>
<html lang="de-DE">
  <head>
    <title>  Notdienst Elektriker Koblenz  </title>
    <meta Name="Description" content="24/7 electrician in Koblenz.">
    <meta name="robots" content="index,follow">
    <link rel="canonical" href="/notdienst/">
    <script type="application/ld+json">
      {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Elektro Koblenz"}
    </script>
    <script type="application/ld+json">
      {"@type": "FAQPage", "mainEntity": [,]}
    </script>
    <script type="application/ld+json">
      [{"@type": "Service", "name": "Notdienst"}, "not-an-object"]
    </script>
    <script type="application/json">{"ignored": true}</script>
  </head>
  <body>
    <h1>Notdienst Elektriker</h1>
    <h2>Koblenz und Umgebung</h2>
    <h3>Preise</h3>
    <h2>Kontakt</h2>
    <p>
      Wir sind rund um die Uhr fuer Sie erreichbar und kommen innerhalb von
      dreissig Minuten zu Ihnen nach Hause. Unsere Monteure arbeiten in Koblenz,
      Neuwied und Andernach und beheben Stoerungen an Sicherungskasten,
      Leitungen und Steckdosen. Rufen Sie uns an, wir helfen sofort und ohne
      langes Warten weiter, auch an Sonn- und Feiertagen.
    </p>
    <a href="/preise">Preise</a>
    <a href="/preise#top">Preise nochmal</a>
    <a href="https://www.example.com/impressum">Impressum</a>
    <a href="https://partner.example.org/x">Partner</a>
    <a href="mailto:hallo@example.com">Mail</a>
    <a href="tel:+4926112345">Anrufen</a>
    <a href="#inhalt">Sprungmarke</a>
    <img src="/img/van.jpg" alt="Unser Servicewagen">
    <img src="https://cdn.example.net/logo.svg">
    <img src="/img/spacer.gif" alt="">
  </body>
</html>
"""


# --------------------------------------------------------------------------- #
# parse_page
# --------------------------------------------------------------------------- #


def test_parse_extracts_head_facts() -> None:
    facts = parse_page(PAGE_HTML, "https://example.com/notdienst")

    assert facts.title == "Notdienst Elektriker Koblenz"
    assert facts.meta_description == "24/7 electrician in Koblenz."
    assert facts.robots_meta == "index,follow"
    assert facts.canonical == "https://example.com/notdienst/"
    assert facts.lang == "de-DE"
    assert facts.status == 200
    assert facts.url == "https://example.com/notdienst"


def test_parse_heading_tree_is_ordered_with_levels() -> None:
    facts = parse_page(PAGE_HTML, "https://example.com/notdienst")

    assert [(h.level, h.text) for h in facts.h_tree] == [
        (1, "Notdienst Elektriker"),
        (2, "Koblenz und Umgebung"),
        (3, "Preise"),
        (2, "Kontakt"),
    ]


def test_parse_splits_internal_and_external_links() -> None:
    facts = parse_page(PAGE_HTML, "https://example.com/notdienst")

    # `/preise` and `/preise#top` are one page: fragments are dropped, order kept.
    # `www.example.com` is the same site as `example.com`.
    assert facts.internal_links == [
        "https://example.com/preise",
        "https://www.example.com/impressum",
    ]
    assert facts.external_links == ["https://partner.example.org/x"]


def test_parse_records_images_and_distinguishes_missing_from_empty_alt() -> None:
    facts = parse_page(PAGE_HTML, "https://example.com/notdienst")

    assert [(i.src, i.alt) for i in facts.images] == [
        ("https://example.com/img/van.jpg", "Unser Servicewagen"),
        ("https://cdn.example.net/logo.svg", None),
        ("https://example.com/img/spacer.gif", ""),
    ]


def test_parse_extracts_jsonld_and_skips_the_malformed_block() -> None:
    facts = parse_page(PAGE_HTML, "https://example.com/notdienst")

    types = [block.get("@type") for block in facts.jsonld_blocks]
    # LocalBusiness parses, the FAQPage block has a syntax error and is dropped,
    # the array block contributes only its object member, and a non-ld+json
    # script is never considered.
    assert types == ["LocalBusiness", "Service"]
    assert all(isinstance(block, dict) for block in facts.jsonld_blocks)


def test_parse_reads_main_text_and_counts_words() -> None:
    facts = parse_page(PAGE_HTML, "https://example.com/notdienst")

    assert "rund um die Uhr" in facts.main_text
    # Navigation and script content must not be counted as prose.
    assert "application/ld+json" not in facts.main_text
    assert facts.word_count > 25
    assert facts.word_count == len(facts.main_text.split())


@pytest.mark.parametrize(
    ("html", "expected_title"),
    [
        ("", None),
        ("<html><head></head><body>hi</body></html>", None),
        ("<title></title>", None),
        ("<title>   </title>", None),
        ("<title>Kept</title>", "Kept"),
        ("<html><body><p>unclosed", None),
        ("not html at all, just text", None),
    ],
)
def test_parse_never_raises_on_degenerate_markup(html: str, expected_title: str | None) -> None:
    facts = parse_page(html, "https://example.com/")

    assert facts.title == expected_title
    assert facts.h_tree == []
    assert facts.jsonld_blocks == []


def test_parse_handles_graph_wrapped_jsonld() -> None:
    html = """
    <script type="application/ld+json">
      {"@graph": [{"@type": "Organization"}, {"@type": "WebSite"}]}
    </script>
    """

    facts = parse_page(html, "https://example.com/")

    assert [b["@type"] for b in facts.jsonld_blocks] == ["Organization", "WebSite"]


def test_jsonld_survives_the_main_text_fallback() -> None:
    """Regression: one field's extraction must not destroy another's.

    The visible-text fallback strips `<script>` tags. When it did that to the
    shared soup, every page too short for trafilatura silently reported zero
    JSON-LD blocks — schema markup on exactly the thin pages the seo engine most
    needs to grade.
    """
    html = (
        "<html><body><p>Short.</p>"
        '<script type="application/ld+json">{"@type":"Thing"}</script>'
        "</body></html>"
    )

    facts = parse_page(html, "https://example.com/")

    assert [b["@type"] for b in facts.jsonld_blocks] == ["Thing"]
    assert facts.main_text == "Short."
    assert "@type" not in facts.main_text


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/a", "https://example.com/a"),
        ("a", "https://example.com/a"),
        ("https://other.test/b?q=1#frag", "https://other.test/b?q=1"),
        ("", None),
        ("#top", None),
        ("mailto:x@y.z", None),
        ("tel:+49123", None),
        ("javascript:alert(1)", None),
        ("data:text/html,<b>x</b>", None),
        ("ftp://files.test/x", None),
    ],
)
def test_normalise_link(href: str, expected: str | None) -> None:
    assert normalise_link("https://example.com/", href) == expected


# --------------------------------------------------------------------------- #
# safety: the SSRF guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("http://127.0.0.1/", "loopback"),
        ("http://127.0.0.1:8080/admin", "loopback"),
        ("https://[::1]/", "loopback"),
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
        ("http://169.254.169.254/", "cloud metadata"),
        ("http://[fd00:ec2::254]/", "cloud metadata"),
        ("http://100.100.100.200/", "cloud metadata"),
        ("http://10.0.0.1/", "private"),
        ("http://192.168.1.1/", "private"),
        ("http://172.16.5.4/", "private"),
        ("http://[fd12:3456::1]/", "private"),
        ("http://0.0.0.0/", "unspecified"),
        ("http://[::]/", "unspecified"),
        ("http://224.0.0.1/", "multicast"),
        ("http://[::ffff:127.0.0.1]/", "loopback"),
        ("http://169.254.1.5/", "link-local"),
        ("file:///etc/passwd", "not allowed"),
        ("ftp://files.test/secret", "not allowed"),
        ("gopher://x.test/", "not allowed"),
        ("//example.com/", "not allowed"),
        ("http://user:pw@example.com/", "credentials"),
        ("http:///nohost", "no host"),
    ],
)
def test_assert_safe_url_refuses_unsafe_targets(url: str, fragment: str) -> None:
    with pytest.raises(UnsafeUrlError) as caught:
        assert_safe_url(url, resolver=exploding_resolver)

    assert fragment in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://example.com/path?q=1",
        "https://example.com:8443/",
        f"http://{PUBLIC_IP}/",
    ],
)
def test_assert_safe_url_allows_public_targets(url: str) -> None:
    # A safe URL is a silent return; anything unsafe raises.
    assert_safe_url(url, resolver=make_resolver())


def test_public_hostname_resolving_to_private_ip_is_refused() -> None:
    """Rejecting by hostname string alone is not enough: this is the real attack."""
    resolver = make_resolver({"totally-normal.example": ["10.1.2.3"]})

    with pytest.raises(UnsafeUrlError) as caught:
        assert_safe_url("https://totally-normal.example/", resolver=resolver)

    assert "10.1.2.3" in str(caught.value)
    assert "private" in str(caught.value)


def test_any_unsafe_answer_in_a_multi_record_response_refuses() -> None:
    resolver = make_resolver({"mixed.example": [PUBLIC_IP, "127.0.0.1"]})

    with pytest.raises(UnsafeUrlError):
        assert_safe_url("https://mixed.example/", resolver=resolver)


@pytest.mark.parametrize(
    "answer",
    [[], ["not-an-ip"], ["999.999.999.999"]],
)
def test_unusable_dns_answers_are_refused(answer: list[str]) -> None:
    resolver = make_resolver({"weird.example": answer}, default=[PUBLIC_IP])

    with pytest.raises(UnsafeUrlError):
        assert_safe_url("https://weird.example/", resolver=resolver)


def test_resolver_failure_is_a_typed_error_not_a_leak() -> None:
    def broken(host: str) -> Sequence[str]:
        raise OSError("nameserver down")

    with pytest.raises(UnsafeUrlError):
        assert_safe_url("https://example.com/", resolver=broken)


def test_ip_literals_do_not_consult_dns() -> None:
    assert_safe_url(f"https://{PUBLIC_IP}/", resolver=exploding_resolver)


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("93.184.216.34", None),
        ("2606:2800:220:1:248:1893:25c8:1946", None),
        ("127.0.0.1", "loopback address"),
        ("10.0.0.1", "private address"),
        ("169.254.169.254", "cloud metadata address"),
        ("169.254.0.1", "link-local address"),
        ("0.0.0.0", "unspecified address"),
        # 240.0.0.0/4 is "reserved for future use", which Python classifies as
        # private; the reason string differs from intuition, the refusal does not.
        ("240.0.0.1", "private address"),
        ("::ffff:10.0.0.1", "private address"),
    ],
)
def test_classify_blocked_ip(ip: str, expected: str | None) -> None:
    assert classify_blocked_ip(ipaddress.ip_address(ip)) == expected


def test_robots_parsing_is_pure() -> None:
    parser = parse_robots("User-agent: *\nDisallow: /private\n")

    assert robots_allows(parser, "GrowthAgentBot/0.1", "https://example.com/public") is True
    assert robots_allows(parser, "GrowthAgentBot/0.1", "https://example.com/private/x") is False
    # No robots.txt means allowed: politeness fails open, security never does.
    assert robots_allows(None, "GrowthAgentBot/0.1", "https://example.com/private/x") is True


# --------------------------------------------------------------------------- #
# fetch_page
# --------------------------------------------------------------------------- #


async def test_fetch_page_returns_html_and_metadata() -> None:
    with respx.mock:
        respx.get("https://example.com/").mock(return_value=html_response(PAGE_HTML))

        result = await fetch_page(
            "https://example.com/", resolver=make_resolver(), respect_robots=False
        )

    assert result.status == 200
    assert result.url == "https://example.com/"
    assert result.requested_url == "https://example.com/"
    assert result.content_type is not None and "text/html" in result.content_type
    assert result.encoding == "utf-8"
    assert result.bytes_read == len(PAGE_HTML.encode("utf-8"))
    assert result.redirect_chain == []
    assert "Notdienst" in result.html


async def test_fetch_page_follows_redirects_and_records_the_chain() -> None:
    with respx.mock:
        respx.get("https://example.com/old").mock(
            return_value=httpx.Response(301, headers={"location": "/middle"})
        )
        respx.get("https://example.com/middle").mock(
            return_value=httpx.Response(302, headers={"location": "https://example.com/new"})
        )
        respx.get("https://example.com/new").mock(return_value=html_response("<h1>new</h1>"))

        result = await fetch_page(
            "https://example.com/old", resolver=make_resolver(), respect_robots=False
        )

    assert result.url == "https://example.com/new"
    assert result.requested_url == "https://example.com/old"
    assert result.redirect_chain == ["https://example.com/old", "https://example.com/middle"]


async def test_redirect_to_a_private_ip_is_refused_on_the_second_hop() -> None:
    """The guard must re-run per hop; a safe first hop proves nothing."""
    resolver = make_resolver(
        {"public.example": [PUBLIC_IP], "internal.example": ["10.0.0.5"]}, default=[PUBLIC_IP]
    )

    with respx.mock:
        first = respx.get("https://public.example/start").mock(
            return_value=httpx.Response(302, headers={"location": "http://internal.example/admin"})
        )
        never = respx.get("http://internal.example/admin").mock(
            return_value=html_response("<h1>secrets</h1>")
        )

        with pytest.raises(UnsafeUrlError) as caught:
            await fetch_page(
                "https://public.example/start", resolver=resolver, respect_robots=False
            )

    assert "10.0.0.5" in str(caught.value)
    assert first.called
    assert never.call_count == 0, "the unsafe hop must never be requested"


async def test_redirect_to_the_metadata_service_is_refused() -> None:
    with respx.mock:
        respx.get("https://example.com/start").mock(
            return_value=httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        )

        with pytest.raises(UnsafeUrlError) as caught:
            await fetch_page(
                "https://example.com/start", resolver=make_resolver(), respect_robots=False
            )

    assert "cloud metadata" in str(caught.value)


async def test_redirect_loop_stops_at_the_hop_limit() -> None:
    with respx.mock:
        respx.get("https://example.com/loop").mock(
            return_value=httpx.Response(302, headers={"location": "/loop"})
        )

        with pytest.raises(TooManyRedirectsError):
            await fetch_page(
                "https://example.com/loop",
                resolver=make_resolver(),
                respect_robots=False,
                max_redirects=3,
            )


async def test_redirect_without_a_location_header_is_an_http_error() -> None:
    with respx.mock:
        respx.get("https://example.com/x").mock(return_value=httpx.Response(302))

        with pytest.raises(HttpStatusError) as caught:
            await fetch_page(
                "https://example.com/x", resolver=make_resolver(), respect_robots=False
            )

    assert caught.value.status == 302


async def test_declared_content_length_over_the_cap_is_refused() -> None:
    with respx.mock:
        respx.get("https://example.com/big").mock(return_value=html_response("x" * 5_000))

        with pytest.raises(TooLargeError) as caught:
            await fetch_page(
                "https://example.com/big",
                resolver=make_resolver(),
                respect_robots=False,
                max_bytes=1_000,
            )

    assert "content-length" in str(caught.value)


async def test_streamed_body_over_the_cap_is_refused_mid_stream() -> None:
    """No content-length, so the cap can only be enforced while streaming."""
    chunks_served = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal chunks_served
        for _ in range(50):
            chunks_served += 1
            yield b"x" * 100

    with respx.mock:
        respx.get("https://example.com/stream").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/html"}, content=body())
        )

        with pytest.raises(TooLargeError):
            await fetch_page(
                "https://example.com/stream",
                resolver=make_resolver(),
                respect_robots=False,
                max_bytes=250,
            )

    assert chunks_served < 50, "the whole body was buffered before the cap was applied"


@pytest.mark.parametrize(
    "exception",
    [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("slow")],
)
async def test_timeouts_become_a_typed_timeout_error(exception: Exception) -> None:
    with respx.mock:
        respx.get("https://example.com/slow").mock(side_effect=exception)

        with pytest.raises(TimeoutError) as caught:
            await fetch_page(
                "https://example.com/slow",
                resolver=make_resolver(),
                respect_robots=False,
                timeout_s=0.25,
            )

    assert caught.value.code == "timeout"
    assert caught.value.url == "https://example.com/slow"


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/png", "application/json", "text/plain", "application/octet-stream"],
)
async def test_non_html_content_types_are_refused(content_type: str) -> None:
    with respx.mock:
        respx.get("https://example.com/file").mock(
            return_value=httpx.Response(
                200, headers={"content-type": content_type}, content=b"%PDF"
            )
        )

        with pytest.raises(NotHtmlError) as caught:
            await fetch_page(
                "https://example.com/file", resolver=make_resolver(), respect_robots=False
            )

    assert content_type in str(caught.value)


@pytest.mark.parametrize("content_type", ["text/html", "application/xhtml+xml", None])
async def test_html_and_missing_content_types_are_accepted(content_type: str | None) -> None:
    headers = {} if content_type is None else {"content-type": content_type}

    with respx.mock:
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, headers=headers, content=b"<h1>ok</h1>")
        )

        result = await fetch_page(
            "https://example.com/page", resolver=make_resolver(), respect_robots=False
        )

    assert result.html == "<h1>ok</h1>"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 500, 503])
async def test_error_statuses_become_http_status_errors(status: int) -> None:
    with respx.mock:
        respx.get("https://example.com/gone").mock(return_value=httpx.Response(status))

        with pytest.raises(HttpStatusError) as caught:
            await fetch_page(
                "https://example.com/gone", resolver=make_resolver(), respect_robots=False
            )

    assert caught.value.status == status
    assert caught.value.to_info().status == status


async def test_transport_failures_become_a_typed_network_error() -> None:
    with respx.mock:
        respx.get("https://example.com/down").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(NetworkError):
            await fetch_page(
                "https://example.com/down", resolver=make_resolver(), respect_robots=False
            )


@pytest.mark.parametrize(
    "error",
    [
        UnsafeUrlError("x"),
        TimeoutError("x"),
        TooLargeError("x"),
        NotHtmlError("x"),
        HttpStatusError("x", status=500),
        RobotsDisallowedError("x"),
        TooManyRedirectsError("x"),
        NetworkError("x"),
    ],
)
def test_every_error_is_a_crawl_error(error: CrawlError) -> None:
    """One `except CrawlError` must be enough for a caller."""
    assert isinstance(error, CrawlError)
    assert error.to_info().code == error.code


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


async def test_robots_disallow_refuses_the_fetch() -> None:
    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"User-agent: *\nDisallow: /private\n",
            )
        )
        page = respx.get("https://example.com/private/secret").mock(
            return_value=html_response("<h1>secret</h1>")
        )

        with pytest.raises(RobotsDisallowedError):
            await fetch_page("https://example.com/private/secret", resolver=make_resolver())

    assert page.call_count == 0, "a disallowed page must never be requested"


async def test_robots_allow_permits_the_fetch() -> None:
    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"User-agent: *\nDisallow: /private\n",
            )
        )
        respx.get("https://example.com/public").mock(return_value=html_response("<h1>ok</h1>"))

        result = await fetch_page("https://example.com/public", resolver=make_resolver())

    assert result.status == 200


async def test_robots_is_fetched_once_per_host_when_the_cache_is_shared() -> None:
    cache = RobotsCache()

    with respx.mock:
        robots = respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"User-agent: *\nAllow: /\n"
            )
        )
        respx.get("https://example.com/a").mock(return_value=html_response("<h1>a</h1>"))
        respx.get("https://example.com/b").mock(return_value=html_response("<h1>b</h1>"))

        for path in ("a", "b"):
            await fetch_page(f"https://example.com/{path}", resolver=make_resolver(), robots=cache)

    assert robots.call_count == 1


async def test_a_missing_robots_file_allows_crawling() -> None:
    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://example.com/page").mock(return_value=html_response("<h1>ok</h1>"))

        result = await fetch_page("https://example.com/page", resolver=make_resolver())

    assert result.status == 200


async def test_respect_robots_false_never_requests_robots() -> None:
    with respx.mock:
        robots = respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, content=b"User-agent: *\nDisallow: /\n")
        )
        respx.get("https://example.com/page").mock(return_value=html_response("<h1>ok</h1>"))

        await fetch_page("https://example.com/page", resolver=make_resolver(), respect_robots=False)

    assert robots.call_count == 0


# --------------------------------------------------------------------------- #
# crawl_site
# --------------------------------------------------------------------------- #

SITE_INDEX = """
<html><head><title>Home</title></head><body>
  <h1>Home</h1>
  <a href="/a">A</a>
  <a href="/b">B</a>
  <a href="https://competitor.test/">Competitor</a>
</body></html>
"""

SITE_A = """
<html><head><title>A</title></head><body>
  <h1>A</h1><a href="/c">C</a><a href="/">Home</a>
</body></html>
"""


def _register_site() -> respx.Route:
    respx.get("https://example.com/").mock(return_value=html_response(SITE_INDEX))
    respx.get("https://example.com/a").mock(return_value=html_response(SITE_A))
    respx.get("https://example.com/b").mock(return_value=html_response("<h1>B</h1>"))
    respx.get("https://example.com/c").mock(return_value=html_response("<h1>C</h1>"))
    return respx.get("https://competitor.test/").mock(return_value=html_response("<h1>nope</h1>"))


async def test_crawl_site_walks_the_start_host_only() -> None:
    with respx.mock:
        competitor = _register_site()

        result = await crawl_site(
            "https://example.com/", resolver=make_resolver(), respect_robots=False
        )

    assert [page.url for page in result.pages] == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert competitor.call_count == 0, "crawl left the start host"
    assert result.errors == []
    assert result.truncated is False
    assert result.page_count == 4


async def test_crawl_site_respects_max_pages_and_reports_truncation() -> None:
    with respx.mock:
        _register_site()

        result = await crawl_site(
            "https://example.com/",
            resolver=make_resolver(),
            respect_robots=False,
            max_pages=2,
        )

    assert result.page_count == 2
    assert result.truncated is True


async def test_crawl_site_respects_max_depth() -> None:
    with respx.mock:
        _register_site()
        deep = respx.get("https://example.com/c")

        result = await crawl_site(
            "https://example.com/",
            resolver=make_resolver(),
            respect_robots=False,
            max_depth=1,
        )

    assert [page.url for page in result.pages] == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert deep.call_count == 0


async def test_crawl_site_records_page_failures_and_keeps_going() -> None:
    with respx.mock:
        respx.get("https://example.com/").mock(return_value=html_response(SITE_INDEX))
        respx.get("https://example.com/a").mock(return_value=httpx.Response(500))
        respx.get("https://example.com/b").mock(return_value=html_response("<h1>B</h1>"))
        respx.get("https://competitor.test/").mock(return_value=html_response("<h1>nope</h1>"))

        result = await crawl_site(
            "https://example.com/", resolver=make_resolver(), respect_robots=False
        )

    assert [page.url for page in result.pages] == [
        "https://example.com/",
        "https://example.com/b",
    ]
    assert [(e.code, e.url, e.status) for e in result.errors] == [
        ("http_status", "https://example.com/a", 500)
    ]


async def test_crawl_site_drops_a_page_that_redirects_off_site() -> None:
    """A safe redirect can still leave the site; `pages` must stay one host."""
    with respx.mock:
        respx.get("https://example.com/").mock(
            return_value=html_response('<h1>Home</h1><a href="/shop">Shop</a>')
        )
        respx.get("https://example.com/shop").mock(
            return_value=httpx.Response(302, headers={"location": "https://storefront.test/"})
        )
        respx.get("https://storefront.test/").mock(return_value=html_response("<h1>Store</h1>"))

        result = await crawl_site(
            "https://example.com/", resolver=make_resolver(), respect_robots=False
        )

    assert [page.url for page in result.pages] == ["https://example.com/"]
    assert result.errors == []


async def test_crawl_site_never_raises_on_an_unsafe_start_url() -> None:
    result = await crawl_site("http://169.254.169.254/", resolver=make_resolver())

    assert result.pages == []
    assert [e.code for e in result.errors] == ["unsafe_url"]
    assert result.truncated is False


async def test_crawl_site_obeys_robots_for_a_subtree() -> None:
    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"User-agent: *\nDisallow: /b\n",
            )
        )
        _register_site()

        result = await crawl_site("https://example.com/", resolver=make_resolver())

    assert "https://example.com/b" not in [page.url for page in result.pages]
    assert [e.code for e in result.errors] == ["robots_disallowed"]


# --------------------------------------------------------------------------- #
# hermeticity
# --------------------------------------------------------------------------- #


async def test_no_real_dns_is_performed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The injected resolver is the only name resolution in the engine's path."""

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("socket.getaddrinfo was called; DNS must be injected")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

    with respx.mock:
        respx.get("https://example.com/").mock(return_value=html_response("<h1>ok</h1>"))

        result = await fetch_page(
            "https://example.com/", resolver=make_resolver(), respect_robots=False
        )

    assert result.status == 200
