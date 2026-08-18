"""Knowledge-base engine: uploaded bytes -> text -> chunks. Pure computation.

This is the deterministic half of RAG. Everything that needs a database session
or a model -- embedding, storing, searching, grading, deciding whether to retrieve
at all -- lives in ``backend.app.services.kb_service``, because an engine may not
import an LLM client or a DB session (docs/ARCHITECTURE.md section 3, enforced by
tests/test_engine_boundary.py). That split is why ``kb`` is two modules rather
than one: retrieval genuinely needs both, extraction genuinely needs neither.

    result = extract_text(data, kind="html", filename="preise.html")
    if result.status == "no_text":
        ...  # offer OCR; do not index an empty document
    chunks = chunk_text(result.text)          # 512 tokens, 64 overlapping
    digest = content_hash(chunks[0].text)     # the dedup key for embeddings

Formats: ``md``, ``txt`` and ``html`` are implemented. ``pdf`` and ``docx`` are
registered and raise :class:`ExtractorUnavailableError` naming the package they
need (``pypdf``, ``python-docx``), which are phase-gated in pyproject.toml. That
is an honest gap: a stub returning empty text would be indistinguishable from a
scanned document, and would get a customer's price list marked "indexed".
"""

from .chunk import chunk_text, content_hash, estimate_tokens
from .contract import (
    CHARS_PER_TOKEN,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    Chunk,
    DocumentKind,
    ExtractionResult,
    ExtractionStatus,
    ExtractorUnavailableError,
    KbEngineError,
    UnsupportedDocumentKindError,
    tokens_for_chars,
)
from .extract import (
    EXTRACTORS,
    MISSING_PACKAGES,
    NO_TEXT_NOTE,
    decode_bytes,
    extract_docx,
    extract_html,
    extract_markdown,
    extract_pdf,
    extract_plain_text,
    extract_text,
    finalise_extraction,
    normalise_text,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_TARGET_TOKENS",
    "EXTRACTORS",
    "MISSING_PACKAGES",
    "NO_TEXT_NOTE",
    "Chunk",
    "DocumentKind",
    "ExtractionResult",
    "ExtractionStatus",
    "ExtractorUnavailableError",
    "KbEngineError",
    "UnsupportedDocumentKindError",
    "chunk_text",
    "content_hash",
    "decode_bytes",
    "estimate_tokens",
    "extract_docx",
    "extract_html",
    "extract_markdown",
    "extract_pdf",
    "extract_plain_text",
    "extract_text",
    "finalise_extraction",
    "normalise_text",
    "tokens_for_chars",
]
