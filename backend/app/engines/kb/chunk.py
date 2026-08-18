"""Text -> chunks, and the hash that stops us paying to embed the same text twice.

Pure and total: no I/O, no randomness, and no input that makes it loop forever.

The chunker's contract is two guarantees, and both exist because breaking either
one fails *silently*:

* **No chunk exceeds ``target_tokens``.** An oversized chunk is truncated by the
  embedding model, so the tail of the passage is unretrievable while the row
  still looks perfectly present in the table.
* **Consecutive chunks overlap by at most ``overlap_tokens``, and by something.**
  A fact that straddles a cut ("Wir kommen innerhalb von / sechzig Minuten") is
  retrievable from neither side unless the boundary is shared.

How it gets there: split the text at the largest boundary that fits -- paragraph,
then line, then sentence, then word, and only as a last resort mid-word -- then
greedily pack those units up to the ceiling, opening each new chunk with the tail
of the previous one. Splitting at semantic boundaries first is what keeps a chunk
readable to a human reviewing the trace, which matters because the retrieval trace
is a UI artifact.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Final

from .contract import (
    CHARS_PER_TOKEN,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    Chunk,
    tokens_for_chars,
)

#: Boundary levels, widest first. A piece too long for its level is re-split at
#: the next one; a piece with no boundary at any level is cut mid-word, which
#: keeps the ceiling guarantee for a 900-character URL or a chemical name.
#:
#: Each pattern captures its separator so the original spacing can be rebuilt
#: rather than guessed at.
_PARAGRAPH_SPLIT: Final = re.compile(r"(\n{2,})")
_LINE_SPLIT: Final = re.compile(r"(\n)")
_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?…])(\s+)")
_WORD_SPLIT: Final = re.compile(r"(\s+)")
_LEVELS: Final = (_PARAGRAPH_SPLIT, _LINE_SPLIT, _SENTENCE_SPLIT, _WORD_SPLIT)

_WHITESPACE_RUN: Final = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    """Approximate the token count of ``text``.

    Four characters per token, rounded up (:data:`CHARS_PER_TOKEN`). Deliberately
    not a real tokeniser: an exact count would tie this engine to one model's
    vocabulary, which is exactly the coupling an engine may not have.

    The same function decides where to cut and asserts the ceiling, so the two
    can never drift apart.
    """
    return tokens_for_chars(len(text))


def content_hash(text: str) -> str:
    """sha256 hex of ``text``, encoded UTF-8.

    Pinned to UTF-8 rather than the platform default so two workers on two
    machines agree. This is the dedup key: it makes "identical chunk never
    re-embedded" (docs/ARCHITECTURE.md section 6) a lookup rather than a hope.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Unit:
    """One indivisible piece of text, plus the separator that preceded it."""

    text: str
    join: str


def _canonical_join(separator: str) -> str:
    """Reduce a captured separator to one of ``""``, ``" "``, ``"\\n"``, ``"\\n\\n"``.

    Bounding the separator to two characters is what keeps the ceiling arithmetic
    honest: a unit is sized against ``target - overlap``, and an unbounded run of
    whitespace between two units could otherwise push the assembled chunk over
    the target.
    """
    if not separator:
        return ""
    if "\n\n" in separator or separator.count("\n") > 1:
        return "\n\n"
    if "\n" in separator:
        return "\n"
    return " "


def _split_units(text: str, max_chars: int, level: int = 0) -> list[_Unit]:
    """Break ``text`` into units of at most ``max_chars`` characters each."""
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [_Unit(text=text, join="")]
    if level >= len(_LEVELS):
        # No boundary left. Cut mid-word rather than emit an oversized chunk, and
        # join with "" so the pieces reassemble exactly.
        return [
            _Unit(text=text[start : start + max_chars], join="")
            for start in range(0, len(text), max_chars)
        ]

    parts = _LEVELS[level].split(text)
    if len(parts) == 1:
        return _split_units(text, max_chars, level + 1)

    units: list[_Unit] = []
    pending_join = ""
    for index, part in enumerate(parts):
        if index % 2 == 1:
            # A captured separator. Held until the next non-empty piece, so an
            # empty piece cannot swallow the spacing around it.
            pending_join = part
            continue
        if not part:
            continue
        sub_units = _split_units(part, max_chars, level + 1)
        if not sub_units:
            continue
        sub_units[0] = replace(sub_units[0], join=pending_join)
        units.extend(sub_units)
        pending_join = ""
    return units


