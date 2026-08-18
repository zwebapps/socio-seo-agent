"""Bytes -> text, one small function per format behind a registry.

Pure: no filesystem, no network, no clock, no randomness. The bytes arrive from
the service layer (which read them out of S3); this module only decodes.

Three things are deliberate:

* **Every extractor returns through :func:`finalise_extraction`.** That is the
  one place that decides ``ok`` versus ``no_text``, so a format added later
  cannot get the scanned-document case wrong. It is also the function the tests
  drive with a PDF-shaped empty result, since ``pypdf`` is not installed.
* **A missing dependency raises, it does not degrade.** ``pdf`` and ``docx`` are
  registered and raise :class:`ExtractorUnavailableError` naming the package they
  need. The registry with an honest gap is the point: the shape of the fix is
  visible, and no document is ever silently indexed as empty.
* **Nothing raises on bad input.** Malformed HTML, a NUL byte in the middle of a
  text file, Windows line endings, a UTF-8 BOM: all handled. A document ingest
  that dies on one bad upload is an ingest a customer cannot use.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable, Mapping
from typing import Final

from bs4 import BeautifulSoup

from .contract import (
    DocumentKind,
    ExtractionResult,
    ExtractorUnavailableError,
    UnsupportedDocumentKindError,
)

#: Byte-order marks, checked before the codec chain. A BOM is a positive
#: statement about the encoding, so trusting it is both faster and more accurate
#: than inference -- and it keeps the reported ``encoding`` honest: decoding a
#: BOM-less file with ``utf-8-sig`` succeeds and would report a BOM that was
#: never there.
_BOMS: Final[tuple[tuple[bytes, str], ...]] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

#: Codecs tried in order, strictly, for a file with no BOM: UTF-8 first, then the
#: one legacy encoding a German SMB's exports actually use.
#:
#: ``latin-1`` is deliberately absent even though it would guarantee a decode: it
#: can never fail, so including it would make ``status="failed"`` unreachable and
#: turn a corrupt download into mojibake indexed as fact. BOM-less UTF-16 is
#: absent for the opposite reason -- it decodes almost any even-length byte string
#: into plausible-looking nonsense, so it would shadow cp1252.
_CODECS: Final[tuple[str, ...]] = ("utf-8", "cp1252")

_PARSER: Final = "lxml"

#: Elements whose text is code or chrome, never content. Indexed JavaScript is
#: retrievable nonsense that dilutes the score of every real chunk.
_NON_CONTENT_TAGS: Final[tuple[str, ...]] = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "head",
)

#: Control characters that are not text. ``\n`` and ``\t`` survive; everything
#: else in the C0 range (NUL, form feed, vertical tab, stray CR) does not.
_CONTROL_CHARS: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRAILING_SPACES: Final = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUNS: Final = re.compile(r"\n{3,}")
_HORIZONTAL_RUNS: Final = re.compile(r"[ \t]{2,}")

#: Formats that are supported but not yet readable, and the package each needs.
#: Phase-gated in pyproject.toml ("Phase 3 langgraph, langchain-core, pypdf,
#: python-docx"), so this table is the honest gap, kept in one place.
MISSING_PACKAGES: Final[Mapping[DocumentKind, str]] = {
    "pdf": "pypdf",
    "docx": "python-docx",
}

#: Note attached to a ``no_text`` result. Names OCR explicitly, because the UI's
#: whole job at that point is to offer it.
NO_TEXT_NOTE: Final = (
    "No extractable text was found{where}. If this is a scanned or "
    "photographed document the text is an image, and OCR is needed before it "
    "can be indexed."
)


def normalise_text(text: str) -> str:
    """Canonicalise extracted text without changing what it says.

    Line endings to ``\\n``, control characters dropped, trailing spaces cut,
    runs of blank lines collapsed to one, and the whole thing stripped.

    Indentation at the start of a line is preserved, because a markdown code
    block or an indented price list carries meaning. Runs of spaces *inside* a
    line are preserved too, for the same reason -- HTML is the one format that
    collapses them, and it does so before calling this.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    without_control = _CONTROL_CHARS.sub("", unified)
    trimmed = _TRAILING_SPACES.sub("", without_control)
    collapsed = _BLANK_RUNS.sub("\n\n", trimmed)
    return collapsed.strip()


def finalise_extraction(
    text: str,
    *,
    kind: DocumentKind,
    filename: str | None = None,
    note: str | None = None,
    encoding: str | None = None,
) -> ExtractionResult:
    """Normalise ``text`` and decide between ``ok`` and ``no_text``.

    The single decision point for "did this document actually give us anything?".
    Every extractor returns through here, so the scanned-document case is
    implemented once rather than once per format.
    """
    normalised = normalise_text(text)
    if not normalised:
        where = f" in {filename!r}" if filename else ""
        return ExtractionResult(
            kind=kind,
            status="no_text",
            text="",
            note=note or NO_TEXT_NOTE.format(where=where),
            encoding=encoding,
        )
    return ExtractionResult(
        kind=kind,
        status="ok",
        text=normalised,
        note=note,
        encoding=encoding,
    )


