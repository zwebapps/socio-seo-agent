"""Bring a piece of copy inside a channel's hashtag range."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Matches the rubric's parser (`evals/rubric._HASHTAG_RE`) so that what this
#: removes is exactly what the scorer counts. If the two ever diverge, enforcement
#: would "fix" text the scorer still fails, which is the worst of both.
_HASHTAG_RE: Final = re.compile(r"#\w+", re.UNICODE)

#: URLs are found first and excluded, so a fragment (`.../page#section`) is never
#: mistaken for a hashtag and mangled into a broken link.
_URL_RE: Final = re.compile(r"https?://\S+", re.IGNORECASE)

#: Collapses the whitespace a removal leaves behind, without touching newlines --
#: paragraph structure is part of the deliverable.
_RUN_OF_SPACES_RE: Final = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE: Final = re.compile(r"[ \t]+([,.;:!?])")
_BLANK_LINES_RE: Final = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class HashtagEnforcement:
    """The corrected text plus what had to be done to it.

    `removed` and `shortfall` are reported rather than swallowed because they are
    evidence about the *model*. A caller that shows `format: 1.00` without also
    showing that enforcement rewrote nine of twenty pieces is reporting the
    renderer's competence as though it were the model's.
    """

    text: str
    removed: int
    shortfall: int
    kept: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """Whether the text was modified at all."""
        return self.removed > 0


def enforce_hashtags(
    text: str,
    *,
    minimum: int = 0,
    maximum: int = 0,
    protect: Sequence[re.Pattern[str]] = (),
) -> HashtagEnforcement:
    """Trim hashtags to at most `maximum`, keeping the earliest ones.

    `protect` names regions this must not touch, in addition to the URLs it always
    protects. It exists because `#` is not only a hashtag marker, and a caller
    knows its own notation better than this module does. The evaluation harness
    passes its citation pattern for exactly this reason: its chunk ids look like
    `plumber-01#0`, so an unprotected pass rewrote `[chunk:plumber-01#0]` into
    `[chunk:plumber-01]` -- silently converting a correct citation into a
    fabricated one, which the 2026-08-19 run measured as grounding collapsing from
    0.35 to 0.03. Stripping a character out of an identifier is the kind of damage
    a formatter must never do.

    Keeping the *earliest* rather than the last is deliberate: a model that
    over-tags usually appends a block of filler at the end, while a hashtag used
    early is more often inside a sentence and carries meaning. Removing from the
    end therefore damages the copy least.

    A shortfall is **never** invented. If a channel wants at least three hashtags
    and the model produced one, this reports `shortfall=2` and leaves the text
    alone: fabricating `#Zahnarzt` here would be this module writing marketing
    copy, which is the one thing an engine must not do. The caller decides whether
    to re-prompt or to ship short.

    Raises `ValueError` if `minimum > maximum`, which is an impossible spec rather
    than a piece of bad copy.
    """
    if minimum < 0 or maximum < 0:
        raise ValueError(f"hashtag bounds cannot be negative: {minimum=}, {maximum=}")
    if minimum > maximum:
        raise ValueError(f"impossible hashtag spec: {minimum=} exceeds {maximum=}")

    protected = [(m.start(), m.end()) for m in _URL_RE.finditer(text)]
    for pattern in protect:
        protected.extend((m.start(), m.end()) for m in pattern.finditer(text))

    def is_protected(start: int, end: int) -> bool:
        return any(p_start <= start and end <= p_end for p_start, p_end in protected)

    matches = [m for m in _HASHTAG_RE.finditer(text) if not is_protected(m.start(), m.end())]
    found = len(matches)

    if found <= maximum:
        return HashtagEnforcement(
            text=text,
            removed=0,
            shortfall=max(0, minimum - found),
            kept=tuple(m.group(0) for m in matches),
        )

    doomed = matches[maximum:]
    kept = tuple(m.group(0) for m in matches[:maximum])

    # Rebuild right-to-left so earlier offsets stay valid.
    result = text
    for match in reversed(doomed):
        result = result[: match.start()] + result[match.end() :]

    result = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", result)
    result = _RUN_OF_SPACES_RE.sub(" ", result)
    result = _BLANK_LINES_RE.sub("\n\n", result)
    result = "\n".join(line.rstrip() for line in result.split("\n")).strip()

    return HashtagEnforcement(
        text=result,
        removed=len(doomed),
        shortfall=max(0, minimum - len(kept)),
        kept=kept,
    )
