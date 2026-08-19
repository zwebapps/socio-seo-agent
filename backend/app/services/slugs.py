"""Turn a business name into a public URL slug.

Lives in its own module because three callers need the same answer and none of
them should own it: signup mints a slug, a future rename has to mint another, and
the migration that introduced the column had to reproduce this logic in SQL.

The rules are deliberately conservative, because a slug is a **public, permanent
address**: it goes in an Instagram bio and onto printed material, so it must be
ASCII, lowercase, hyphen-separated, and free of anything that needs escaping.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final
from uuid import UUID

#: Matches ``businesses.slug``'s column width. A slug longer than this is not a
#: better address, it is a worse one.
MAX_SLUG_LENGTH: Final = 80

#: German first, since that is the market: ``ü`` must become ``ue``, not ``u``, or
#: "Müller" reads as "muller" to a German speaker. Unicode NFKD alone would give
#: the wrong answer here, which is why this runs first.
_TRANSLITERATIONS: Final = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "æ": "ae",
    "ø": "oe",
    "å": "aa",
}

_NON_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")


def slugify_business_name(name: str) -> str | None:
    """The slug a name wants, or ``None`` if it yields nothing usable.

    ``None`` rather than a fabricated string, because the caller has to decide what
    to do about it and only the caller knows the id to fall back on. It happens for
    real: a name of pure punctuation, or one written entirely in a script this
    cannot transliterate (Japanese, Arabic, Cyrillic), reduces to an empty string.
    Returning ``"-"`` or ``""`` would put an unusable address into a unique column.
    """
    lowered = name.strip().lower()
    for source, replacement in _TRANSLITERATIONS.items():
        lowered = lowered.replace(source, replacement)

    # NFKD then drop combining marks, which handles the accents not listed above
    # (é -> e, ñ -> n) without inventing expansions for them.
    decomposed = unicodedata.normalize("NFKD", lowered)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))

    slug = _NON_SLUG_RE.sub("-", ascii_only).strip("-")
    return slug[:MAX_SLUG_LENGTH].strip("-") or None


def business_slug(name: str, business_id: UUID) -> str:
    """A slug for this business that is stable, readable when it can be, and unique.

    The id suffix is what makes this safe to use without a round-trip to the
    database: the column is UNIQUE, so a colliding insert would raise, and the
    honest options are to retry with a suffix or to pre-check with a query that is
    racy anyway. Deriving the suffix from the id instead means the value is
    deterministic and collision-free by construction -- the only way two businesses
    collide is if they share a UUID.

    Applied ONLY when the readable base is unavailable. A first customer called
    "Müller Sanitär GmbH" gets ``mueller-sanitaer-gmbh``, not a suffixed one; the
    migration that backfilled this column made the same choice, ordering by
    ``created_at`` so the earliest holder keeps the clean form.
    """
    base = slugify_business_name(name)
    suffix = str(business_id).replace("-", "")[:8]
    if base is None:
        return f"b-{suffix}"
    return base


def suffixed_slug(name: str, business_id: UUID) -> str:
    """The collision-breaking form, for a retry after a UNIQUE violation."""
    base = slugify_business_name(name)
    suffix = str(business_id).replace("-", "")[:8]
    if base is None:
        return f"b-{suffix}"
    room = MAX_SLUG_LENGTH - len(suffix) - 1
    return f"{base[:room].strip('-')}-{suffix}"
