"""The pure half of the lead loop: codes, UTM tags, and bot detection.

Written before the service. Nothing here touches a database, a request, or a
clock, because none of these four functions needs to -- which is the point of
keeping them in a service module rather than in the route that calls them.

The tests worth reading are the ones that encode a decision rather than a
signature:

* ``apply_utm`` must MERGE. A destination URL that already carries
  ``?utm_source=newsletter`` is the normal case, not the exotic one, and appending
  a second ``utm_source`` produces a URL where the analytics answer depends on
  which duplicate the reader picks first.
* ``is_bot`` must not flag a real browser, and must flag a link previewer. The
  Instagram and WhatsApp preview fetchers hit a bio link on every paste and every
  view of the message that contains it; counting those as human clicks would
  inflate exactly the number this whole loop exists to report honestly.
* An absent user agent is treated as a bot, and that direction is deliberate --
  see the test that says so.
"""

from __future__ import annotations

from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest

from backend.app.services.link_service import (
    CODE_ALPHABET,
    CODE_LENGTH,
    MAX_CODE_LENGTH,
    apply_utm,
    build_utm,
    is_bot,
    new_code,
    require_http_url,
)

# --------------------------------------------------------------------------- #
# new_code
# --------------------------------------------------------------------------- #


def test_new_code_has_the_default_length_and_only_uses_the_declared_alphabet() -> None:
    code = new_code()

    assert len(code) == CODE_LENGTH
    assert set(code) <= set(CODE_ALPHABET)


def test_new_code_alphabet_excludes_the_characters_people_misread() -> None:
    """A code gets read off a printed flyer and typed back in.

    ``0/O`` and ``1/l/I`` are the pairs that get mistyped, and a mistyped code is a
    404 on a link the business paid to print.
    """
    for character in "0O1lI":
        assert character not in CODE_ALPHABET


def test_new_code_does_not_repeat_across_many_draws() -> None:
    """Collision resistance, asserted rather than assumed.

    Five thousand draws from 56**8 should not collide. This is a smoke test for
    the generator being random at all -- a constant, or a counter seeded per
    process, fails here immediately.
    """
    codes = {new_code() for _ in range(5000)}

    assert len(codes) == 5000


def test_new_code_honours_an_explicit_length() -> None:
    assert len(new_code(length=12)) == 12


@pytest.mark.parametrize("length", [0, 5, MAX_CODE_LENGTH + 1])
def test_new_code_refuses_a_length_that_is_unsafe_or_unstorable(length: int) -> None:
    """Too short is guessable; too long does not fit ``short_links.code``.

    Both are programming errors, and both are cheaper as an exception here than as
    a driver-level string-too-long error or a quietly enumerable link.
    """
    with pytest.raises(ValueError, match="length"):
        new_code(length=length)


# --------------------------------------------------------------------------- #
# build_utm
# --------------------------------------------------------------------------- #


def test_build_utm_follows_the_per_channel_policy() -> None:
    utm = build_utm(channel="instagram", campaign="sommer-aktion", content="variant-a")

    assert utm == {
        "utm_source": "instagram",
        "utm_medium": "social_organic",
        "utm_campaign": "sommer-aktion",
        "utm_content": "variant-a",
    }


def test_build_utm_omits_content_when_there_is_no_variant() -> None:
    """An empty ``utm_content`` is not the same as no ``utm_content``.

    Writing the key with an empty value creates a second bucket in every report
    that groups by it.
    """
    assert "utm_content" not in build_utm(channel="facebook", campaign="sommer-aktion")


def test_build_utm_uses_the_conventional_source_and_medium_for_paid_search() -> None:
    """``google_ads`` is the one channel whose UTM names are not its own name.

    ``utm_source=google&utm_medium=cpc`` is the convention every analytics tool
    already understands; inventing ``source=google_ads`` would put paid search in
    a bucket of its own and break the comparison against organic.
    """
    utm = build_utm(channel="google_ads", campaign="notdienst")

    assert utm["utm_source"] == "google"
    assert utm["utm_medium"] == "cpc"


def test_build_utm_gives_email_its_own_medium() -> None:
    assert build_utm(channel="email", campaign="oktober")["utm_medium"] == "email"