def decode_bytes(data: bytes) -> tuple[str, str] | None:
    """Decode ``data``, trusting a BOM and otherwise trying codecs strictly.

    Returns ``(text, codec)``, where ``codec`` names what actually decoded the
    bytes -- reported to the UI, so it must be true rather than merely
    successful. ``None`` means every attempt refused, which is the only way to
    reach ``status="failed"`` for a text-shaped format.
    """
    for bom, codec in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(codec), codec
            except UnicodeDecodeError:
                # A BOM followed by bytes that are not that encoding: the file is
                # damaged. Fall through rather than trust the marker.
                break

    for codec in _CODECS:
        try:
            return data.decode(codec), codec
        except UnicodeDecodeError:
            continue
    return None


def _failed_decode(kind: DocumentKind, filename: str | None) -> ExtractionResult:
    where = f" of {filename!r}" if filename else ""
    return ExtractionResult(
        kind=kind,
        status="failed",
        text="",
        note=(
            f"Could not decode the bytes{where} as text. Tried "
            f"{', '.join(_CODECS)}. The file is probably binary, truncated, or "
            "was saved in an encoding we do not handle -- re-exporting it as "
            "UTF-8 will fix it. This is not a scanned document, so OCR will not help."
        ),
    )


# --------------------------------------------------------------------------- #
# One function per format
# --------------------------------------------------------------------------- #


def extract_plain_text(data: bytes, filename: str | None = None) -> ExtractionResult:
    """Decode a ``.txt`` upload."""
    decoded = decode_bytes(data)
    if decoded is None:
        return _failed_decode("txt", filename)
    text, codec = decoded
    return finalise_extraction(text, kind="txt", filename=filename, encoding=codec)


def extract_markdown(data: bytes, filename: str | None = None) -> ExtractionResult:
    """Decode a ``.md`` upload, keeping the markup.

    Markdown is *not* rendered to plain prose. ``# Leistungen`` and ``- Notdienst``
    are retrieval signal: the heading tells a grader what the passage is about,
    and the list marker keeps items from running together. Stripping them would
    throw away structure the author already provided for free.
    """
    decoded = decode_bytes(data)
    if decoded is None:
        return _failed_decode("md", filename)
    text, codec = decoded
    return finalise_extraction(text, kind="md", filename=filename, encoding=codec)


def extract_html(data: bytes, filename: str | None = None) -> ExtractionResult:
    """Pull the visible text out of an HTML upload.

    BeautifulSoup rather than trafilatura, which the crawl engine uses: trafilatura
    is tuned to isolate *article prose* from navigation and boilerplate, which is
    right for a crawled page and wrong for an uploaded document, where the price
    table in the footer may be the only thing worth indexing.

    Blocks are joined with a newline so ``<li>Notdienst</li><li>Wartung</li>``
    cannot become ``NotdienstWartung`` -- a fused token that matches no query.
    """
    decoded = decode_bytes(data)
    if decoded is None:
        return _failed_decode("html", filename)
    markup, codec = decoded

    soup = BeautifulSoup(markup, _PARSER)
    for tag_name in _NON_CONTENT_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    raw = soup.get_text("\n")
    # HTML is the one format where a run of spaces is layout, not content: source
    # indentation would otherwise survive into every chunk and inflate its tokens.
    lines = [_HORIZONTAL_RUNS.sub(" ", line).strip() for line in raw.split("\n")]
    text = "\n".join(line for line in lines if line)
    return finalise_extraction(text, kind="html", filename=filename, encoding=codec)


def extract_pdf(data: bytes, filename: str | None = None) -> ExtractionResult:
    """Not implemented: ``pypdf`` is not installed.

    When it is, the body becomes "read every page's text, join with a blank line,
    return through :func:`finalise_extraction`" -- and the scanned-PDF case is
    already handled, because a scan produces an empty string and the finaliser
    turns that into ``no_text`` with an OCR hint.
    """
    del data, filename
    raise ExtractorUnavailableError("pdf", MISSING_PACKAGES["pdf"])


def extract_docx(data: bytes, filename: str | None = None) -> ExtractionResult:
    """Not implemented: ``python-docx`` is not installed.

    When it is: paragraphs plus table cells, joined with a blank line, returned
    through :func:`finalise_extraction`. Tables matter -- a price list in a Word
    document is almost always a table.
    """
    del data, filename
    raise ExtractorUnavailableError("docx", MISSING_PACKAGES["docx"])


#: The registry. Adding a format is one entry plus one function, and the function
#: is required to return through :func:`finalise_extraction`.
Extractor = Callable[[bytes, str | None], ExtractionResult]

EXTRACTORS: Final[Mapping[DocumentKind, Extractor]] = {
    "txt": extract_plain_text,
    "md": extract_markdown,
    "html": extract_html,
    "pdf": extract_pdf,
    "docx": extract_docx,
}


def extract_text(
    data: bytes,
    *,
    kind: DocumentKind,
    filename: str | None = None,
) -> ExtractionResult:
    """Turn one uploaded file into text.

    Raises :class:`UnsupportedDocumentKindError` for a kind with no entry, and
    :class:`ExtractorUnavailableError` for a registered format whose library is
    missing. Everything else -- corrupt bytes, empty file, markup-only page --
    comes back as a typed result rather than an exception, because those are
    normal facts about real uploads.
    """
    extractor = EXTRACTORS.get(kind)
    if extractor is None:
        raise UnsupportedDocumentKindError(str(kind))
    return extractor(data, filename)
