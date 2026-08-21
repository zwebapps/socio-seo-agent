"""Auditing the site the business ALREADY has, page by page and across pages.

Everything else in this engine scores a page the agent just wrote. This module
scores the pages the customer already owns, and that is a different job with a
different reason: `PROBLEM.md` states it plainly -- "the fastest leads are not from
new content, they come from fixing CONVERSION on pages that already get traffic",
because new Google content takes 6-12 weeks to rank at best. So the audit is the
first thing a run can hand back that is worth acting on the same day.

Three properties, and each one is a decision rather than an accident.

**It reads `crawl.PageFacts`, it does not re-parse HTML.** That contract's own
docstring says "`seo`, `geo` and the agent layer all read this; none of them re-parse
HTML", and this module is the newest reader of it. The practical gain is that the
audit costs nothing on top of the crawl HARVEST already performs, and a page cannot
be parsed two ways by two modules.

**Keyword rules are deliberately absent.** `rules.check_keyword_density` needs a
target keyword, and an existing page has none -- the business did not write it against
a brief. Scoring density against a keyword we invented would be marking their homework
against an answer we made up. What is audited is what is true of a page regardless of
intent: whether it says what it is about (title, meta), whether it is navigable
(headings, internal links), whether it is reachable (orphan), whether a machine can
read it (schema), and whether there is enough of it to rank at all.

**Cross-page findings are the point, not a bonus.** Duplicate titles, duplicate metas
and orphan pages are invisible to any single-page check, and they are among the most
common real defects on a small business's site -- a template that emits the same
`<title>` on every service page is one line of someone's theme and costs the site
every one of those pages in search.

Pure: no model, no database, no I/O. `tests/test_engine_boundary.py` walks this
module's AST and fails the build on a forbidden import.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from backend.app.engines.crawl.contract import PageFacts
from backend.app.engines.seo.rules import (
    META_TARGET_MAX,
    META_TARGET_MIN,
    MIN_INTERNAL_LINKS,
    TITLE_HARD_MAX,
    TITLE_HARD_MIN,
    TITLE_TARGET_MAX,
    TITLE_TARGET_MIN,
)

__all__ = [
    "THIN_CONTENT_WORDS",
    "AuditFinding",
    "PageAudit",
    "SiteAuditResult",
    "audit_site",
]

#: Below this, a page has too little text to rank for anything, whatever else is
#: right about it. 300 is the number `docs/CHANNELS.md` uses for the same judgement
#: about our own output ("a 300-word article cannot cover a commercial-intent query"),
#: and using one number for both means the audit holds the customer's pages to the
#: standard we hold ours to.
THIN_CONTENT_WORDS: Final = 300

#: How many example URLs a site-level finding names. Enough to act on, short enough
#: to read -- the same reasoning as `rules._MAX_HINT_EXAMPLES`.
_MAX_EXAMPLES: Final = 5

#: Severity ordering, so a summary can report the worst thing found without a caller
#: re-deriving what "worst" means.
_SEVERITY_RANK: Final[Mapping[str, int]] = {"error": 2, "warn": 1, "info": 0}


class AuditFinding(BaseModel):
    """One thing that is true of the site, and what to do about it.

    Deliberately NOT `SeoFinding`. That model's `code` is a closed `Literal` of the
    nine rules the draft scorer runs, and its `fix_hint` is written FOR A MODEL to
    act on in a retry loop. These findings are written for a PERSON to act on in
    their own CMS, and several of them (a duplicate title across four pages, an
    orphan page) have no equivalent in the draft scorer at all. Reusing the type
    would have meant widening a `Literal` that exists to keep the retry loop honest.
    """

    code: str
    severity: str
    #: What is true, in the words the owner reads.
    message: str
    #: What to do, naming the measured value and the target. A passing finding
    #: carries "".
    fix_hint: str
    measured: float | None = None
    expected: str = ""
    #: The pages this finding is about. Empty for a page-level finding, where the
    #: page is `PageAudit.url`; populated for a site-level one.
    urls: list[str] = Field(default_factory=list)


class PageAudit(BaseModel):
    """One page's verdict."""

    url: str
    title: str | None = None
    word_count: int = 0
    findings: list[AuditFinding] = Field(default_factory=list)

    @property
    def problems(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity != "info"]