def test_build_utm_keeps_the_two_instagram_surfaces_apart() -> None:
    """A feed caption carries no link and a Story sticker does.

    Collapsing both onto ``utm_source=instagram`` would average a channel that
    cannot convert together with one that can, and the resulting number would say
    nothing about either.
    """
    feed = build_utm(channel="instagram", campaign="c")
    story = build_utm(channel="instagram_story", campaign="c")

    assert feed["utm_source"] != story["utm_source"]


def test_build_utm_rejects_an_unknown_channel() -> None:
    """Channels are generated by our own code, so an unknown one is a typo.

    Defaulting it to ``referral`` would file the clicks under a bucket that means
    something else, and the channel comparison would quietly stop being a
    comparison.
    """
    with pytest.raises(ValueError, match="channel"):
        build_utm(channel="mastodon", campaign="c")


def test_build_utm_requires_a_campaign() -> None:
    with pytest.raises(ValueError, match="campaign"):
        build_utm(channel="facebook", campaign="   ")


def test_build_utm_normalises_a_campaign_into_a_stable_slug() -> None:
    """Two spellings of one campaign are two rows in every report.

    Normalising here -- rather than trusting whatever produced the string -- is
    what makes ``utm_campaign`` groupable at all.
    """
    utm = build_utm(channel="facebook", campaign="  Sommer Aktion 2026!  ")

    assert utm["utm_campaign"] == "sommer-aktion-2026"


def test_build_utm_normalises_the_channel_case() -> None:
    assert build_utm(channel="LinkedIn", campaign="c")["utm_source"] == "linkedin"


# --------------------------------------------------------------------------- #
# apply_utm -- the merge
# --------------------------------------------------------------------------- #


def test_apply_utm_adds_the_tags_to_a_bare_url() -> None:
    result = apply_utm(
        "https://mueller.example/notdienst",
        build_utm(channel="facebook", campaign="notdienst"),
    )

    query = parse_qs(urlsplit(result).query)
    assert query["utm_source"] == ["facebook"]
    assert query["utm_medium"] == ["social_organic"]
    assert query["utm_campaign"] == ["notdienst"]


def test_apply_utm_replaces_an_existing_tag_instead_of_duplicating_it() -> None:
    """The behaviour the whole function exists for.

    ``?utm_source=a&utm_source=b`` is a legal URL and a broken measurement: which
    value wins depends on the reader, so the same click is attributed differently
    by two tools.
    """
    result = apply_utm(
        "https://mueller.example/lp?utm_source=newsletter&utm_campaign=alt",
        {"utm_source": "instagram", "utm_campaign": "sommer"},
    )

    pairs = parse_qsl(urlsplit(result).query)
    assert [key for key, _ in pairs].count("utm_source") == 1
    assert dict(pairs)["utm_source"] == "instagram"
    assert dict(pairs)["utm_campaign"] == "sommer"


def test_apply_utm_collapses_a_url_that_already_carried_a_duplicate() -> None:
    """A URL that arrives already broken comes out fixed, not doubly broken."""
    result = apply_utm(
        "https://mueller.example/lp?utm_source=a&utm_source=b",
        {"utm_source": "tiktok"},
    )

    pairs = parse_qsl(urlsplit(result).query)
    assert pairs == [("utm_source", "tiktok")]


def test_apply_utm_preserves_query_parameters_it_does_not_own() -> None:
    result = apply_utm(
        "https://mueller.example/lp?ref=flyer&tag=a&tag=b",
        {"utm_source": "facebook"},
    )

    pairs = parse_qsl(urlsplit(result).query)
    assert ("ref", "flyer") in pairs
    assert pairs.count(("tag", "a")) == 1
    assert pairs.count(("tag", "b")) == 1


def test_apply_utm_preserves_path_and_fragment() -> None:
    """The fragment is where a landing page anchors its form.

    Dropping it sends the visitor to the top of the page instead of to the thing
    the CTA promised.
    """
    result = apply_utm("https://mueller.example/lp/notdienst#formular", {"utm_source": "youtube"})

    parts = urlsplit(result)
    assert parts.path == "/lp/notdienst"
    assert parts.fragment == "formular"


def test_apply_utm_ignores_empty_values() -> None:
    result = apply_utm("https://mueller.example/lp", {"utm_source": "facebook", "utm_content": ""})

    assert "utm_content" not in parse_qs(urlsplit(result).query)


