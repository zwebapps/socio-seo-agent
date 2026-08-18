"""HTTP fetching for the crawl engine. The only module here that touches a socket.

Four properties are non-negotiable, and each one is a decision that reads as
awkward until you know what it prevents:

*   **Redirects are followed by hand, never by httpx.** `follow_redirects=False`
    everywhere, and `assert_safe_url` runs on every hop. Letting the client
    follow redirects would make the SSRF guard decorative: one 302 to
    `169.254.169.254` and the guard has been walked around.
*   **`max_bytes` is enforced while streaming.** A declared `content-length`
    over the cap is refused before a byte of body is read, and the streaming
    loop refuses the moment the accumulated size crosses the cap. A 2 GB
    response must never be buffered to find out it is 2 GB.
*   **robots.txt is fetched through this module and cached per origin.** Not via
    `urllib.robotparser`'s own fetcher, which is blocking, unmockable, and
    outside the SSRF guard.
*   **No exception from httpx escapes.** Everything becomes a `CrawlError`
    subclass, so the caller's error handling is against this engine's contract
    rather than against a transport library's.

`RobotsCache` is instantiated per call or per crawl and passed in explicitly:
there is no module-level cache, because an engine with global mutable state is
not the deterministic component this architecture claims it is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .contract import (
    CrawlError,
    CrawlResult,
    FetchResult,
    HttpStatusError,
    NetworkError,
    NotHtmlError,
    PageFacts,
    RobotsDisallowedError,
    TimeoutError,  # noqa: A004 - the engine's error surface names it this
    TooLargeError,
    TooManyRedirectsError,
)
from .parse import parse_page
from .safety import (
    MAX_REDIRECTS,
    Resolver,
    assert_safe_url,
    parse_robots,
    robots_allows,
    robots_url_for,
)

#: Identifiable, contactable, and honest about being a bot. A crawler that
#: pretends to be a browser cannot then claim to respect robots.txt.
DEFAULT_USER_AGENT = "GrowthAgentBot/0.1 (+https://growth-agent.example/bot)"

DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_PAGES = 40
DEFAULT_MAX_DEPTH = 3

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HTML_TYPES = ("text/html", "application/xhtml+xml", "application/xhtml")
_ROBOTS_MAX_BYTES = 512_000


class RobotsCache:
    """robots.txt per origin, fetched at most once.

    Cached on `(user_agent, origin)`: the same crawl always uses one agent, but
    keying on it means a differently-agented fetch can never inherit another
    agent's verdict.
    """

    def __init__(self) -> None:
        self._parsers: dict[tuple[str, str], RobotFileParser | None] = {}

    async def allows(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        user_agent: str,
        timeout_s: float,
        resolver: Resolver | None = None,
    ) -> bool:
        robots_url = robots_url_for(url)
        key = (user_agent, robots_url)

        if key not in self._parsers:
            self._parsers[key] = await self._load(
                robots_url,
                client=client,
                user_agent=user_agent,
                timeout_s=timeout_s,
                resolver=resolver,
            )

        return robots_allows(self._parsers[key], user_agent, url)

    async def _load(
        self,
        robots_url: str,
        *,
        client: httpx.AsyncClient,
        user_agent: str,
        timeout_s: float,
        resolver: Resolver | None,
    ) -> RobotFileParser | None:
        """Fetch and parse robots.txt, or `None` if there is nothing usable.

        `robots=None` on the inner fetch is what stops the obvious recursion:
        fetching robots.txt must not itself consult robots.txt.
        """
        try:
            result = await _fetch(
                robots_url,
                client=client,
                timeout_s=timeout_s,
                max_bytes=_ROBOTS_MAX_BYTES,
                user_agent=user_agent,
                resolver=resolver,
                robots=None,
                require_html=False,
            )
        except CrawlError:
            # Absent, unreachable, oversized or refused: treat the origin as
            # having no robots.txt. Fail-open is correct for a politeness
            # protocol and is never applied to the SSRF guard.
            return None

        return parse_robots(result.html)


async def fetch_page(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    user_agent: str = DEFAULT_USER_AGENT,
    resolver: Resolver | None = None,
    respect_robots: bool = True,
    max_redirects: int = MAX_REDIRECTS,
    client: httpx.AsyncClient | None = None,
    robots: RobotsCache | None = None,
) -> FetchResult:
    """Fetch one URL safely and return its raw HTML.

    Raises a `CrawlError` subclass on every failure mode; never returns `None`,
    and never lets an httpx exception through. Parsing is deliberately not done
    here — pass the result to `parse_page`.

    `client` and `robots` exist so `crawl_site` can share one connection pool and
    one robots verdict across a whole site; a single fetch can ignore both.
    """
    if client is not None:
        return await _fetch(
            url,
            client=client,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            user_agent=user_agent,
            resolver=resolver,
            robots=robots if respect_robots else None,
            max_redirects=max_redirects,
        )

    async with _make_client(timeout_s=timeout_s, user_agent=user_agent) as owned:
        return await _fetch(
            url,
            client=owned,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            user_agent=user_agent,
            resolver=resolver,
            robots=(robots or RobotsCache()) if respect_robots else None,
            max_redirects=max_redirects,
        )


async def crawl_site(
    start_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    user_agent: str = DEFAULT_USER_AGENT,
    resolver: Resolver | None = None,
    respect_robots: bool = True,
    max_redirects: int = MAX_REDIRECTS,
    client: httpx.AsyncClient | None = None,
) -> CrawlResult:
    """Breadth-first crawl of one site, capped by `max_pages` and `max_depth`.

    Never raises. A page that fails becomes a `CrawlErrorInfo` on the result and
    the crawl continues, because one 500 on a customer's blog must not cost them
    the other 42 pages. `truncated` says whether the frontier still had URLs when
    the cap was hit, which is what HARVEST turns into a `fact_gap`.

    Only URLs on the start host are followed (`www.` ignored, per `parse.py`).
    A redirect that lands off-site ends that branch: the response is not
    recorded as a page of this site and its links are not expanded.
    """
    result = CrawlResult(start_url=start_url)

    if client is not None:
        await _crawl_with_client(
            result,
            client=client,
            max_pages=max_pages,
            max_depth=max_depth,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            user_agent=user_agent,
            resolver=resolver,
            respect_robots=respect_robots,
            max_redirects=max_redirects,
        )
        return result

    async with _make_client(timeout_s=timeout_s, user_agent=user_agent) as owned:
        await _crawl_with_client(
            result,
            client=owned,
            max_pages=max_pages,
            max_depth=max_depth,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            user_agent=user_agent,
            resolver=resolver,
            respect_robots=respect_robots,
            max_redirects=max_redirects,
        )
    return result


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _make_client(*, timeout_s: float, user_agent: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_s),
        headers={"user-agent": user_agent, "accept": "text/html,application/xhtml+xml"},
    )


async def _crawl_with_client(
    result: CrawlResult,
    *,
    client: httpx.AsyncClient,
    max_pages: int,
    max_depth: int,
    timeout_s: float,
    max_bytes: int,
    user_agent: str,
    resolver: Resolver | None,
    respect_robots: bool,
    max_redirects: int,
) -> None:
    from .parse import normalise_link  # local: keeps the module's public surface flat

    robots = RobotsCache() if respect_robots else None
    start = normalise_link(result.start_url, result.start_url) or result.start_url
    start_host = _site_key(start)

    frontier: list[tuple[str, int]] = [(start, 0)]
    seen: set[str] = {start}

    while frontier and len(result.pages) < max_pages:
        url, depth = frontier.pop(0)

        try:
            fetched = await _fetch(
                url,
                client=client,
                timeout_s=timeout_s,
                max_bytes=max_bytes,
                user_agent=user_agent,
                resolver=resolver,
                robots=robots,
                max_redirects=max_redirects,
            )
        except CrawlError as exc:
            result.errors.append(exc.to_info())
            continue

        # A redirect can land off-site even though every hop was safe (a /shop
        # that now points at a hosted storefront). The SSRF guard has already
        # cleared it, but it is not a page of *this* site: recording it would put
        # a foreign host in `pages`, which every consumer reads as "the
        # customer's website", and expanding its links would walk off into the
        # web. So it is dropped, not an error.
        if _site_key(fetched.url) != start_host:
            continue

        facts = parse_page(fetched.html, fetched.url, status=fetched.status)
        result.pages.append(facts)

        if depth >= max_depth:
            continue

        for link in facts.internal_links:
            if link in seen or _site_key(link) != start_host:
                continue
            seen.add(link)
            frontier.append((link, depth + 1))

    result.truncated = bool(frontier)


def _site_key(url: str) -> str | None:
    host = urlsplit(url).hostname
    return host.lower().removeprefix("www.") if host else None


async def _fetch(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_s: float,
    max_bytes: int,
    user_agent: str,
    resolver: Resolver | None,
    robots: RobotsCache | None,
    max_redirects: int = MAX_REDIRECTS,
    require_html: bool = True,
) -> FetchResult:
    """One safe fetch, following redirects manually and re-validating each hop."""
    requested = url
    current = url
    chain: list[str] = []

    for _ in range(max_redirects + 1):
        # The guard is synchronous (DNS is a blocking call), so it runs off the
        # event loop; with worker concurrency of 8 a stalled lookup would
        # otherwise stall every other crawl in the pool.
        await asyncio.to_thread(assert_safe_url, current, resolver=resolver)

        if robots is not None and not await robots.allows(
            current,
            client=client,
            user_agent=user_agent,
            timeout_s=timeout_s,
            resolver=resolver,
        ):
            raise RobotsDisallowedError(
                f"robots.txt disallows {current} for {user_agent!r}", url=current
            )

        outcome = await _request(
            current,
            client=client,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            user_agent=user_agent,
            requested=requested,
            chain=chain,
            require_html=require_html,
        )

        if isinstance(outcome, FetchResult):
            return outcome

        chain.append(current)
        current = outcome

    raise TooManyRedirectsError(
        f"more than {max_redirects} redirects starting at {requested}", url=requested
    )


async def _request(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_s: float,
    max_bytes: int,
    user_agent: str,
    requested: str,
    chain: Sequence[str],
    require_html: bool,
) -> FetchResult | str:
    """Issue one request. Returns a `FetchResult`, or the next URL on a redirect."""
    headers = {"user-agent": user_agent}

    try:
        async with client.stream(
            "GET",
            url,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_s),
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                location: str | None = response.headers.get("location")
                if not location:
                    raise HttpStatusError(
                        f"{response.status_code} with no Location header",
                        url=url,
                        status=response.status_code,
                    )
                return urljoin(url, location.strip())

            if response.status_code >= 400:
                raise HttpStatusError(
                    f"HTTP {response.status_code} for {url}",
                    url=url,
                    status=response.status_code,
                )

            content_type = response.headers.get("content-type")
            if require_html and not _is_html(content_type):
                raise NotHtmlError(
                    f"content-type {content_type or '(none)'!r} is not HTML", url=url
                )

            _assert_declared_size(response.headers.get("content-length"), max_bytes, url)

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise TooLargeError(f"response body exceeded {max_bytes} bytes", url=url)

            encoding = _encoding_for(response)
            return FetchResult(
                requested_url=requested,
                url=url,
                status=response.status_code,
                content_type=content_type,
                html=bytes(body).decode(encoding, errors="replace"),
                encoding=encoding,
                bytes_read=len(body),
                redirect_chain=list(chain),
            )

    except httpx.TimeoutException as exc:
        raise TimeoutError(f"timed out after {timeout_s}s fetching {url}", url=url) from exc
    except httpx.HTTPError as exc:
        raise NetworkError(f"{type(exc).__name__} fetching {url}: {exc}", url=url) from exc


def _assert_declared_size(header: str | None, max_bytes: int, url: str) -> None:
    """Refuse an oversize response before reading its body.

    A lying or absent `content-length` is handled by the streaming check; this is
    the cheap path that avoids transferring the bytes at all.
    """
    if header is None:
        return
    try:
        declared = int(header)
    except ValueError:
        return
    if declared > max_bytes:
        raise TooLargeError(f"content-length {declared} exceeds the {max_bytes} byte cap", url=url)


def _is_html(content_type: str | None) -> bool:
    """Whether this response is worth parsing as HTML.

    A missing content-type is accepted: plenty of small origins omit it, and
    refusing would cost real pages. A *wrong* one (pdf, image, json) is refused.
    """
    if not content_type:
        return True
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type in _HTML_TYPES


def _encoding_for(response: httpx.Response) -> str:
    """Charset from the response headers, defaulting to UTF-8.

    Only the header is consulted, not a `<meta charset>` in the body: the body is
    not decoded yet, and every mis-decoded byte becomes U+FFFD rather than an
    exception. Legacy pages that declare their charset only in markup will read
    slightly wrong; that is a known, bounded compromise.
    """
    declared = response.charset_encoding
    if not declared:
        return "utf-8"
    try:
        "".encode(declared)
    except LookupError:
        return "utf-8"
    return declared


def facts_from_fetch(result: FetchResult) -> PageFacts:
    """Convenience for the common `fetch_page` then `parse_page` pairing."""
    return parse_page(result.html, result.url, status=result.status)