class SiteAuditResult(BaseModel):
    """The audit of one site.

    `pages_crawled` and `truncated` travel with the result because a partial audit
    must never read as a complete one: "no duplicate titles found" across 8 of 200
    pages is not a finding, it is a sample, and the caller has to be able to say so.
    """

    start_url: str
    pages_crawled: int = 0
    truncated: bool = False
    pages: list[PageAudit] = Field(default_factory=list)
    #: Findings about the site as a whole, which no single page could produce.
    site_findings: list[AuditFinding] = Field(default_factory=list)

    @property
    def problem_count(self) -> int:
        return sum(len(p.problems) for p in self.pages) + len(
            [f for f in self.site_findings if f.severity != "info"]
        )

    @property
    def worst_severity(self) -> str:
        """The most serious severity present, or "info" when nothing is wrong."""
        worst = "info"
        for finding in (*self.site_findings, *(f for p in self.pages for f in p.findings)):
            if _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[worst]:
                worst = finding.severity
        return worst


def audit_site(
    start_url: str,
    pages: Sequence[PageFacts],
    *,
    truncated: bool = False,
) -> SiteAuditResult:
    """Audit a crawled site. Pure, and never raises.

    An empty `pages` is a legitimate input -- the crawl may have been refused by
    `robots.txt` or the site may be a single unreachable page -- and it produces a
    result with no findings rather than an exception. The CALLER says "the crawl
    found nothing", because only it knows why the crawl was empty; an engine
    inventing "your site has no pages" as a finding would be reporting our failure
    as the customer's defect.
    """
    audits = [_audit_page(page) for page in pages]
    return SiteAuditResult(
        start_url=start_url,
        pages_crawled=len(pages),
        truncated=truncated,
        pages=audits,
        site_findings=_site_findings(pages),
    )


# --------------------------------------------------------------------------- #
# per page
# --------------------------------------------------------------------------- #


def _audit_page(page: PageFacts) -> PageAudit:
    return PageAudit(
        url=page.url,
        title=page.title,
        word_count=page.word_count,
        findings=[
            _title_finding(page),
            _meta_finding(page),
            _h1_finding(page),
            _thin_content_finding(page),
            _image_alt_finding(page),
            _schema_finding(page),
            _internal_links_finding(page),
        ],
    )


def _title_finding(page: PageFacts) -> AuditFinding:
    title = (page.title or "").strip()
    if not title:
        return AuditFinding(
            code="title_missing",
            severity="error",
            message="This page has no title.",
            fix_hint=(
                f"Add a <title> of {TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} characters "
                "naming what the page is about and where you are."
            ),
            measured=0,
            expected=f"{TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} characters",
        )
    length = len(title)
    if length < TITLE_HARD_MIN or length > TITLE_HARD_MAX:
        return AuditFinding(
            code="title_length",
            severity="warn",
            message=(
                f"The title is {length} characters, which search results will cut off"
                if length > TITLE_HARD_MAX
                else f"The title is only {length} characters."
            ),
            fix_hint=(
                f"Rewrite the title to {TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} "
                f"characters (currently {length})."
            ),
            measured=length,
            expected=f"{TITLE_TARGET_MIN}-{TITLE_TARGET_MAX} characters",
        )
    return _passing("title_length", f"Title length is {length} characters.", measured=length)


