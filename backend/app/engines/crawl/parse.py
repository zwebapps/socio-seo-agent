"""HTML -> `PageFacts`. Pure: no network, no clock, no randomness, no I/O.

Structure (title, meta, headings, links, images, JSON-LD) comes from
BeautifulSoup + lxml, because those are exactly the facts the seo engine scores
and they must be read from the markup as written. Readable body text comes from
trafilatura, because separating article prose from navigation, cookie banners and
footers is a solved problem that a hand-rolled heuristic solves badly — and
`word_count` is only meaningful over the prose.

Everything here is total: malformed markup, a missing `<head>`, a JSON-LD block
containing a trailing comma — none of them raise. A crawler that dies on one bad
page is a crawler that never finishes a real website.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import trafilatura
from bs4 import BeautifulSoup, Tag

from .contract import Heading, ImageRef, PageFacts

_PARSER = "lxml"
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_WHITESPACE = re.compile(r"\s+")

#: Schemes that are legitimate in an `href` but are not pages we could crawl.
_NON_PAGE_SCHEMES = frozenset({"mailto", "tel", "javascript", "data", "sms", "callto", "file"})


def parse_page(html: str, url: str, *, status: int = 200) -> PageFacts:
    """Extract every deterministic fact from one HTML document.

    `status` is keyword-only with a default so the documented signature
    `parse_page(html, url)` holds for pure-parsing callers, while `fetch_page`'s
    caller can stamp the real status without a second model copy.
    """
    soup = BeautifulSoup(html, _PARSER)

    main_text = _main_text(html, url)
    internal, external = _links(soup, url)

    return PageFacts(
        url=url,
        status=status,
        title=_title(soup),
        meta_description=_meta_content(soup, "description"),
        canonical=_canonical(soup, url),
        lang=_lang(soup),
        robots_meta=_meta_content(soup, "robots"),
        h_tree=_headings(soup),
        internal_links=internal,
        external_links=external,
        images=_images(soup, url),
        word_count=len(main_text.split()),
        main_text=main_text,
        jsonld_blocks=_jsonld(soup),
    )


# --------------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------------- #


def _clean(value: str | None) -> str | None:
    """Collapse whitespace; treat an empty result as absent."""
    if value is None:
        return None
    collapsed = _WHITESPACE.sub(" ", value).strip()
    return collapsed or None


def _attr(tag: Tag, name: str) -> str | None:
    """Read one attribute as a string.

    bs4 returns a list for multi-valued attributes (`rel`, `class`), so this
    normalises both shapes rather than making every call site handle it.
    """
    value = tag.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return " ".join(str(item) for item in value)


def _title(soup: BeautifulSoup) -> str | None:
    tag = soup.find("title")
    if not isinstance(tag, Tag):
        return None
    return _clean(tag.get_text())


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    """First `<meta name=...>` content, matching the name case-insensitively.

    Real pages ship `<meta Name="Description">`; bs4's attribute matching is
    case-sensitive on values, so the scan is explicit.
    """
    target = name.lower()
    for tag in soup.find_all("meta"):
        candidate = _attr(tag, "name")
        if candidate is not None and candidate.strip().lower() == target:
            return _clean(_attr(tag, "content"))
    return None


def _canonical(soup: BeautifulSoup, base: str) -> str | None:
    for tag in soup.find_all("link"):
        rel = _attr(tag, "rel")
        if rel is None or "canonical" not in rel.lower().split():
            continue
        href = _clean(_attr(tag, "href"))
        if href is None:
            continue
        return urljoin(base, href)
    return None


def _lang(soup: BeautifulSoup) -> str | None:
    tag = soup.find("html")
    if not isinstance(tag, Tag):
        return None
    return _clean(_attr(tag, "lang"))


def _headings(soup: BeautifulSoup) -> list[Heading]:
    """Headings in document order, which is what makes the tree checkable."""
    headings: list[Heading] = []
    for tag in soup.find_all(list(_HEADING_TAGS)):
        text = _clean(tag.get_text())
        headings.append(Heading(level=int(tag.name[1]), text=text or ""))
    return headings


def _images(soup: BeautifulSoup, base: str) -> list[ImageRef]:
    """Images with absolute `src`.

    A missing `alt` becomes `None`; a present-but-empty one stays `""`. See
    `ImageRef` for why that distinction is load-bearing.
    """
    images: list[ImageRef] = []
    for tag in soup.find_all("img"):
        src = _clean(_attr(tag, "src"))
        if src is None:
            continue
        raw_alt = _attr(tag, "alt")
        alt = None if raw_alt is None else _WHITESPACE.sub(" ", raw_alt).strip()
        images.append(ImageRef(src=urljoin(base, src), alt=alt))
    return images


def _links(soup: BeautifulSoup, base: str) -> tuple[list[str], list[str]]:
    """Split `<a href>` into internal and external, absolute and de-duplicated.

    Order is preserved (first appearance wins) so a crawl frontier built from
    this is deterministic, which is what makes `crawl_site` reproducible.
    """
    base_host = _site_key(urlsplit(base).hostname)
    internal: dict[str, None] = {}
    external: dict[str, None] = {}

    for tag in soup.find_all("a"):
        href = _attr(tag, "href")
        normalised = normalise_link(base, href)
        if normalised is None:
            continue
        host = _site_key(urlsplit(normalised).hostname)
        bucket = internal if host is not None and host == base_host else external
        bucket.setdefault(normalised, None)

    return list(internal), list(external)


def normalise_link(base: str, href: str | None) -> str | None:
    """Absolutise one `href`, or return `None` if it is not a crawlable page.

    Fragments are dropped and an empty path becomes `/`, so `example.com`,
    `example.com/` and `example.com/#top` are one URL in the frontier instead of
    three fetches of the same page.
    """
    if href is None:
        return None
    candidate = href.strip()
    if not candidate or candidate.startswith("#"):
        return None

    scheme = candidate.partition(":")[0].lower() if ":" in candidate.split("/")[0] else ""
    if scheme in _NON_PAGE_SCHEMES:
        return None

    parts = urlsplit(urljoin(base, candidate))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _site_key(host: str | None) -> str | None:
    """Compare hosts ignoring case and a leading `www.`.

    `www.example.com` and `example.com` are one site to every human and to
    Google; treating them as two would make an internal link look external and
    silently halve a crawl.
    """
    if not host:
        return None
    lowered = host.lower()
    return lowered.removeprefix("www.")


def _jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Parsed `application/ld+json` objects; malformed blocks are skipped.

    Structured data is frequently hand-edited and frequently broken. Raising here
    would let one stray comma in a customer's schema block cost them the whole
    page's facts, so a block that will not parse is simply not a fact.
    """
    blocks: list[dict[str, Any]] = []

    for tag in soup.find_all("script"):
        script_type = _attr(tag, "type")
        if script_type is None or script_type.strip().lower() != "application/ld+json":
            continue

        raw = tag.string if tag.string is not None else tag.get_text()
        text = raw.strip() if raw else ""
        if not text:
            continue

        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            continue

        blocks.extend(_flatten_jsonld(payload))

    return blocks


