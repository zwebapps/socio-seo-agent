"""Auditing the site the business already has.

Every test here targets a failure that would read as a clean audit. A duplicate
`<title>` emitted by a theme across every service page is invisible to any
per-page check and is one of the most common real defects on a small business's
site. An orphan check that compares URLs raw reports every page on a site with
tidy trailing-slash links as unreachable. And a keyword rule applied to a page
the business wrote without a brief marks their homework against an answer we
invented.

The engine is pure, so these tests build `PageFacts` directly: no HTTP, no
fixture site, and no HTML parsing on the way in.
"""

from backend.app.engines.crawl.contract import Heading, ImageRef, PageFacts
from backend.app.engines.seo import THIN_CONTENT_WORDS, audit_site

BASE = "https://mueller-sanitaer.de"


def _page(
    path: str = "/",
    *,
    title: str | None = "Sanitärnotdienst Koblenz — Müller Sanitär, 24 Stunden",
    meta: str | None = None,
    h1s: int = 1,
    words: int = 900,
    internal: list[str] | None = None,
    images: list[ImageRef] | None = None,
    jsonld: list[dict[str, object]] | None = None,
) -> PageFacts:
    """One crawled page, defaulting to a page with nothing wrong with it."""
    return PageFacts(
        url=f"{BASE}{path}",
        title=title,
        meta_description=(
            meta
            if meta is not None
            else (
                "Sanitärnotdienst in Koblenz, 24 Stunden erreichbar. Festpreis vor "
                "Beginn der Arbeit, Rohrreinigung und Badsanierung vom "
                "Meisterbetrieb seit 1998."
            )
        ),
        h_tree=[Heading(level=1, text=f"H1 {i}") for i in range(h1s)],
        internal_links=internal if internal is not None else [f"{BASE}/kontakt"],
        images=images or [],
        word_count=words,
        jsonld_blocks=jsonld if jsonld is not None else [{"@type": "LocalBusiness"}],
    )


def _codes(findings: object) -> set[str]:
    assert isinstance(findings, list)
    return {f.code for f in findings if f.severity != "info"}


# --------------------------------------------------------------------------- #
# the audit says nothing when there is nothing to say
# --------------------------------------------------------------------------- #


def test_a_healthy_page_reports_no_problems_but_still_reports_the_checks() -> None:
    """Passes are kept deliberately.

    A checklist that lists only problems cannot be read as "everything else was
    checked and is fine", so an owner cannot tell a clean page from an unchecked one.
    """
    result = audit_site(BASE, [_page()])

    page = result.pages[0]
    assert page.problems == []
    assert page.findings, "a clean page must still show what was checked"
    assert result.worst_severity == "info"
    assert result.problem_count == 0


def test_an_empty_crawl_produces_no_findings_rather_than_blaming_the_customer() -> None:
    """`robots.txt` may have refused us. "Your site has no pages" would be reporting
    our own failure as their defect — the caller says why the crawl was empty."""
    result = audit_site(BASE, [])

    assert result.pages == []
    assert result.site_findings == []
    assert result.pages_crawled == 0


# --------------------------------------------------------------------------- #
# per page
# --------------------------------------------------------------------------- #


def test_a_missing_title_is_an_error_not_a_warning() -> None:
    """A page with no title has nothing to show in a search result at all."""
    result = audit_site(BASE, [_page(title=None)])

    finding = next(f for f in result.pages[0].findings if f.code == "title_missing")
    assert finding.severity == "error"
    assert result.worst_severity == "error"


def test_a_missing_h1_is_reported_and_several_h1s_are_reported_differently() -> None:
    """Two different defects, two different codes: one page is missing its subject,
    the other claims several. A single "h1" finding would make them indistinguishable
    in a report the owner is meant to act on."""
    none_ = audit_site(BASE, [_page(h1s=0)]).pages[0]
    many = audit_site(BASE, [_page(h1s=3)]).pages[0]

    assert "h1_missing" in _codes(none_.findings)
    assert "h1_multiple" in _codes(many.findings)


def test_thin_content_names_the_measured_count_and_the_target() -> None:
    """A fix hint without numbers is not actionable — the same rule the draft
    scorer's `fix_hint` follows."""
    result = audit_site(BASE, [_page(words=120)])

    finding = next(f for f in result.pages[0].findings if f.code == "thin_content")
    assert finding.measured == 120
    assert "120" in finding.fix_hint
    assert str(THIN_CONTENT_WORDS) in finding.fix_hint


def test_images_with_no_alt_attribute_are_counted_not_just_flagged() -> None:
    result = audit_site(
        BASE,
        [
            _page(
                images=[
                    ImageRef(src=f"{BASE}/a.jpg", alt="Ein Monteur bei der Arbeit"),
                    ImageRef(src=f"{BASE}/b.jpg", alt=None),
                    ImageRef(src=f"{BASE}/c.jpg", alt=None),
                ]
            )
        ],
    )

    finding = next(f for f in result.pages[0].findings if f.code == "image_alt")
    assert finding.measured == 2, "two of three, not 'some'"


def test_an_empty_alt_is_a_decorative_image_not_a_missing_description() -> None:
    """`ImageRef`'s docstring draws this distinction and says why: `alt=""` is the
    CORRECT markup for an image that carries no meaning, and collapsing it with a
    missing attribute makes the engine report false positives. An audit that tells an
    owner to describe their spacer GIFs is an audit they stop reading."""
    result = audit_site(
        BASE,
        [_page(images=[ImageRef(src=f"{BASE}/spacer.gif", alt="")])],
    )

    finding = next(f for f in result.pages[0].findings if f.code == "image_alt")
    assert finding.severity == "info"