def _meta_finding(page: PageFacts) -> AuditFinding:
    meta = (page.meta_description or "").strip()
    if not meta:
        return AuditFinding(
            code="meta_missing",
            severity="warn",
            message="This page has no meta description.",
            fix_hint=(
                f"Add a meta description of {META_TARGET_MIN}-{META_TARGET_MAX} "
                "characters. Without one, search engines quote arbitrary page text."
            ),
            measured=0,
            expected=f"{META_TARGET_MIN}-{META_TARGET_MAX} characters",
        )
    length = len(meta)
    if length < META_TARGET_MIN or length > META_TARGET_MAX:
        return AuditFinding(
            code="meta_length",
            severity="warn",
            message=f"The meta description is {length} characters.",
            fix_hint=(
                f"Rewrite it to {META_TARGET_MIN}-{META_TARGET_MAX} characters "
                f"(currently {length})."
            ),
            measured=length,
            expected=f"{META_TARGET_MIN}-{META_TARGET_MAX} characters",
        )
    return _passing("meta_length", f"Meta description is {length} characters.", measured=length)


def _h1_finding(page: PageFacts) -> AuditFinding:
    h1s = [h for h in page.h_tree if h.level == 1]
    count = len(h1s)
    if count == 0:
        return AuditFinding(
            code="h1_missing",
            severity="error",
            message="This page has no H1 heading.",
            fix_hint="Add exactly one H1 stating what the page offers.",
            measured=0,
            expected="exactly 1",
        )
    if count > 1:
        return AuditFinding(
            code="h1_multiple",
            severity="warn",
            message=f"This page has {count} H1 headings.",
            fix_hint=(
                f"Keep one H1 and demote the other {count - 1} to H2, so the page "
                "states one subject."
            ),
            measured=count,
            expected="exactly 1",
        )
    return _passing("h1", "Exactly one H1.", measured=1)


def _thin_content_finding(page: PageFacts) -> AuditFinding:
    words = page.word_count
    if words < THIN_CONTENT_WORDS:
        return AuditFinding(
            code="thin_content",
            severity="warn",
            message=f"This page has {words} words of text.",
            fix_hint=(
                f"Expand it past {THIN_CONTENT_WORDS} words (currently {words}), or "
                "accept that it will not rank for anything on its own."
            ),
            measured=words,
            expected=f"at least {THIN_CONTENT_WORDS} words",
        )
    return _passing("thin_content", f"{words} words of text.", measured=words)


def _image_alt_finding(page: PageFacts) -> AuditFinding:
    images = page.images
    if not images:
        return _passing("image_alt", "No images to describe.")
    # `alt is None` ONLY. `ImageRef`'s own docstring draws this distinction and says
    # why: an absent attribute is a finding, while `alt=""` is the CORRECT markup for
    # a decorative image, and "collapsing the two would make the seo engine report
    # false positives". An audit that tells an owner to describe their spacer GIFs is
    # an audit they stop reading.
    missing = [img for img in images if img.alt is None]
    if missing:
        return AuditFinding(
            code="image_alt",
            severity="warn",
            message=f"{len(missing)} of {len(images)} images have no alt attribute.",
            fix_hint=(
                f"Describe the {len(missing)} image(s) with no alt attribute. Alt text "
                "is what a screen reader announces and what image search reads. Leave "
                'alt="" only on images that carry no meaning.'
            ),
            measured=len(missing),
            expected="every meaningful image described",
        )
    return _passing("image_alt", f"All {len(images)} images have an alt attribute.", measured=0)


def _schema_finding(page: PageFacts) -> AuditFinding:
    if not page.jsonld_blocks:
        return AuditFinding(
            code="schema_missing",
            severity="warn",
            message="This page carries no structured data.",
            fix_hint=(
                "Add JSON-LD describing the business (LocalBusiness) or the page's "
                "subject. It is what lets an AI answer engine quote you accurately."
            ),
            measured=0,
            expected="at least one JSON-LD block",
        )
    return _passing(
        "schema",
        f"{len(page.jsonld_blocks)} structured-data block(s).",
        measured=len(page.jsonld_blocks),
    )


def _internal_links_finding(page: PageFacts) -> AuditFinding:
    count = len(page.internal_links)
    if count < MIN_INTERNAL_LINKS:
        return AuditFinding(
            code="internal_links",
            severity="warn",
            message="This page links nowhere else on the site.",
            fix_hint=(
                "Link to at least one other page. A page that links nowhere is a "
                "dead end for a visitor and for a crawler."
            ),
            measured=count,
            expected=f"at least {MIN_INTERNAL_LINKS}",
        )
    return _passing("internal_links", f"{count} internal link(s).", measured=count)