def _tail(text: str, max_chars: int) -> str:
    """The last ``max_chars`` characters of ``text``, starting at a word boundary.

    Starting mid-word would put a fragment ("...nerhalb von") at the head of the
    next chunk, which reads as corruption to a human and embeds as noise.
    """
    if max_chars <= 0 or not text:
        return ""
    window = text[-max_chars:] if len(text) > max_chars else text
    boundary = _WHITESPACE_RUN.search(window)
    tail = window[boundary.end() :] if boundary is not None else window
    return tail.strip()


def _build_chunk(ordinal: int, text: str, carried: str) -> Chunk | None:
    """Finalise one chunk, or ``None`` if it holds nothing but whitespace."""
    stripped = text.strip()
    if not stripped:
        return None
    # Report only a carry that survived stripping intact; anything else would put
    # a number in the UI that does not match the text next to it.
    carried_tokens = estimate_tokens(carried) if carried and stripped.startswith(carried) else 0
    return Chunk(
        ordinal=ordinal,
        text=stripped,
        token_estimate=estimate_tokens(stripped),
        content_hash=content_hash(stripped),
        carried_tokens=carried_tokens,
    )


def chunk_text(
    text: str,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Split extracted text into overlapping chunks of at most ``target_tokens``.

    Expects text that has already been through
    ``extract.normalise_text`` (every :class:`ExtractionResult` has been); only
    leading and trailing whitespace is handled here.

    Raises ``ValueError`` for arguments that cannot be satisfied -- a non-positive
    target, a negative overlap, or an overlap at least as large as the target,
    which would let a chunk consist entirely of carried text and make no
    progress. Failing at the call is better than returning a plausible list from
    a nonsense request.
    """
    if target_tokens < 1:
        raise ValueError(f"target_tokens must be at least 1, got {target_tokens}.")
    if overlap_tokens < 0:
        raise ValueError(f"overlap_tokens must not be negative, got {overlap_tokens}.")
    if overlap_tokens >= target_tokens:
        raise ValueError(
            f"overlap_tokens ({overlap_tokens}) must be smaller than target_tokens "
            f"({target_tokens}): an overlap that fills a chunk would carry the "
            "previous chunk forever and never advance through the document."
        )

    source = text.strip()
    if not source:
        return []

    target_chars = target_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    # A unit is capped so that carry + separator + unit still fits the target.
    max_unit_chars = max(1, target_chars - overlap_chars)

    units = _split_units(source, max_unit_chars)
    chunks: list[Chunk] = []
    buffer = ""
    carried = ""
    index = 0

    while index < len(units):
        unit = units[index]
        join = _canonical_join(unit.join) or " "

        if not buffer:
            # Opening a chunk: the carry comes first, then this unit. If the pair
            # will not fit, the carry is dropped -- losing overlap is a small
            # cost, exceeding the ceiling is a silent truncation.
            candidate = f"{carried}{join}{unit.text}" if carried else unit.text
            if len(candidate) > target_chars:
                carried = ""
                candidate = unit.text
            buffer = candidate
            index += 1
            continue

        candidate = f"{buffer}{join}{unit.text}"
        if len(candidate) <= target_chars:
            buffer = candidate
            index += 1
            continue

        # Full. Flush, carry the tail forward, and re-offer this unit as the
        # opener of the next chunk -- `index` deliberately does not advance, and
        # the branch above always advances it, so the loop cannot stall.
        built = _build_chunk(len(chunks), buffer, carried)
        if built is not None:
            chunks.append(built)
        carried = _tail(buffer, overlap_chars)
        buffer = ""

    if buffer:
        built = _build_chunk(len(chunks), buffer, carried)
        if built is not None:
            chunks.append(built)

    return chunks