def test_apply_utm_is_idempotent() -> None:
    """Tagging twice must not change the URL.

    A link can be regenerated -- on republish, on a retry -- and a URL that grows
    a parameter each time is a different URL each time, which breaks both the
    unique index and every cached report.
    """
    utm = build_utm(channel="facebook", campaign="notdienst", content="a")
    once = apply_utm("https://mueller.example/lp", utm)

    assert apply_utm(once, utm) == once


def test_apply_utm_encodes_a_value_that_would_otherwise_break_the_query() -> None:
    result = apply_utm("https://mueller.example/lp", {"utm_campaign": "a&b=c"})

    assert parse_qs(urlsplit(result).query)["utm_campaign"] == ["a&b=c"]


# --------------------------------------------------------------------------- #
# require_http_url
# --------------------------------------------------------------------------- #


def test_require_http_url_accepts_http_and_https() -> None:
    assert require_http_url("https://mueller.example/lp") == "https://mueller.example/lp"
    assert require_http_url(" http://mueller.example ") == "http://mueller.example"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "//mueller.example/lp",
        "https://",
        "",
    ],
)
def test_require_http_url_refuses_anything_that_is_not_a_web_address(url: str) -> None:
    """A stored target becomes a ``Location`` header on a public 302.

    ``javascript:`` in that header is a stored XSS with our own domain in the
    address bar, so the guard belongs at the point the value is accepted, not at
    the point it is served.
    """
    with pytest.raises(ValueError, match="url"):
        require_http_url(url)


@pytest.mark.parametrize("url", ["https://x.example/a\r\nX-Injected: 1", "https://x.example/\x00"])
def test_require_http_url_refuses_control_characters(url: str) -> None:
    """CR/LF in a redirect target is response splitting."""
    with pytest.raises(ValueError, match="url"):
        require_http_url(url)


# --------------------------------------------------------------------------- #
# is_bot
# --------------------------------------------------------------------------- #

REAL_BROWSERS = [
    # Desktop Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/131.0.0.0 Safari/537.36",
    # iPhone Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    # Android Firefox
    "Mozilla/5.0 (Android 14; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0",
    # The Instagram in-app browser -- a real person who tapped the bio link
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 Instagram 331.0.0.37.90",
    # The Facebook in-app browser
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/470.0.0.36.109]",
]

NOT_PEOPLE = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "WhatsApp/2.23.20.0 A",
    "TelegramBot (like TwitterBot)",
    "LinkedInBot/1.0 (compatible; Mozilla/5.0)",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Twitterbot/1.0",
    "curl/8.7.1",
    "Wget/1.21.4",
    "python-requests/2.32.3",
    "python-httpx/0.28.1",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
    "Mozilla/5.0 (compatible; SemrushBot/7~bl)",
    "Pingdom.com_bot_version_1.4",
]


@pytest.mark.parametrize("user_agent", REAL_BROWSERS)
def test_is_bot_leaves_a_real_browser_alone(user_agent: str) -> None:
    assert is_bot(user_agent) is False


@pytest.mark.parametrize("user_agent", NOT_PEOPLE)
def test_is_bot_flags_crawlers_previewers_and_scripts(user_agent: str) -> None:
    """Link previewers are the ones that actually matter here.

    Instagram and TikTok cannot carry a clickable link at all (docs/CHANNELS.md
    section 1), so the bio-link hub is the whole conversion path for those two
    channels -- and every paste of that URL into a chat app fetches it once. If
    those fetches counted, the number we report as "clicks from Instagram" would
    be mostly robots.
    """
    assert is_bot(user_agent) is True


@pytest.mark.parametrize("user_agent", [None, "", "   "])
def test_is_bot_treats_a_missing_user_agent_as_a_bot(user_agent: str | None) -> None:
    """Deliberate, and the safe direction.

    Every mainstream browser sends a user agent, so an absent one is a script.
    The flag never blocks the redirect -- the visitor is served either way -- so
    the only cost of being wrong is one uncounted click, against the benefit of
    not inflating the one number this product is judged on.
    """
    assert is_bot(user_agent) is True


def test_is_bot_is_case_insensitive() -> None:
    assert is_bot("CURL/8.7.1") is True