def test_a_page_with_no_images_passes_rather_than_being_penalised() -> None:
    """Nothing to describe is not a defect."""
    result = audit_site(BASE, [_page(images=[])])

    finding = next(f for f in result.pages[0].findings if f.code == "image_alt")
    assert finding.severity == "info"


def test_missing_structured_data_is_reported_because_answer_engines_read_it() -> None:
    result = audit_site(BASE, [_page(jsonld=[])])

    assert "schema_missing" in _codes(result.pages[0].findings)


def test_a_page_that_links_nowhere_is_reported() -> None:
    result = audit_site(BASE, [_page(internal=[])])

    assert "internal_links" in _codes(result.pages[0].findings)


# --------------------------------------------------------------------------- #
# across pages — what a single-page check cannot see
# --------------------------------------------------------------------------- #


def test_a_title_repeated_across_pages_is_reported_with_the_pages_named() -> None:
    """The defect this engine exists for.

    One line of somebody's theme emits the same `<title>` on every service page, and
    every per-page check passes: the title is present and a sensible length. The
    pages then compete with each other in search.
    """
    shared = "Müller Sanitär GmbH — Ihr Meisterbetrieb in Koblenz seit 1998"
    result = audit_site(
        BASE,
        [
            _page("/", title=shared, internal=[f"{BASE}/bad", f"{BASE}/rohr"]),
            _page("/bad", title=shared),
            _page("/rohr", title=shared),
        ],
    )

    finding = next(f for f in result.site_findings if f.code == "duplicate_title")
    assert finding.severity == "warn"
    assert finding.measured == 3
    assert f"{BASE}/bad" in finding.urls, "the owner needs to know WHICH pages"


def test_a_repeated_meta_description_is_reported_separately_from_the_title() -> None:
    shared = (
        "Müller Sanitär GmbH ist Ihr Meisterbetrieb für Sanitär, Heizung und Bad "
        "in Koblenz und Umgebung. Rufen Sie uns an, wir sind 24 Stunden da."
    )
    result = audit_site(
        BASE,
        [
            _page("/", title="A" * 55, meta=shared, internal=[f"{BASE}/bad"]),
            _page("/bad", title="B" * 55, meta=shared),
        ],
    )

    assert "duplicate_meta" in _codes(result.site_findings)
    assert "duplicate_title" not in _codes(result.site_findings)


def test_pages_sharing_an_empty_title_are_not_also_reported_as_duplicates() -> None:
    """Otherwise one defect is counted twice under two names, and the problem count
    tells the owner their site is worse than it is. `title_missing` already says it."""
    result = audit_site(
        BASE,
        [
            _page("/", title=None, internal=[f"{BASE}/bad"]),
            _page("/bad", title=None),
        ],
    )

    assert "duplicate_title" not in _codes(result.site_findings)
    assert all("title_missing" in _codes(p.findings) for p in result.pages)


def test_a_page_nothing_links_to_is_reported_as_an_orphan() -> None:
    result = audit_site(
        BASE,
        [
            _page("/", internal=[f"{BASE}/bad"]),
            _page("/bad", internal=[f"{BASE}/"]),
            _page("/versteckt", internal=[f"{BASE}/"]),
        ],
    )

    finding = next(f for f in result.site_findings if f.code == "orphan_pages")
    assert finding.urls == [f"{BASE}/versteckt"]


def test_the_start_page_is_never_an_orphan() -> None:
    """It is where the crawl began, so it is reachable by definition — and telling
    an owner their homepage is unreachable is obviously wrong to them, which costs
    the whole report its credibility."""
    result = audit_site(BASE, [_page("/", internal=[f"{BASE}/bad"]), _page("/bad", internal=[])])

    finding = next(f for f in result.site_findings if f.code == "orphan_pages")
    assert BASE not in finding.urls
    assert f"{BASE}/" not in finding.urls


def test_a_trailing_slash_or_www_or_fragment_is_the_same_page_not_an_orphan() -> None:
    """Compared raw, every page on a site whose nav links carry trailing slashes
    reads as unreachable — a report full of false orphans, which is worse than no
    report because it trains the owner to ignore it."""
    result = audit_site(
        BASE,
        [
            _page("/", internal=[f"{BASE}/kontakt/", "https://WWW.mueller-sanitaer.de/bad#top"]),
            _page("/kontakt", internal=[f"{BASE}/"]),
            _page("/bad", internal=[f"{BASE}/"]),
        ],
    )

    finding = next(f for f in result.site_findings if f.code == "orphan_pages")
    assert finding.severity == "info", f"false orphans: {finding.urls}"


def test_a_single_page_site_reports_no_orphans() -> None:
    """With one page there is nothing to be linked from."""
    result = audit_site(BASE, [_page("/", internal=[])])

    finding = next(f for f in result.site_findings if f.code == "orphan_pages")
    assert finding.severity == "info"


# --------------------------------------------------------------------------- #
# honesty about partial audits
# --------------------------------------------------------------------------- #


def test_a_truncated_crawl_says_so_on_the_result() -> None:
    """ "No duplicate titles" across 8 of 200 pages is a sample, not a finding, and
    the caller has to be able to say which it is."""
    result = audit_site(BASE, [_page()], truncated=True)

    assert result.truncated is True
    assert result.pages_crawled == 1


def test_no_keyword_rule_is_applied_to_a_page_the_business_wrote() -> None:
    """Deliberate absence, asserted so it cannot be "helpfully" added later.

    `rules.check_keyword_density` needs a target keyword. An existing page has none
    — the business did not write it against a brief — so scoring density against a
    keyword we chose would be marking their homework against our own answer.
    """
    result = audit_site(BASE, [_page(words=40)])

    codes = {f.code for f in result.pages[0].findings}
    assert not any("keyword" in code for code in codes)
    assert not any("readability" in code for code in codes)
