"""The PDF and DOCX extractors, and the honest limit of what can be tested here.

``pypdf`` and ``python-docx`` are not installed in this build (they are phase-gated
in pyproject.toml), so there are two genuinely different things to test and only one
of them can be tested against the real libraries:

1. **The unavailable path.** Real, and it runs against reality: the import really
   fails, and :class:`ExtractorUnavailableError` really names the package. These
   tests skip themselves if the package ever IS installed, so they do not become
   quiet liars on the day it lands.

2. **The parsing logic.** Tested by putting a STUB module in ``sys.modules`` shaped
   like the real library's small public surface -- ``pypdf.PdfReader(stream).pages``
   with ``page.extract_text()``, and ``docx.Document(stream).paragraphs`` /
   ``.tables``. That is the same fake-provider discipline every external seam in
   this project uses, and it proves everything downstream of the library call: page
   joining, table flattening, the ``no_text`` decision, and the failure branches.

   **What it does NOT prove, stated plainly:** that our calls match the real
   libraries' behaviour, and that the libraries extract bytes correctly. A
   hand-built minimal PDF was considered and rejected as theatre -- without ``pypdf``
   installed there is nothing to feed it to, so the fixture would exercise no code.
   The first run with the real dependency is where assumption 2 gets confirmed, and
   the stub is written narrowly against the documented API so that run is boring.
"""

import sys
from importlib.util import find_spec
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from backend.app.engines.kb.contract import DocumentKind, ExtractorUnavailableError
from backend.app.engines.kb.extract import (
    MISSING_PACKAGES,
    extract_docx,
    extract_pdf,
    extract_text,
)

PDF_BYTES = b"%PDF-1.7 not really a pdf, the stub does not look at this"
DOCX_BYTES = b"PK\x03\x04 not really a docx"


# --------------------------------------------------------------------------- #
# The unavailable path -- run against the real (absent) libraries
# --------------------------------------------------------------------------- #


def _skip_if_installed(module_name: str) -> None:
    if find_spec(module_name) is not None:
        pytest.skip(
            f"{module_name} is installed, so the unavailable path cannot be exercised honestly here"
        )


@pytest.mark.parametrize(
    ("kind", "module_name"),
    [("pdf", "pypdf"), ("docx", "docx")],
)
def test_a_missing_library_raises_and_names_the_package_to_install(
    kind: DocumentKind, module_name: str
) -> None:
    """The message has to be actionable: a developer reading it should not have to
    know that ``docx`` on the import line is ``python-docx`` on the install line."""
    _skip_if_installed(module_name)

    with pytest.raises(ExtractorUnavailableError) as caught:
        extract_text(PDF_BYTES, kind=kind, filename=f"leistungen.{kind}")

    error = caught.value
    assert error.kind == kind
    assert error.package == MISSING_PACKAGES[kind]
    assert MISSING_PACKAGES[kind] in str(error)
    # And it must not be mistakeable for the scanned-document case.
    assert "OCR" not in str(error)


def test_the_missing_library_is_an_error_not_an_empty_success() -> None:
    """``no_text`` sends the user to OCR. A document marked that way because a
    dependency was absent would have indexed perfectly on another machine."""
    _skip_if_installed("pypdf")

    with pytest.raises(ExtractorUnavailableError):
        extract_pdf(PDF_BYTES, "preise.pdf")


# --------------------------------------------------------------------------- #
# Stub libraries, shaped like the real public surface
# --------------------------------------------------------------------------- #


class _StubPage:
    def __init__(self, text: str | None) -> None:
        self._text = text

    def extract_text(self) -> str | None:
        return self._text


class _StubReader:
    """Stands in for ``pypdf.PdfReader``.

    ``raises`` covers a corrupt file; ``encrypted`` covers the one parse failure
    whose remedy is different, and it is exposed the way pypdf exposes it, as
    ``is_encrypted`` on the reader rather than as an exception type.
    """

    def __init__(
        self,
        pages: list[str | None],
        *,
        raises: Exception | None = None,
        encrypted: bool = False,
    ) -> None:
        self._pages = pages
        self._raises = raises
        self.is_encrypted = encrypted

    @property
    def pages(self) -> list[_StubPage]:
        if self._raises is not None:
            raise self._raises
        return [_StubPage(text) for text in self._pages]


def _install_pypdf(monkeypatch: pytest.MonkeyPatch, reader: _StubReader) -> list[bytes]:
    """Put a stub ``pypdf`` in ``sys.modules`` and record what it was handed.

    Recording the bytes is not decoration: it asserts the extractor passes the whole
    upload to the library rather than, say, a decoded string.
    """
    seen: list[bytes] = []

    def _reader(stream: Any) -> _StubReader:
        seen.append(stream.read())
        return reader

    module = ModuleType("pypdf")
    module.PdfReader = _reader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", module)
    return seen


def _cell(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


def _row(*cells: str) -> SimpleNamespace:
    return SimpleNamespace(cells=[_cell(text) for text in cells])


def _install_docx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    paragraphs: list[str],
    tables: list[list[SimpleNamespace]] | None = None,
    raises: Exception | None = None,
) -> None:
    def _document(stream: Any) -> SimpleNamespace:
        stream.read()
        if raises is not None:
            raise raises
        return SimpleNamespace(
            paragraphs=[_cell(text) for text in paragraphs],
            tables=[SimpleNamespace(rows=rows) for rows in (tables or [])],
        )

    module = ModuleType("docx")
    module.Document = _document  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "docx", module)


# --------------------------------------------------------------------------- #
# PDF parsing
# --------------------------------------------------------------------------- #


