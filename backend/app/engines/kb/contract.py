"""Typed contract for the kb engine: document bytes in, retrievable chunks out.

This module is types and constants only. It imports Pydantic and nothing else --
no LLM client, no DB session, no HTTP client -- which is what lets a service, an
agent prompt and a test all speak the same vocabulary without any of them
dragging in a vendor SDK (docs/ARCHITECTURE.md section 3, enforced by
tests/test_engine_boundary.py).

Two design points carry most of the weight:

* **``no_text`` is a status, not an empty ``ok``.** A scanned PDF parses
  perfectly and yields nothing. If that came back as ``ok`` with ``text=""`` the
  ingest path would write zero chunks, mark the document indexed, and tell the
  customer their price list is searchable when not one word of it is. So the
  three outcomes are named and kept apart: ``ok``, ``no_text`` (offer OCR),
  ``failed`` (offer a re-export).
* **A missing extractor raises, loudly, naming the package.** ``pdf`` and
  ``docx`` need ``pypdf`` and ``python-docx``, which are phase-gated in
  pyproject.toml. Returning empty text for them would be indistinguishable from
  a scan -- a registry with an honest gap beats a fake implementation.
"""

from __future__ import annotations

import math
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

#: Every upload format the engine knows about. Matches the values allowed by
#: ``documents.kind`` in backend/app/db/models.py, minus ``url`` -- a URL is the
#: crawl engine's input, not a byte payload this engine decodes.
DocumentKind = Literal["md", "txt", "html", "pdf", "docx"]

#: What extraction concluded.
#:
#: ``ok``      -- text was found; index it.
#: ``no_text`` -- the file parsed but holds no extractable text (a scan, an empty
#:                file, a page of markup). The UI should offer OCR.
#: ``failed``  -- the bytes could not be decoded or parsed at all. The UI should
#:                ask for a different export, not for OCR.
ExtractionStatus = Literal["ok", "no_text", "failed"]

#: Characters per token, for the only token arithmetic this engine does.
#:
#: A real tokeniser would be exact and would also make the engine depend on a
#: model's vocabulary -- which is precisely the coupling an engine may not have.
#: Four characters per token is the standard rule of thumb for English and German
#: prose. It is used for *both* deciding where to cut and asserting the ceiling,
#: so the two can never disagree; see ``chunk.estimate_tokens``.
CHARS_PER_TOKEN: Final = 4

#: Chunk size defaults. 512 tokens is large enough to hold a whole answer (an
#: opening-hours table, a service description) and small enough that a retrieved
#: chunk is mostly signal. 64 tokens of overlap is one or two sentences: enough
#: that a fact spanning a cut is still retrievable from at least one side.
DEFAULT_TARGET_TOKENS: Final = 512
DEFAULT_OVERLAP_TOKENS: Final = 64


class ExtractionResult(BaseModel):
    """The outcome of turning one uploaded file into text.

    Frozen: an engine that mutates its own output is no longer a pure function,
    and a caller that can edit ``status`` after the fact can turn a ``no_text``
    into an ``ok`` by accident.
    """

    model_config = ConfigDict(frozen=True)

    kind: DocumentKind
    status: ExtractionStatus
    #: Normalised text. Always ``""`` unless ``status`` is ``ok``.
    text: str = ""
    #: Human-readable explanation, shown in the UI. Required in spirit for
    #: ``no_text`` and ``failed``: a status with no explanation is a dead end for
    #: whoever has to decide what to do about the document.
    note: str | None = None
    #: Which codec decoded the bytes, for text-shaped formats. ``None`` for
    #: binary formats, where decoding is the parser's business.
    encoding: str | None = None

    @property
    def has_text(self) -> bool:
        """Whether there is anything worth chunking."""
        return self.status == "ok" and bool(self.text)

    @property
    def char_count(self) -> int:
        """Length of the extracted text, for the report and the UI."""
        return len(self.text)


class Chunk(BaseModel):
    """One passage, ready to embed.

    Frozen, and carries its own hash: the hash is what lets ingest skip an
    embedding call for text already indexed (docs/ARCHITECTURE.md section 6,
    "identical chunk never re-embedded"), and computing it here means no caller
    can hash a slightly different string than the one it stores.
    """

    model_config = ConfigDict(frozen=True)

    #: Position in the document, dense and zero-based. Pairs with
    #: ``kb_chunks.ordinal``, which is unique per document.
    ordinal: int
    text: str
    #: ``estimate_tokens(text)``. Stored so a caller can budget a prompt without
    #: recomputing, and so a test can assert the ceiling on the stored value.
    token_estimate: int
    #: sha256 hex of ``text``.
    content_hash: str
    #: How many tokens at the start of ``text`` were carried over from the
    #: previous chunk. ``0`` for the first chunk and whenever overlap is off.
    #: Rendered in the UI so a retrieved passage's leading sentence can be shown
    #: as context rather than as this chunk's own content.
    carried_tokens: int = 0


class KbEngineError(Exception):
    """Base class for every failure raised by the kb engine."""


class UnsupportedDocumentKindError(KbEngineError):
    """The requested kind is not in the registry at all.

    Distinct from :class:`ExtractorUnavailableError`: this is "we do not support
    spreadsheets", which is a product decision, not a missing dependency.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        supported = ", ".join(sorted(_kinds()))
        super().__init__(f"Unsupported document kind {kind!r}. Supported kinds: {supported}.")


class ExtractorUnavailableError(KbEngineError):
    """The format is supported, but the library that reads it is not installed.

    Raised rather than returning ``no_text``, because ``no_text`` means "this
    file genuinely has no text in it" and sends the user to OCR. Silently
    conflating a missing dependency with a scanned page would produce a document
    marked ``no_text`` that would have indexed perfectly on a machine with the
    dependency installed.
    """

    def __init__(self, kind: str, package: str) -> None:
        self.kind = kind
        self.package = package
        super().__init__(
            f"No extractor available for {kind!r}: the {package!r} package is not "
            f"installed. Add {package} to pyproject.toml and implement the "
            f"{kind} branch in backend/app/engines/kb/extract.py -- returning "
            "empty text here would be indistinguishable from a scanned document."
        )


def _kinds() -> tuple[str, ...]:
    """The literal values of :data:`DocumentKind`, for error messages."""
    return ("docx", "html", "md", "pdf", "txt")


def tokens_for_chars(char_count: int) -> int:
    """Token estimate for a string of ``char_count`` characters.

    Separated from ``estimate_tokens`` so the chunker can measure a candidate it
    has not built yet -- tracking a length is exact and cheap, rebuilding a
    string to measure it is neither.
    """
    if char_count <= 0:
        return 0
    return math.ceil(char_count / CHARS_PER_TOKEN)
