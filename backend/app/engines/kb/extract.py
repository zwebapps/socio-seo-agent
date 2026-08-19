"""Bytes -> text, one small function per format behind a registry.

Pure: no filesystem, no network, no clock, no randomness. The bytes arrive from
the service layer (which read them out of S3); this module only decodes.

Three things are deliberate:

* **Every extractor returns through :func:`finalise_extraction`.** That is the
  one place that decides ``ok`` versus ``no_text``, so a format added later
  cannot get the scanned-document case wrong. It is also what makes the
  scanned-PDF case free: a scan parses perfectly and yields an empty string, and
  the finaliser turns that into ``no_text`` with an OCR hint.
* **A missing dependency raises, it does not degrade.** ``pdf`` and ``docx`` need
  ``pypdf`` and ``python-docx``, which are phase-gated in pyproject.toml. Their
  parsing logic is written and tested; the library is imported LAZILY, on first
  use, and its absence raises :class:`ExtractorUnavailableError` naming the
  package to install. So the gap is honest and the fix is one line of
  pyproject.toml -- and no document is ever silently indexed as empty, which is
  what returning ``no_text`` for a missing library would do.
* **Nothing raises on bad input.** Malformed HTML, a NUL byte in the middle of a
  text file, Windows line endings, a UTF-8 BOM: all handled. A document ingest
  that dies on one bad upload is an ingest a customer cannot use.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from importlib import import_module
from io import BytesIO
from types import ModuleType
from typing import Any, Final

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

#: The module each format is imported as. Deliberately a SECOND table rather than a
#: reuse of :data:`MISSING_PACKAGES`: ``python-docx`` installs under one name and
#: imports under another (``docx``), and conflating the two produces the worst
#: possible message -- "install python-docx" on a machine where it is installed.
IMPORT_NAMES: Final[Mapping[DocumentKind, str]] = {
    "pdf": "pypdf",
    "docx": "docx",
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


def _require(kind: DocumentKind) -> ModuleType:
    """Import the library this format needs, or raise naming the package to install.

    **Imported lazily, and through ``importlib`` rather than a plain ``import``.**
    Both halves of that are deliberate:

    * *Lazily*, because ``pypdf`` and ``python-docx`` are phase-gated in
      pyproject.toml. A module-level import would make the whole kb engine
      unimportable -- and with it every other extractor, the chunker, and the
      service above them -- because one optional dependency is absent.
    * *Through ``importlib``*, because ``import pypdf`` does not type-check while
      the package is missing, and the usual remedy is worse than the disease: a
      ``# type: ignore[import-not-found]`` would itself become an error the day the
      package IS installed, since ``mypy --strict`` warns on unused ignores. A
      dynamic import type-checks identically in both states, so this file needs no
      edit when the dependency lands -- only pyproject.toml does.

    The refusal is an exception rather than an empty result. ``no_text`` means "this
    file genuinely has no text in it" and sends the user to OCR; returning it for a
    missing library would produce a document marked unscannable that would have
    indexed perfectly on a machine with the dependency installed.
    """
    try:
        return import_module(IMPORT_NAMES[kind])
    except ImportError as exc:
        # The message names the PACKAGE, not the module: nobody should have to know
        # that `docx` on the import line is `python-docx` on the install line.
        raise ExtractorUnavailableError(kind, MISSING_PACKAGES[kind]) from exc


def _failed_parse(kind: DocumentKind, filename: str | None, *, reason: str) -> ExtractionResult:
    """A binary file the library could not read.

    Distinct from :func:`_failed_decode`, which is about text encodings, and
    distinct from ``no_text``: this file did not parse at all, so OCR is not the
    answer -- a different export is.
    """
    where = f" of {filename!r}" if filename else ""
    return ExtractionResult(
        kind=kind,
        status="failed",
        text="",
        note=(
            f"The contents{where} could not be read as {kind.upper()}: {reason} "
            "This is not a scanned document, so OCR will not help -- please export "
            "the file again, or upload it in another format."
        ),
    )


def extract_pdf(data: bytes, filename: str | None = None) -> ExtractionResult:
    """Pull the text out of a PDF, one page at a time.

    Pages are joined with a blank line, which is the one structural fact a PDF
    reliably carries: it keeps the last sentence of page 3 from fusing into the
    first heading of page 4, and it gives the chunker a boundary it can prefer.

    **The scanned-PDF case needs no code here.** A scan parses perfectly and yields
    an empty string per page, so it arrives at :func:`finalise_extraction` as empty
    text and comes back as ``no_text`` with the OCR note -- which is exactly why
    that decision lives in one function rather than in each extractor.
    """
    pypdf = _require("pdf")
    reader: Any = None
    try:
        reader = pypdf.PdfReader(BytesIO(data))
        # `extract_text()` returns None for a page with no text layer in some pypdf
        # versions and "" in others; both mean the same thing here.
        pages = [str(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # a bad upload is a fact about the file, not a crash
        # An encrypted file is the one parse failure with a different remedy, so it
        # gets its own sentence. Checked AFTER the attempt, not before: pypdf opens
        # a file encrypted with an empty owner password on its own, and refusing
        # those up front would reject documents we can in fact read.
        if getattr(reader, "is_encrypted", False):
            return _failed_parse(
                "pdf",
                filename,
                reason="it is password-protected, so its pages cannot be opened.",
            )
        return _failed_parse("pdf", filename, reason=f"{type(exc).__name__}: {exc}.")

    return finalise_extraction("\n\n".join(pages), kind="pdf", filename=filename)


def _without_consecutive_repeats(values: Iterable[str]) -> Iterator[str]:
    """Drop each value that equals the one before it.

    python-docx reports a horizontally merged cell once per column it spans, so a
    two-column row with a merged header comes back as ``["Preise", "Preise"]``.
    Indexing the repeat would double that phrase's weight in retrieval for no
    reason.
    """
    previous: str | None = None
    for value in values:
        if value != previous:
            yield value
        previous = value


def extract_docx(data: bytes, filename: str | None = None) -> ExtractionResult:
    """Pull the text out of a Word document: paragraphs AND tables.

    **Tables are not optional.** A price list, an opening-hours grid and a service
    matrix in a ``.docx`` are almost always tables, and those are the passages a
    diner-facing question actually needs. python-docx's ``document.paragraphs``
    excludes anything inside a table, so a paragraphs-only implementation would
    silently drop the most valuable page of a price list and report ``ok``.

    Cells are joined with ``" | "`` so a row stays one retrievable line with its
    pairing intact: ``"Notdienst | 89,00 EUR"`` answers a question that
    ``"Notdienst"`` followed by ``"89,00 EUR"`` on separate lines does not.
    """
    docx = _require("docx")
    try:
        document = docx.Document(BytesIO(data))
        blocks: list[str] = ["\n".join(str(p.text) for p in document.paragraphs)]
        for table in document.tables:
            rows: list[str] = []
            for row in table.rows:
                cells = _without_consecutive_repeats(str(cell.text).strip() for cell in row.cells)
                line = " | ".join(cell for cell in cells if cell)
                if line:
                    rows.append(line)
            if rows:
                blocks.append("\n".join(rows))
    except Exception as exc:  # a bad upload is a fact about the file, not a crash
        return _failed_parse("docx", filename, reason=f"{type(exc).__name__}: {exc}.")

    text = "\n\n".join(block for block in blocks if block.strip())
    return finalise_extraction(text, kind="docx", filename=filename)


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
