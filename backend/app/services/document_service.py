"""Upload a document, index it, and say honestly what happened to it.

The gap this closes. `kb_service.ingest_document` extracts, chunks, embeds and stores
a file, and it was called from twenty tests and nowhere else: no route, no UI, no
`documents` row. So the whole knowledge base -- pgvector, the agentic retrieval loop,
the extractors, the dedup-by-hash -- was reachable only by writing Python, and
`docs/FEATURES.md` §7 lists "ingest documents" as step 1 of the customer journey.

This module is deliberately thin, because the interesting decisions are already made
elsewhere and copying them here is how they drift:

* WHAT a file's bytes mean is `engines/kb.extract`'s question, and it already
  distinguishes "no extractable text" (offer OCR) from "could not be parsed at all"
  (ask for a different export) from "the library is not installed" (an operator
  problem, and NOT the customer's file being broken);
* whether a chunk needs embedding again is `kb_service`'s, answered on a content
  hash so a re-upload of the same price list costs nothing;
* whose document this is, is row-level security's.

What this owns is the ORDER: register the row first, ingest second, record the verdict
third. A row written only on success is a file that vanishes when it fails, leaving a
customer who watched an upload complete with nothing on screen and nothing to report.

**The bytes are not kept.** `documents.storage_key` stays null: object storage is
behind a compose profile that nothing has needed yet, and the retrievable artifact is
the chunks, not the file. The honest consequence, stated because it will surprise
somebody: re-indexing with a better chunker later means asking the customer to upload
again. That is a worse answer than keeping the bytes and a much better one than
pretending we kept them.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.app.engines.kb import DocumentKind, ExtractorUnavailableError
from backend.app.services.kb_service import ChunkStore, Embedder, IngestReport, ingest_document

__all__ = [
    "DocumentKindError",
    "DocumentStore",
    "ExtractorMissingError",
    "IngestFailedError",
    "IngestOutcome",
    "kind_for_filename",
    "store_document",
]

#: Filename suffix -> the extractor to use. The engine's `DocumentKind` is the
#: authority on what can be read; this maps what a browser sends onto it.
#:
#: An allowlist, not a blocklist, and checked before a single byte is extracted: the
#: extractors are third-party parsers for hostile-by-default input (a PDF is a
#: programming language), so a format nobody asked for must not reach one.
_SUFFIXES: dict[str, DocumentKind] = {
    "pdf": "pdf",
    "docx": "docx",
    "md": "md",
    "markdown": "md",
    "txt": "txt",
    "text": "txt",
    "html": "html",
    "htm": "html",
}


class DocumentKindError(ValueError):
    """The file is not a format any extractor can read.

    Carries the accepted list, because "unsupported file type" alone leaves the
    customer guessing which of their exports to try next.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.accepted = tuple(sorted(set(_SUFFIXES)))
        super().__init__(
            f"{filename!r} is not a format we can read. Accepted: "
            + ", ".join(f".{suffix}" for suffix in self.accepted)
        )


class ExtractorMissingError(RuntimeError):
    """The format is supported and the library that reads it is not installed.

    A distinct error from :class:`DocumentKindError` all the way to the HTTP status,
    because they are different people's problems: one is the customer's file, the
    other is our deployment. Telling a customer their PDF is unreadable because we
    forgot to install `pypdf` is a lie with a support ticket attached.
    """

    def __init__(self, kind: str, package: str) -> None:
        self.kind = kind
        self.package = package
        super().__init__(
            f"{kind} files need the {package!r} package, which is not installed on this deployment."
        )


class IngestFailedError(RuntimeError):
    """Indexing broke for a reason that is neither the file nor a missing parser.

    In practice this is an embeddings provider that did not answer, which is the same
    kind of failure `onboarding.py` reports as a 502 for an unreachable website. It is
    its own type so the route does not have to guess whether a bare ``Exception``
    means "their file" or "upstream" — and the document row is already marked `failed`
    with a note by the time it is raised, so the customer sees a file that did not
    index rather than one that disappeared.
    """


def kind_for_filename(filename: str) -> DocumentKind:
    """The extractor for this filename, or raise :class:`DocumentKindError`.

    Decided on the SUFFIX, not on the browser's `Content-Type`. The content type is
    caller-controlled and routinely wrong (`application/octet-stream` for everything
    from some clients), so trusting it would either refuse valid files or send a
    `.exe` to the PDF parser.
    """
    _, _, suffix = filename.rpartition(".")
    kind = _SUFFIXES.get(suffix.strip().lower())
    if kind is None:
        raise DocumentKindError(filename)
    return kind


class DocumentStore(Protocol):
    """The document register. A protocol so the service tests need no database."""

    async def create(self, *, filename: str, kind: str) -> UUID: ...

    async def finish(
        self,
        document_id: UUID,
        *,
        status: str,
        chunk_count: int,
        extraction_note: str | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """The document id, and the report of what indexing it achieved."""

    document_id: UUID
    report: IngestReport


async def store_document(
    data: bytes,
    *,
    filename: str,
    business_id: UUID,
    documents: DocumentStore,
    embedder: Embedder,
    chunks: ChunkStore,
) -> IngestOutcome:
    """Register, index, and record the verdict. One document, one call.

    Raises :class:`DocumentKindError` before anything is written, and either
    :class:`ExtractorMissingError` or :class:`IngestFailedError` after the row exists
    -- the row is then marked `failed` with a note naming the cause, so an operator
    can see it in the product rather than only in a log, and the customer sees a
    document that did not index rather than one that silently disappeared.

    An ingest that raises for any other reason also marks the row `failed`, and is
    re-raised as :class:`IngestFailedError` so the route can answer 502 rather than
    guessing. A `pending` row nothing will ever come back to is the one state this
    function must not leave behind: it renders as "still indexing" forever.
    """
    kind = kind_for_filename(filename)
    document_id = await documents.create(filename=filename, kind=kind)

    try:
        report = await ingest_document(
            data,
            business_id=business_id,
            document_id=document_id,
            kind=kind,
            embedder=embedder,
            store=chunks,
            filename=filename,
        )
    except ExtractorUnavailableError as exc:
        await documents.finish(
            document_id,
            status="failed",
            chunk_count=0,
            extraction_note=(
                f"This deployment cannot read {exc.kind} files: the {exc.package!r} "
                "package is not installed. Nothing is wrong with your file."
            ),
        )
        raise ExtractorMissingError(exc.kind, exc.package) from exc
    except Exception as exc:
        await documents.finish(
            document_id,
            status="failed",
            chunk_count=0,
            extraction_note="Indexing could not be completed. Please try uploading again.",
        )
        raise IngestFailedError(str(exc)) from exc

    await documents.finish(
        document_id,
        status=report.status,
        chunk_count=report.chunks_stored,
        extraction_note=report.note,
    )
    return IngestOutcome(document_id=document_id, report=report)
