"""Short-link codes, per-channel UTM tags, and bot detection.

Why this exists at all is docs/CHANNELS.md section 1: **an Instagram feed caption
and a TikTok caption do not render a clickable link.** A UTM-tagged URL pasted
into either one is not a broken link, it is no link at all, so attribution cannot
be a property of publishing. It has to be a property of a redirect we own. That is
what makes the short link the highest-leverage component in the product: it works
whether we published the post, the owner pasted the caption by hand, or the click
came from a channel we cannot publish to.

Everything in this module is a pure function -- no clock, no database, no request.
The three decisions worth defending:

**The alphabet excludes ``0O1lI``.** A code is read off a printed flyer and typed
back in, and those are the characters people mistype. Fifty-six symbols at the
default length of eight is about 46 bits, so a code is not enumerable either --
which matters, because :func:`~backend.app.db.adapters.lead_store.PostgresLeadStore.resolve`
treats possession of a code as the whole authorisation for an unauthenticated
lookup.

**An unknown channel raises.** Channels are produced by our own code, so an
unfamiliar one is a typo. Silently filing it under a default medium would put
those clicks in a bucket that means something else, and the channel comparison
would stop being a comparison while still rendering a number.

**A user agent is used and thrown away.** :func:`is_bot` takes the string,
returns a boolean, and keeps nothing. The caller stores the boolean. A hashed UA
is a weak fingerprint and an IP is personal data; neither is needed to attribute a
lead to a piece of content, so neither is kept -- see ``LinkClick`` in
``backend/app/db/models.py``. Note that docs/CHANNELS.md section 5 still lists
``ua_hash`` in the recorded shape; the model is the newer decision and it wins.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "KNOWN_CHANNELS",
    "MAX_CODE_LENGTH",
    "MIN_CODE_LENGTH",
    "apply_utm",
    "build_utm",
    "is_bot",
    "new_code",
    "require_http_url",
]

#: Base 56: digits and letters, minus the pairs a human misreads (``0``/``O``,
#: ``1``/``l``/``I``). A mistyped code is a 404 on a link somebody printed.
CODE_ALPHABET: Final = "23456789abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"

#: 56**8, about 46 bits. Short enough to print, long enough that scanning the
#: keyspace is not a plan.
CODE_LENGTH: Final = 8

#: 56**6 is about 35 bits, which is the floor at which enumeration stops being
#: cheap. Below it the code stops being a credential, and it is one.
MIN_CODE_LENGTH: Final = 6

#: ``short_links.code`` is ``String(16)``. Longer would fail in the driver with a
#: message about a value nobody can locate.
MAX_CODE_LENGTH: Final = 16


#: ``channel -> (utm_source, utm_medium)``.
#:
#: ``google_ads`` is the one entry whose names are not its own key:
#: ``source=google&medium=cpc`` is the convention every analytics tool already
#: groups by, and inventing ``source=google_ads`` would isolate paid search from
#: the organic rows it is supposed to be compared against.
#:
#: ``instagram`` and ``instagram_story`` stay separate on purpose. A feed caption
#: cannot carry a link and a Story sticker can, so averaging them would describe
#: neither surface. ``link_hub`` is the bio-link page itself -- the only route
#: Instagram feed and TikTok have at all.
_CHANNEL_TAGS: Final[Mapping[str, tuple[str, str]]] = {
    "facebook": ("facebook", "social_organic"),
    "instagram": ("instagram", "social_organic"),
    "instagram_story": ("instagram_story", "social_organic"),
    "tiktok": ("tiktok", "social_organic"),
    "linkedin": ("linkedin", "social_organic"),
    "youtube": ("youtube", "social_organic"),
    "link_hub": ("link_hub", "referral"),
    "email": ("email", "email"),
    "google_ads": ("google", "cpc"),
}

#: The channels this build knows how to tag. Exported so a caller can validate a
#: channel without catching an exception.
KNOWN_CHANNELS: Final[frozenset[str]] = frozenset(_CHANNEL_TAGS)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def new_code(*, length: int = CODE_LENGTH) -> str:
    """Return a fresh, unguessable short-link code.

    ``secrets``, not ``random``: the code is the only thing standing between an
    anonymous caller and one row of another business's ``short_links``, so a
    seedable PRNG would make the whole keyspace reproducible.

    Uniqueness is *probabilistic here and enforced in the database* -- the unique
    index on ``short_links.code`` is what actually guarantees it, and the store
    retries on the conflict.
    """
    if not MIN_CODE_LENGTH <= length <= MAX_CODE_LENGTH:
        raise ValueError(
            f"length must be between {MIN_CODE_LENGTH} and {MAX_CODE_LENGTH}, got {length}. "
            f"Shorter is enumerable; longer does not fit short_links.code (String(16))."
        )
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def slugify(value: str) -> str:
    """Lowercase, ASCII-alphanumeric, hyphen-separated, no leading or trailing hyphen.

    Applied to ``utm_campaign`` and ``utm_content`` so that "Sommer Aktion" and
    "sommer-aktion" are one row in a report rather than two. Deliberately naive:
    it transliterates nothing, because a reversible mangling that everybody applies
    identically is more useful here than a prettier one applied inconsistently.
    """
    return _SLUG_STRIP.sub("-", value.strip().lower()).strip("-")


def build_utm(*, channel: str, campaign: str, content: str | None = None) -> dict[str, str]:
    """Return the UTM parameters for one channel, per docs/CHANNELS.md section 5.

    ``content`` is omitted entirely when there is no variant, rather than written
    as an empty string: an empty ``utm_content`` is its own bucket in every report
    that groups by it, so the empty value would show up as a third variant.

    Raises ``ValueError`` for an unknown channel or an empty campaign -- both are
    programming errors in the generator that called this, and both produce numbers
    that look real.
    """
    key = channel.strip().lower()
    tags = _CHANNEL_TAGS.get(key)
    if tags is None:
        known = ", ".join(sorted(KNOWN_CHANNELS))
        raise ValueError(f"unknown channel {channel!r}. Known channels: {known}.")

    campaign_slug = slugify(campaign)
    if not campaign_slug:
        raise ValueError(
            "campaign must contain at least one alphanumeric character: it is the "
            "only field that ties a click back to the content that earned it."
        )

    source, medium = tags
    utm = {
        "utm_source": source,
        "utm_medium": medium,
        "utm_campaign": campaign_slug,
    }
    content_slug = slugify(content) if content else ""
    if content_slug:
        utm["utm_content"] = content_slug
    return utm


def apply_utm(url: str, utm: Mapping[str, str]) -> str:
    """Merge ``utm`` into ``url``'s query string, replacing rather than appending.

    The merge is the whole point. A destination that already carries
    ``?utm_source=newsletter`` is the ordinary case, and appending a second
    ``utm_source`` yields a URL whose attribution depends on which duplicate the
    reader happens to take first -- so the same click is filed under two different
    channels by two different tools.

    Parameters we do not own are preserved in place, including legitimate repeats
    such as ``?tag=a&tag=b``. A key that arrives already duplicated *and* is being
    set here is collapsed to one. Path and fragment survive untouched, because the
    fragment is where a landing page anchors its form.

    Empty values are dropped, and the function is idempotent: applying the same
    tags twice returns the same string, which matters because a link is
    regenerated on every republish.
    """
    incoming = {key: value for key, value in utm.items() if value}
    parts = urlsplit(url)

    merged: list[tuple[str, str]] = []
    written: set[str] = set()
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in incoming:
            if key in written:
                continue
            merged.append((key, incoming[key]))
            written.add(key)
        else:
            merged.append((key, value))
    for key, value in incoming.items():
        if key not in written:
            merged.append((key, value))
            written.add(key)

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment))


def require_http_url(url: str) -> str:
    """Return ``url`` stripped, or raise if it is not a fetchable web address.

    This guard is load-bearing rather than defensive. A stored ``target_url``
    becomes the ``Location`` header of a public, unauthenticated 302, so:

    * ``javascript:`` or ``data:`` there is stored XSS served from our own domain,
      with our domain in the address bar;
    * a CR or LF is HTTP response splitting;
    * a scheme-relative ``//host/path`` reads as a path on some clients and as a
      host on others, which is an open redirect that depends on the reader.

    It belongs at the point the value is *accepted*, not the point it is served,
    so a bad target can never reach the table in the first place.
    """
    candidate = url.strip()
    if _CONTROL_CHARS.search(candidate):
        raise ValueError("url must not contain control characters")

    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"url must start with http:// or https://, got {candidate[:32]!r}")
    if not parts.netloc:
        raise ValueError("url must contain a host")
    return candidate


#: Substrings that identify a non-human fetch, matched case-insensitively.
#:
#: The link previewers are the entries that matter. Instagram feed and TikTok have
#: no clickable link at all, so the bio-link hub is their entire conversion path
#: -- and every paste of that URL into a chat app fetches it once, sometimes on
#: every view of the message. Counting those would inflate precisely the number
#: this product is judged on.
_BOT_MARKERS: Final[tuple[str, ...]] = (
    # Generic self-identification
    "bot",
    "crawler",
    "crawl",
    "spider",
    "slurp",
    "scraper",
    # Link previewers and chat unfurlers
    "facebookexternalhit",
    "whatsapp",
    "embedly",
    "quora link preview",
    "vkshare",
    "skypeuripreview",
    "nuzzel",
    "outbrain",
    "flipboard",
    "bitlybot",
    # Scripts and headless browsers
    "curl",
    "wget",
    "python-requests",
    "python-httpx",
    "python-urllib",
    "libwww-perl",
    "java/",
    "go-http-client",
    "okhttp",
    "axios",
    "node-fetch",
    "headless",
    "phantomjs",
    "puppeteer",
    "playwright",
    "selenium",
    # Monitoring and audit tools
    "pingdom",
    "uptimerobot",
    "statuscake",
    "lighthouse",
    "gtmetrix",
    "site24x7",
)


def is_bot(user_agent: str | None) -> bool:
    """Return whether this fetch should be excluded from the human click count.

    The string is inspected and discarded -- the caller stores only this boolean.

    **An absent or blank user agent counts as a bot.** Every mainstream browser
    sends one, so nothing means a script. That direction is chosen knowingly: the
    flag never affects what the visitor is served, so being wrong costs one
    uncounted click, while being wrong the other way inflates the click number
    that the entire lead loop reports.

    Substring matching, not a parsed UA database. A library here would be a
    dependency and a monthly data update in exchange for detail nobody uses: the
    only question asked of the answer is whether to add one to a counter.
    """
    if user_agent is None or not user_agent.strip():
        return True
    haystack = user_agent.lower()
    return any(marker in haystack for marker in _BOT_MARKERS)