# --------------------------------------------------------------------------- #
# across pages — the findings a single-page check cannot see
# --------------------------------------------------------------------------- #


def _site_findings(pages: Sequence[PageFacts]) -> list[AuditFinding]:
    if not pages:
        return []
    findings = [
        _duplicate_finding(
            pages,
            key=lambda p: (p.title or "").strip(),
            code="duplicate_title",
            label="title",
            fix_hint=(
                "Give each page its own title. A template emitting one title across "
                "several pages makes them compete with each other in search."
            ),
        ),
        _duplicate_finding(
            pages,
            key=lambda p: (p.meta_description or "").strip(),
            code="duplicate_meta",
            label="meta description",
            fix_hint="Write a distinct meta description per page.",
        ),
    ]
    orphans = _orphans(pages)
    if orphans:
        findings.append(
            AuditFinding(
                code="orphan_pages",
                severity="warn",
                message=f"{len(orphans)} page(s) are not linked from any other page crawled.",
                fix_hint=(
                    "Link these from your navigation or from a related page. A page "
                    "nothing links to is one a visitor can only reach by knowing the URL."
                ),
                measured=len(orphans),
                expected="every page linked from at least one other",
                urls=orphans[:_MAX_EXAMPLES],
            )
        )
    else:
        findings.append(_passing("orphan_pages", "Every page is linked from another."))
    return findings


def _duplicate_finding(
    pages: Sequence[PageFacts],
    *,
    key: Callable[[PageFacts], str],
    code: str,
    label: str,
    fix_hint: str,
) -> AuditFinding:
    """Pages sharing one non-empty `key` value.

    Empty values are excluded rather than grouped: "four pages share the same empty
    title" is already reported per page as `title_missing`, and counting it twice
    would inflate the problem count with the same defect under two names.
    """
    groups: Counter[str] = Counter()
    for page in pages:
        value = key(page)
        if value:
            groups[value] += 1
    repeated = {value: count for value, count in groups.items() if count > 1}
    if not repeated:
        return _passing(f"{code}", f"Every page has its own {label}.")

    affected = [page.url for page in pages if key(page) in repeated]
    worst = max(repeated.values())
    return AuditFinding(
        code=code,
        severity="warn",
        message=(
            f"{len(affected)} pages share a {label} with another page "
            f"({len(repeated)} duplicated value(s), the worst on {worst} pages)."
        ),
        fix_hint=fix_hint,
        measured=len(affected),
        expected=f"a unique {label} per page",
        urls=affected[:_MAX_EXAMPLES],
    )


def _orphans(pages: Sequence[PageFacts]) -> list[str]:
    """Crawled pages that no other crawled page links to.

    The start page is never an orphan: it is where the crawl began, so it is
    reachable by definition, and reporting the homepage as unreachable would be
    obviously wrong to the person reading it.

    URLs are compared normalised -- scheme and host lowercased, a `www.` prefix and
    a trailing slash and any fragment removed -- because `/kontakt`, `/kontakt/` and
    `https://WWW.example.de/kontakt#top` are one page, and comparing them raw reports
    every page on a site with tidy trailing-slash links as an orphan.
    """
    if len(pages) < 2:
        return []
    linked: set[str] = set()
    for page in pages:
        for href in page.internal_links:
            linked.add(_normalise(href))
    start = _normalise(pages[0].url)
    return [
        page.url
        for page in pages
        if _normalise(page.url) != start and _normalise(page.url) not in linked
    ]


def _normalise(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def _passing(code: str, message: str, *, measured: float | None = None) -> AuditFinding:
    """A rule that holds.

    Passes are kept in the list for the same reason `SeoFindingCode`'s "info" exists:
    a checklist that shows only problems cannot be read as "everything else was
    checked and is fine", so an owner cannot tell a clean page from an unchecked one.
    """
    return AuditFinding(code=code, severity="info", message=message, fix_hint="", measured=measured)
