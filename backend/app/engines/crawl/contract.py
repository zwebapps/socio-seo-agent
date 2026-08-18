"""Typed inputs, outputs and errors for the crawl engine.

This module is deliberately dependency-free: Pydantic and the standard library
only. It is the contract other layers (HARVEST, the seo engine, the API) import,
so it must never grow an HTTP client, a parser, or anything else that could pull
a side effect in behind it.

Two conventions worth stating once, because every consumer relies on them:

*   **Failure is typed, never `None`.** A page that could not be fetched raises a
    `CrawlError` subclass carrying a machine-readable `code`; the single-page API
    (`fetch_page`) raises it, and the multi-page API (`crawl_site`) collects it as
    a `CrawlErrorInfo` on the result so one dead page cannot abort a site crawl.
    Nothing in this engine raises a bare `Exception`, and nothing returns `None`
    to mean "it went wrong".
*   **Prices of correctness are paid here, not by the caller.** URLs on
    `PageFacts` are already absolute, links are already de-duplicated, and
    `jsonld_blocks` already contains only successfully parsed objects. The agent
    layer reads facts; it never cleans them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorCode = Literal[
    "unsafe_url",
    "timeout",
    "too_large",
    "not_html",
    "http_status",
    "robots_disallowed",
    "too_many_redirects",
    "network",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CrawlError(Exception):
    """Base class for every failure this engine can produce.

    Carrying `code` and `url` on the exception is what lets a caller degrade
    gracefully (record a `fact_gap`, keep crawling) instead of pattern-matching
    on message strings.
    """

    code: ErrorCode = "network"

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.url = url

    def to_info(self) -> CrawlErrorInfo:
        """Serialisable form, for `CrawlResult.errors` and for the UI."""
        return CrawlErrorInfo(code=self.code, url=self.url, message=self.message)


class UnsafeUrlError(CrawlError):
    """The URL, or a redirect hop, pointed somewhere we refuse to talk to."""

    code: ErrorCode = "unsafe_url"


class TimeoutError(CrawlError):  # noqa: A001 - the public contract names it this
    """The request exceeded `timeout_s`.

    Shadows the builtin deliberately: the crawl engine's public error surface is
    specified as `CrawlError` subclasses, and a caller writing
    `except crawl.TimeoutError` should not have to remember a different spelling.
    Always reference it via the module or an explicit import.
    """

    code: ErrorCode = "timeout"


class TooLargeError(CrawlError):
    """The response exceeded `max_bytes`; detected while streaming, not after."""

    code: ErrorCode = "too_large"


class NotHtmlError(CrawlError):
    """The response was not HTML, so there are no page facts to extract."""

    code: ErrorCode = "not_html"


class HttpStatusError(CrawlError):
    """The origin answered with a 4xx or 5xx status."""

    code: ErrorCode = "http_status"

    def __init__(self, message: str, *, url: str | None = None, status: int) -> None:
        super().__init__(message, url=url)
        self.status = status

    def to_info(self) -> CrawlErrorInfo:
        return CrawlErrorInfo(
            code=self.code, url=self.url, message=self.message, status=self.status
        )


class RobotsDisallowedError(CrawlError):
    """`robots.txt` disallows this path for our user agent."""

    code: ErrorCode = "robots_disallowed"


class TooManyRedirectsError(CrawlError):
    """The redirect chain exceeded the hop limit.

    Not in the minimum error list, but a chain that never terminates is a real
    outcome and squeezing it into `HttpStatusError` would lose that information.
    """

    code: ErrorCode = "too_many_redirects"


class NetworkError(CrawlError):
    """Connection refused, DNS failure, TLS failure, malformed response.

    Everything the transport can fail with that is not a timeout, so the caller
    never sees a raw `httpx` exception leak out of the engine.
    """

    code: ErrorCode = "network"


class CrawlErrorInfo(BaseModel):
    """A failure, in data form. This is what `crawl_site` returns instead of raising."""

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    url: str | None = None
    message: str
    status: int | None = None


# --------------------------------------------------------------------------- #
# Page facts
# --------------------------------------------------------------------------- #


class Heading(BaseModel):
    """One heading, in document order. The seo engine scores the tree's shape."""

    model_config = ConfigDict(frozen=True)

    level: int = Field(ge=1, le=6)
    text: str


class ImageRef(BaseModel):
    """An image and its alt text.

    `alt is None` means the attribute is absent (an accessibility and SEO
    finding); `alt == ""` means it is present and empty, which is the correct
    markup for a decorative image. Collapsing the two would make the seo engine
    report false positives, so the distinction is preserved here.
    """

    model_config = ConfigDict(frozen=True)

    src: str
    alt: str | None = None


class PageFacts(BaseModel):
    """Everything the deterministic layer knows about one page.

    No opinions, no scores, no prose. `seo`, `geo` and the agent layer all read
    this; none of them re-parse HTML.
    """

    url: str
    status: int = 200
    title: str | None = None
    meta_description: str | None = None
    canonical: str | None = None
    lang: str | None = None
    robots_meta: str | None = None
    h_tree: list[Heading] = Field(default_factory=list)
    internal_links: list[str] = Field(default_factory=list)
    external_links: list[str] = Field(default_factory=list)
    images: list[ImageRef] = Field(default_factory=list)
    word_count: int = 0
    main_text: str = ""
    jsonld_blocks: list[dict[str, Any]] = Field(default_factory=list)


class FetchResult(BaseModel):
    """The raw outcome of one HTTP fetch.

    Kept separate from `PageFacts` so that fetching stays about I/O and parsing
    stays pure: `fetch_page` never calls the parser, and `parse_page` never
    touches the network.
    """

    requested_url: str
    url: str  # final URL, after redirects
    status: int
    content_type: str | None = None
    html: str
    encoding: str
    bytes_read: int
    redirect_chain: list[str] = Field(default_factory=list)


class CrawlResult(BaseModel):
    """The outcome of a site crawl.

    `truncated` and `errors` exist so a partial crawl is a first-class result:
    HARVEST turns them into `fact_gaps` and the UI can say "43 of an unknown
    number of pages" instead of implying the crawl was complete.
    """

    start_url: str
    pages: list[PageFacts] = Field(default_factory=list)
    errors: list[CrawlErrorInfo] = Field(default_factory=list)
    truncated: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)
