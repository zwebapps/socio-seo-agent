"""Tests for the kb engine: bytes -> text -> chunks, and nothing else.

The engine is pure, so these tests need no fixtures, no event loop and no
network. What they are really defending is two failure modes that both *look*
like success:

* **Silently indexing an empty document.** A scanned PDF contains no extractable
  text. If extraction returns `""` with an `ok` status, ingest happily writes
  zero chunks, marks the document indexed, and the customer is told their price
  list is searchable when it is not. So ``no_text`` is a first-class status with
  a note, tested separately from ``failed``.
* **A chunk larger than the target.** An oversized chunk is silently truncated by
  the embedding model, so the tail of the text is unretrievable while the row
  still looks present. Every chunking test therefore asserts the ceiling.

The third thing tested here is honesty about what is *not* implemented: `pdf`
and `docx` raise :class:`ExtractorUnavailableError` naming the package they need,
rather than returning empty text that would be indistinguishable from a scan.
"""

import hashlib
from itertools import pairwise

import pytest

from backend.app.engines.kb import (
    CHARS_PER_TOKEN,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    Chunk,
    ExtractionResult,
    ExtractorUnavailableError,
    UnsupportedDocumentKindError,
    chunk_text,
    content_hash,
    estimate_tokens,
    extract_text,
    finalise_extraction,
)

# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


class TestPlainText:
    def test_utf8_text_is_extracted(self) -> None:
        result = extract_text("Öffnungszeiten: Mo-Fr 8-18 Uhr.".encode(), kind="txt")

        assert result.status == "ok"
        assert result.text == "Öffnungszeiten: Mo-Fr 8-18 Uhr."
        assert result.kind == "txt"
        assert result.encoding == "utf-8"
        assert result.note is None

    def test_bom_is_not_part_of_the_text(self) -> None:
        """A UTF-8 BOM left in the text corrupts the first chunk's hash."""
        result = extract_text(b"\xef\xbb\xbfPreise 2026", kind="txt")

        assert result.text == "Preise 2026"
        assert result.encoding == "utf-8-sig"

    def test_windows_line_endings_are_normalised(self) -> None:
        result = extract_text(b"line one\r\nline two\r\n", kind="txt")

        assert result.text == "line one\nline two"

    def test_runs_of_blank_lines_collapse_to_one(self) -> None:
        result = extract_text(b"para one\n\n\n\n\npara two", kind="txt")

        assert result.text == "para one\n\npara two"

    def test_nul_bytes_are_stripped_rather_than_stored(self) -> None:
        result = extract_text(b"price\x00list", kind="txt")

        assert result.status == "ok"
        assert "\x00" not in result.text

    def test_empty_file_is_no_text_not_ok(self) -> None:
        result = extract_text(b"", kind="txt")

        assert result.status == "no_text"
        assert result.text == ""
        assert result.note is not None

    def test_whitespace_only_file_is_no_text(self) -> None:
        result = extract_text(b"   \n\n\t\n  ", kind="txt")

        assert result.status == "no_text"

    def test_undecodable_bytes_are_failed_not_no_text(self) -> None:
        """`failed` and `no_text` mean different things to the UI.

        `no_text` offers OCR. `failed` offers "re-export this file". Collapsing
        them would send someone to OCR a corrupt download.
        """
        result = extract_text(b"\x81\x8d\x8f\x90\x9d", kind="txt")

        assert result.status == "failed"
        assert result.note is not None
        assert "decode" in result.note.lower()


class TestMarkdown:
    def test_markdown_is_kept_verbatim(self) -> None:
        """Headings and list markers are retrieval signal, not noise."""
        source = "# Leistungen\n\n- Notdienst 24/7\n- Wartung\n"
        result = extract_text(source.encode(), kind="md")

        assert result.status == "ok"
        assert result.text == "# Leistungen\n\n- Notdienst 24/7\n- Wartung"

    def test_indented_code_block_keeps_its_indentation(self) -> None:
        result = extract_text(b"Beispiel:\n\n    curl https://x\n", kind="md")

        assert "    curl https://x" in result.text