def _flatten_jsonld(payload: object) -> list[dict[str, Any]]:
    """A block may be one object, a list of objects, or an `@graph` wrapper."""
    if isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list) and len(payload) == 1:
            return [item for item in graph if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _main_text(html: str, url: str) -> str:
    """Readable body prose, via trafilatura, with a visible-text fallback.

    trafilatura returns `None` for pages with no article-like content (a link
    hub, a very short page). Falling back to stripped visible text keeps
    `word_count` and `main_text` meaningful rather than empty, and the fallback
    is deterministic too.

    The fallback parses its OWN soup rather than reusing the caller's. It has to
    strip `<script>`/`<style>` to avoid counting code as prose, and doing that
    to a shared tree silently emptied `jsonld_blocks` for exactly the pages
    trafilatura could not extract — a field's value depending on another field's
    extraction path is the kind of bug that is nearly invisible in production.
    """
    extracted = trafilatura.extract(
        html,
        url=url,
        favor_recall=True,
        include_comments=False,
        include_tables=True,
        output_format="txt",
    )
    if extracted:
        return extracted.strip()

    scratch = BeautifulSoup(html, _PARSER)
    for tag in scratch.find_all(["script", "style", "noscript", "template"]):
        tag.decompose()

    body = scratch.body if isinstance(scratch.body, Tag) else scratch
    return _WHITESPACE.sub(" ", body.get_text(" ")).strip()