def test_pdf_pages_are_joined_with_a_blank_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank line is the one structural fact a PDF reliably carries: it keeps the
    last sentence of one page from fusing into the first heading of the next."""
    seen = _install_pypdf(
        monkeypatch, _StubReader(["Notdienst rund um die Uhr", "Preise ab 89 EUR"])
    )

    result = extract_pdf(PDF_BYTES, "preise.pdf")

    assert result.status == "ok"
    assert result.text == "Notdienst rund um die Uhr\n\nPreise ab 89 EUR"
    assert seen == [PDF_BYTES], "the extractor must hand the library the raw upload"
    # Binary formats report no codec: decoding is the parser's business, and naming
    # one here would be a guess presented as a fact.
    assert result.encoding is None


def test_a_scanned_pdf_is_no_text_with_an_ocr_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """The named case from Phase 3: a scan parses perfectly and yields nothing.

    Reported as ``no_text`` -- not as an error, and NOT as an empty ``ok``, which
    would write zero chunks, mark the document indexed, and tell the customer their
    price list is searchable when not one word of it is.
    """
    _install_pypdf(monkeypatch, _StubReader([None, "", "   \n  "]))

    result = extract_pdf(PDF_BYTES, "gescannte-preisliste.pdf")

    assert result.status == "no_text"
    assert result.text == ""
    assert result.note is not None
    assert "OCR" in result.note
    assert "gescannte-preisliste.pdf" in result.note
    assert result.has_text is False


def test_a_pdf_with_no_pages_at_all_is_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_pypdf(monkeypatch, _StubReader([]))

    assert extract_pdf(PDF_BYTES).status == "no_text"


def test_a_corrupt_pdf_is_failed_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ingest that dies on one bad upload is an ingest a customer cannot use."""
    _install_pypdf(monkeypatch, _StubReader([], raises=ValueError("EOF marker not found")))

    result = extract_pdf(PDF_BYTES, "truncated.pdf")

    assert result.status == "failed"
    assert result.note is not None
    assert "EOF marker not found" in result.note
    # The remedy is a different export, so it must not mention OCR.
    assert "OCR will not help" in result.note


def test_an_encrypted_pdf_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one parse failure with a different remedy gets its own sentence: nobody
    can re-export their way out of a password."""
    _install_pypdf(
        monkeypatch,
        _StubReader([], raises=ValueError("file has not been decrypted"), encrypted=True),
    )

    result = extract_pdf(PDF_BYTES, "vertrag.pdf")

    assert result.status == "failed"
    assert result.note is not None
    assert "password-protected" in result.note


def test_pdf_text_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control characters and CRLF come out of real PDFs constantly."""
    _install_pypdf(monkeypatch, _StubReader(["Preise\r\n\r\n\r\n\r\nab \x0c89 EUR   "]))

    assert extract_pdf(PDF_BYTES).text == "Preise\n\nab 89 EUR"


def test_the_registry_routes_pdf_to_the_pdf_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """``extract_text`` is what the service calls, so the registry entry is part of
    the contract, not an implementation detail."""
    _install_pypdf(monkeypatch, _StubReader(["Notdienst"]))

    result = extract_text(PDF_BYTES, kind="pdf", filename="a.pdf")

    assert (result.kind, result.status, result.text) == ("pdf", "ok", "Notdienst")


# --------------------------------------------------------------------------- #
# DOCX parsing
# --------------------------------------------------------------------------- #


def test_docx_paragraphs_are_extracted(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_docx(monkeypatch, paragraphs=["Unsere Leistungen", "", "Notdienst in Koblenz"])

    result = extract_docx(DOCX_BYTES, "leistungen.docx")

    assert result.status == "ok"
    assert result.text == "Unsere Leistungen\n\nNotdienst in Koblenz"


def test_docx_tables_are_extracted_because_a_price_list_is_a_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing case. ``document.paragraphs`` excludes everything inside a
    table, so a paragraphs-only implementation would silently drop the most
    valuable page of a price list and still report ``ok``.

    Cells are joined so the pairing survives: "Notdienst | 89,00 EUR" answers a
    question that "Notdienst" and "89,00 EUR" on separate lines do not.
    """
    _install_docx(
        monkeypatch,
        paragraphs=["Preisliste"],
        tables=[[_row("Leistung", "Preis"), _row("Notdienst", "89,00 EUR")]],
    )

    result = extract_docx(DOCX_BYTES, "preise.docx")

    assert result.text == "Preisliste\n\nLeistung | Preis\nNotdienst | 89,00 EUR"


def test_a_merged_docx_cell_is_not_indexed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """python-docx reports a horizontally merged cell once per column it spans.
    Indexing the repeat would double that phrase's weight in retrieval for nothing."""
    _install_docx(
        monkeypatch,
        paragraphs=[],
        tables=[[_row("Preise 2026", "Preise 2026"), _row("Notdienst", "89,00 EUR")]],
    )

    assert extract_docx(DOCX_BYTES).text == "Preise 2026\nNotdienst | 89,00 EUR"


def test_an_empty_docx_is_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_docx(monkeypatch, paragraphs=["", "   "], tables=[])

    result = extract_docx(DOCX_BYTES, "leer.docx")

    assert result.status == "no_text"
    assert result.note is not None
    assert "OCR" in result.note


def test_a_corrupt_docx_is_failed_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_docx(
        monkeypatch,
        paragraphs=[],
        raises=KeyError("There is no item named 'word/document.xml' in the archive"),
    )

    result = extract_docx(DOCX_BYTES, "kaputt.docx")

    assert result.status == "failed"
    assert result.note is not None
    assert "KeyError" in result.note


def test_the_registry_routes_docx_to_the_docx_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_docx(monkeypatch, paragraphs=["Notdienst"])

    result = extract_text(DOCX_BYTES, kind="docx", filename="a.docx")

    assert (result.kind, result.status, result.text) == ("docx", "ok", "Notdienst")