class TestHtml:
    def test_visible_text_is_extracted(self) -> None:
        html = b"<html><body><h1>Preise</h1><p>Ab 49 EUR</p></body></html>"
        result = extract_text(html, kind="html")

        assert result.status == "ok"
        assert "Preise" in result.text
        assert "Ab 49 EUR" in result.text

    def test_script_and_style_never_reach_the_index(self) -> None:
        """Indexed JavaScript is retrievable nonsense that dilutes every search."""
        html = (
            b"<html><head><style>.a{color:red}</style>"
            b"<script>var secret = 1;</script></head>"
            b"<body><p>Kontakt</p><noscript>enable js</noscript></body></html>"
        )
        result = extract_text(html, kind="html")

        assert "Kontakt" in result.text
        assert "secret" not in result.text
        assert "color:red" not in result.text
        assert "enable js" not in result.text

    def test_markup_only_document_is_no_text(self) -> None:
        html = b"<html><head><script>var x = 1;</script></head><body></body></html>"
        result = extract_text(html, kind="html")

        assert result.status == "no_text"

    def test_block_elements_do_not_run_words_together(self) -> None:
        html = b"<ul><li>Notdienst</li><li>Wartung</li></ul>"
        result = extract_text(html, kind="html")

        assert "NotdienstWartung" not in result.text
        assert "Notdienst" in result.text
        assert "Wartung" in result.text

    def test_broken_markup_does_not_raise(self) -> None:
        result = extract_text(b"<p>unclosed <b>bold", kind="html")

        assert result.status == "ok"
        assert "unclosed" in result.text


class TestUnavailableExtractors:
    """The registry has an honest gap: pypdf and python-docx are not installed."""

    @pytest.mark.parametrize(
        ("kind", "package"),
        [("pdf", "pypdf"), ("docx", "python-docx")],
    )
    def test_missing_extractor_raises_and_names_the_package(self, kind: str, package: str) -> None:
        with pytest.raises(ExtractorUnavailableError) as excinfo:
            extract_text(b"%PDF-1.7 whatever", kind=kind)  # type: ignore[arg-type]

        assert package in str(excinfo.value)
        assert excinfo.value.kind == kind
        assert excinfo.value.package == package

    def test_unknown_kind_is_a_different_error(self) -> None:
        with pytest.raises(UnsupportedDocumentKindError):
            extract_text(b"x", kind="xlsx")  # type: ignore[arg-type]


class TestScannedDocumentShape:
    """What a scanned PDF will produce once pypdf is installed.

    The pdf extractor cannot be exercised end to end without the dependency, so
    the *shared finaliser* every extractor must return through is tested with
    exactly the input a scan produces: a successful parse that yielded no text.
    """

    def test_empty_extraction_from_a_pdf_is_no_text_with_an_ocr_hint(self) -> None:
        result = finalise_extraction("", kind="pdf", filename="scan.pdf")

        assert result.status == "no_text"
        assert result.text == ""
        assert result.note is not None
        assert "OCR" in result.note
        assert "scan.pdf" in result.note

    def test_page_break_only_extraction_is_still_no_text(self) -> None:
        result = finalise_extraction("\n\n\f\n", kind="pdf")

        assert result.status == "no_text"

    def test_a_result_carrying_text_is_ok(self) -> None:
        result = finalise_extraction("Rechnung 2026", kind="pdf")

        assert result.status == "ok"
        assert result.has_text is True
        assert result.char_count == len("Rechnung 2026")


class TestExtractionResultIsFrozen:
    def test_result_cannot_be_mutated_after_return(self) -> None:
        result = extract_text(b"hello", kind="txt")

        assert isinstance(result, ExtractionResult)
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            result.text = "tampered"


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #


class TestContentHash:
    def test_is_sha256_hex_of_utf8_bytes(self) -> None:
        assert content_hash("Preise") == hashlib.sha256(b"Preise").hexdigest()

    def test_is_deterministic_across_calls(self) -> None:
        assert content_hash("same text") == content_hash("same text")

    def test_differs_for_different_text(self) -> None:
        assert content_hash("a") != content_hash("b")

    def test_is_length_64_hex(self) -> None:
        digest = content_hash("Öffnungszeiten")

        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_non_ascii_is_stable(self) -> None:
        """Two processes must agree, so the encoding is pinned, not the platform's."""
        assert content_hash("Müller") == hashlib.sha256("Müller".encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Token estimation
# --------------------------------------------------------------------------- #


class TestEstimateTokens:
    """The estimate is an approximation, but it must be the *same* approximation.

    Chunking uses it to decide where to cut and the tests use it to assert the
    ceiling. If the two ever diverged, "no chunk exceeds the target" would be
    asserted against arithmetic nobody performs.
    """

    def test_empty_text_is_zero_tokens(self) -> None:
        assert estimate_tokens("") == 0

    def test_is_chars_over_the_documented_ratio_rounded_up(self) -> None:
        assert estimate_tokens("a" * CHARS_PER_TOKEN) == 1
        assert estimate_tokens("a" * (CHARS_PER_TOKEN + 1)) == 2

    def test_is_monotonic_in_length(self) -> None:
        short = estimate_tokens("a" * 100)
        long = estimate_tokens("a" * 400)

        assert long > short


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def shared_boundary(before: str, after: str) -> str:
    """The longest string that ends `before` and begins `after`.

    This is how the tests measure overlap without the implementation telling
    them what it did: real carried context is literally a suffix of the previous
    chunk and a prefix of the next one.
    """
    limit = min(len(before), len(after))
    for size in range(limit, 0, -1):
        if before[-size:] == after[:size]:
            return after[:size]
    return ""


def prose(sentences: int) -> str:
    """Realistic German business prose, every sentence distinct.

    Distinctness is load-bearing, not decoration: :func:`shared_boundary` finds
    the longest suffix of one chunk that is a prefix of the next, and against a
    document of identical repeated sentences it would report a whole-chunk
    "overlap" that the chunker never created. Repeated text would make the
    overlap assertions pass for the wrong reason.
    """
    return " ".join(
        f"Fall {index}: Der Notdienst ist rund um die Uhr erreichbar und wir "
        f"kommen in etwa {index} Minuten zu Ihnen nach Hause oder in den Betrieb."
        for index in range(1, sentences + 1)
    )


class TestChunkBoundaries:
    def test_empty_text_yields_no_chunks(self) -> None:
        assert chunk_text("") == []

    def test_whitespace_only_text_yields_no_chunks(self) -> None:
        assert chunk_text("   \n\n\t ") == []

    def test_text_shorter_than_one_chunk_is_a_single_chunk(self) -> None:
        chunks = chunk_text("Wir haben Montag bis Freitag von 8 bis 18 Uhr geoeffnet.")

        assert len(chunks) == 1
        assert chunks[0].ordinal == 0
        assert chunks[0].carried_tokens == 0
        assert chunks[0].text == "Wir haben Montag bis Freitag von 8 bis 18 Uhr geoeffnet."

    def test_single_chunk_carries_its_own_hash_and_estimate(self) -> None:
        text = "Preisliste 2026: Wartung ab 89 EUR."
        chunk = chunk_text(text)[0]

        assert chunk.content_hash == content_hash(text)
        assert chunk.token_estimate == estimate_tokens(text)

    def test_ordinals_are_dense_and_start_at_zero(self) -> None:
        chunks = chunk_text(prose(40), target_tokens=64, overlap_tokens=8)

        assert len(chunks) > 3
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    def test_no_chunk_exceeds_the_target(self) -> None:
        chunks = chunk_text(prose(40), target_tokens=64, overlap_tokens=8)

        assert chunks
        for chunk in chunks:
            assert chunk.token_estimate <= 64, chunk.text
            assert estimate_tokens(chunk.text) <= 64

    def test_no_chunk_exceeds_the_target_at_the_defaults(self) -> None:
        chunks = chunk_text(
            prose(200),
            target_tokens=DEFAULT_TARGET_TOKENS,
            overlap_tokens=DEFAULT_OVERLAP_TOKENS,
        )

        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.token_estimate <= DEFAULT_TARGET_TOKENS

    def test_every_chunk_carries_text(self) -> None:
        chunks = chunk_text(prose(40), target_tokens=32, overlap_tokens=4)

        for chunk in chunks:
            assert chunk.text.strip() == chunk.text
            assert chunk.text != ""

    def test_exact_multiple_of_the_target_splits_evenly_and_losslessly(self) -> None:
        """A hard split has no word boundaries to lose, so reassembly is exact."""
        text = "a" * (10 * CHARS_PER_TOKEN * 3)

        chunks = chunk_text(text, target_tokens=10, overlap_tokens=0)

        assert len(chunks) == 3
        assert [chunk.token_estimate for chunk in chunks] == [10, 10, 10]
        assert "".join(chunk.text for chunk in chunks) == text

    def test_a_word_longer_than_the_target_is_split_rather_than_dropped(self) -> None:
        """The alternative is a chunk over the ceiling, or lost text."""
        long_word = "x" * (40 * CHARS_PER_TOKEN)

        chunks = chunk_text(long_word, target_tokens=10, overlap_tokens=0)

        assert len(chunks) == 4
        assert "".join(chunk.text for chunk in chunks) == long_word


class TestChunkOverlap:
    def test_consecutive_chunks_share_a_real_boundary(self) -> None:
        chunks = chunk_text(prose(30), target_tokens=64, overlap_tokens=16)

        assert len(chunks) > 2
        for previous, current in pairwise(chunks):
            carried = shared_boundary(previous.text, current.text)
            assert carried.strip() != "", (
                "a chunk boundary with no overlap can split a fact in half, so the "
                "sentence is retrievable from neither side"
            )

    def test_carried_context_never_exceeds_the_requested_overlap(self) -> None:
        chunks = chunk_text(prose(30), target_tokens=64, overlap_tokens=16)

        for previous, current in pairwise(chunks):
            carried = shared_boundary(previous.text, current.text)
            assert estimate_tokens(carried) <= 16

    def test_reported_carried_tokens_match_the_measured_boundary(self) -> None:
        chunks = chunk_text(prose(30), target_tokens=64, overlap_tokens=16)

        for previous, current in pairwise(chunks):
            carried = shared_boundary(previous.text, current.text)
            assert current.carried_tokens == estimate_tokens(carried)

    def test_zero_overlap_means_no_shared_text(self) -> None:
        chunks = chunk_text(prose(30), target_tokens=64, overlap_tokens=0)

        assert len(chunks) > 2
        for previous, current in pairwise(chunks):
            assert shared_boundary(previous.text, current.text).strip() == ""
            assert current.carried_tokens == 0

    def test_overlap_does_not_prevent_progress(self) -> None:
        """The loop must consume input every turn, or it never terminates."""
        chunks = chunk_text(prose(10), target_tokens=16, overlap_tokens=15)

        assert len(chunks) < 400
        for chunk in chunks:
            assert chunk.token_estimate <= 16


class TestChunkArguments:
    @pytest.mark.parametrize(
        ("target", "overlap"),
        [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)],
    )
    def test_impossible_arguments_raise_rather_than_loop(self, target: int, overlap: int) -> None:
        with pytest.raises(ValueError):
            chunk_text(prose(4), target_tokens=target, overlap_tokens=overlap)

    def test_chunks_are_frozen(self) -> None:
        chunk = chunk_text("Preise auf Anfrage.")[0]

        assert isinstance(chunk, Chunk)
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            chunk.text = "tampered"


class TestChunkHashesSupportDeduplication:
    def test_identical_paragraphs_produce_identical_hashes(self) -> None:
        """This is what makes `existing_hashes` able to skip an embedding call."""
        first = chunk_text("Wartung ab 89 EUR.")
        second = chunk_text("Wartung ab 89 EUR.")

        assert first[0].content_hash == second[0].content_hash

    def test_hash_is_of_the_stored_text_not_the_source(self) -> None:
        chunks = chunk_text(prose(30), target_tokens=64, overlap_tokens=16)

        for chunk in chunks:
            assert chunk.content_hash == content_hash(chunk.text)
