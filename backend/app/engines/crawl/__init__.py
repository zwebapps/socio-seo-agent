"""crawl engine: fetch a page or a site safely, and turn HTML into typed facts.

An engine in the sense of docs/ARCHITECTURE.md section 3 — deterministic,
read-only, no LLM, no database, no side effects. It answers "what is on this
website", and nothing above it re-parses HTML.

    from backend.app.engines.crawl import crawl_site, fetch_page, parse_page

    facts = parse_page(html, "https://example.com/")           # pure
    result = await fetch_page("https://example.com/")           # I/O, safe
    site = await crawl_site("https://example.com/", max_pages=40)

Failure is typed. `fetch_page` and `assert_safe_url` raise a `CrawlError`
subclass; `crawl_site` never raises and reports per-page failures as
`CrawlErrorInfo` entries on the result, so a partial crawl is a usable result
rather than an exception.
"""

from .contract import (
    CrawlError,
    CrawlErrorInfo,
    CrawlResult,
    ErrorCode,
    FetchResult,
    Heading,
    HttpStatusError,
    ImageRef,
    NetworkError,
    NotHtmlError,
    PageFacts,
    RobotsDisallowedError,
    TimeoutError,  # noqa: A004 - re-exported under the name the contract specifies
    TooLargeError,
    TooManyRedirectsError,
    UnsafeUrlError,
)
from .fetch import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PAGES,
    DEFAULT_TIMEOUT_S,
    DEFAULT_USER_AGENT,
    RobotsCache,
    crawl_site,
    facts_from_fetch,
    fetch_page,
)
from .parse import normalise_link, parse_page
from .safety import (
    ALLOWED_SCHEMES,
    CLOUD_METADATA_ADDRESSES,
    MAX_REDIRECTS,
    Resolver,
    assert_safe_url,
    classify_blocked_ip,
    parse_robots,
    resolve_safe_ips,
    robots_allows,
    robots_url_for,
    system_resolver,
)

__all__ = [
    "ALLOWED_SCHEMES",
    "CLOUD_METADATA_ADDRESSES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_USER_AGENT",
    "MAX_REDIRECTS",
    "CrawlError",
    "CrawlErrorInfo",
    "CrawlResult",
    "ErrorCode",
    "FetchResult",
    "Heading",
    "HttpStatusError",
    "ImageRef",
    "NetworkError",
    "NotHtmlError",
    "PageFacts",
    "Resolver",
    "RobotsCache",
    "RobotsDisallowedError",
    "TimeoutError",
    "TooLargeError",
    "TooManyRedirectsError",
    "UnsafeUrlError",
    "assert_safe_url",
    "classify_blocked_ip",
    "crawl_site",
    "facts_from_fetch",
    "fetch_page",
    "normalise_link",
    "parse_page",
    "parse_robots",
    "resolve_safe_ips",
    "robots_allows",
    "robots_url_for",
    "system_resolver",
]
