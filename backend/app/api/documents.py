"""Document routes: the upload path the knowledge base never had.

Every piece of the knowledge base shipped and none of it was reachable.
`kb_service.ingest_document` had twenty tests and no caller, so no business could
ever have a chunk in it -- which made the agentic retrieval loop, the pgvector store
and the pdf/docx extractors dead weight in the running product, and made
`docs/FEATURES.md` §7's step 1 ("crawl site, ingest documents") half true.

Three rules shape this module, the same three as `onboarding.py`:

* **every failure says whose problem it is.** An unreadable format is the caller's
  (415), a file too large is the caller's (413, from the body-size middleware), a
  parser that is not installed is OURS (503) -- and none of them is a 500;
* **the tenant comes from the session, never from the body.** Accepting a business id
  here would be letting the client make an authorisation decision;
* **the response says what indexing actually achieved**, not that it succeeded. A
  scanned PDF yields no text; "uploaded" would be true and useless, so the report's
  chunk counts and note travel to the screen intact.

The format allowlist is checked before any byte reaches an extractor. A PDF parser is
a third-party reader of hostile-by-default input, and the cheapest defence is not
handing it a file nobody asked us to read.
"""

from collections.abc import Sequence
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from backend.app.api.runs import current_business
from backend.app.db.adapters.chunk_store import PgVectorChunkStore
from backend.app.db.adapters.document_store import DocumentRecord, PostgresDocumentStore
from backend.app.llm.embedder import RouterEmbedder
from backend.app.services.document_service import (
    DocumentKindError,
    ExtractorMissingError,
    IngestFailedError,
    store_document,
)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

#: How many documents the list returns. A business with more than this has a
#: different problem than pagination.
LIST_LIMIT = 100


# --------------------------------------------------------------------------- #
# Dependencies -- functions, so tests can override them
# --------------------------------------------------------------------------- #


def get_documents(
    business_id: Annotated[UUID, Depends(current_business)],
) -> PostgresDocumentStore:
    """The document register for this caller's business."""
    return PostgresDocumentStore(business_id)


def get_chunks() -> Any:
    """The pgvector chunk store. Stateless, so one instance is fine."""
    return PgVectorChunkStore()


def get_embedder() -> Any:
    """The embedder.

    Built per request rather than cached, because it resolves the routing table at
    construction: an operator changing the EMBED route in `/developer/models` should
    take effect on the next upload, not on the next restart.
    """
    return RouterEmbedder()


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DocumentOut(CamelModel):
    """One document, as the screen shows it.

    ``status`` and ``chunkCount`` are both here because either alone can mislead:
    `indexed` with zero chunks is a scan, and 12 chunks on a `failed` row cannot
    happen but would be worth seeing if it did.
    """

    id: UUID
    filename: str
    kind: str
    status: str
    chunk_count: int
    #: Why a document is not searchable, in words a customer can act on.
    extraction_note: str | None
    created_at: str


class DocumentListResponse(CamelModel):
    documents: list[DocumentOut]
    #: Chunks across every document. What the agent can actually retrieve.
    total_chunks: int


class UploadResponse(CamelModel):
    """What indexing achieved, echoed back rather than assumed.

    ``chunksDuplicate`` is not noise: a customer who re-uploads last month's price
    list should be told nothing changed, and the number is the proof that the dedup
    is real rather than described.
    """

    document: DocumentOut
    status: str
    chunks_stored: int
    chunks_duplicate: int
    chars_extracted: int
    note: str | None


def _out(record: DocumentRecord) -> DocumentOut:
    return DocumentOut(
        id=record.id,
        filename=record.filename,
        kind=record.kind,
        status=record.status,
        chunk_count=record.chunk_count,
        extraction_note=record.extraction_note,
        created_at=record.created_at.isoformat(),
    )


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=DocumentListResponse,
    response_model_by_alias=True,
    summary="Documents this business has uploaded, newest first",
)
async def list_documents(
    documents: Annotated[PostgresDocumentStore, Depends(get_documents)],
    response: Response,
) -> DocumentListResponse:
    """`no-store`, because filenames are customer data and this sits behind a session
    cookie. Same rule as the leads list and the runs list."""
    response.headers["Cache-Control"] = "no-store"
    records: Sequence[DocumentRecord] = await documents.list(limit=LIST_LIMIT)
    return DocumentListResponse(
        documents=[_out(record) for record in records],
        total_chunks=await documents.chunk_count(),
    )


@router.post(
    "",
    response_model=UploadResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Upload one document and index it into the knowledge base",
)
async def upload_document(
    business_id: Annotated[UUID, Depends(current_business)],
    documents: Annotated[PostgresDocumentStore, Depends(get_documents)],
    embedder: Annotated[Any, Depends(get_embedder)],
    chunks: Annotated[Any, Depends(get_chunks)],
    file: Annotated[UploadFile, File(description="A pdf, docx, md, txt or html file")],
) -> UploadResponse:
    """Index one file. Synchronous, and that is a deliberate trade for now.

    Embedding a document is one batched provider call, so a price list finishes in
    seconds -- and a synchronous answer means the customer learns immediately that
    their scanned PDF has no text in it, rather than watching a row say "pending"
    while a queue nobody has deployed fails to pick it up. `ROADMAP` names ARQ/Redis
    for the background path; until a worker actually runs (see the note in
    `run_executor`), a job queue here would be a slower way to get no answer.

    The body-size ceiling is enforced by `BodySizeLimitMiddleware` before a byte is
    read, and it is raised for this prefix only -- see `main.create_app`.
    """
    filename = (file.filename or "").strip() or "upload"
    data = await file.read()

    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_error("empty_file", "That file is empty."),
        )

    try:
        outcome = await store_document(
            data,
            filename=filename,
            business_id=business_id,
            documents=documents,
            embedder=embedder,
            chunks=chunks,
        )
    except DocumentKindError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=_error(
                "unsupported_kind",
                "We can read PDF, Word (.docx), Markdown, plain text and HTML. "
                f"{filename!r} is none of those — please export it and try again.",
            ),
        ) from exc
    except IngestFailedError as exc:
        # The document row is already marked `failed` with a note, so the screen shows
        # a document that did not index rather than one that vanished. The status is
        # 502 rather than 500 because the failure this actually catches is an
        # embeddings provider that did not answer -- upstream's problem, and the same
        # reading `onboarding.py` applies to an unreachable website.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_error(
                "indexing_failed",
                "We could not finish indexing that file just now — the embedding "
                "service did not answer. Your file was not indexed; please try again "
                "in a moment.",
            ),
        ) from exc
    except ExtractorMissingError as exc:
        # OURS, not theirs, and the status says so. A 4xx here would tell a customer
        # their file is broken because we did not install a parser.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_error(
                "extractor_unavailable",
                f"This deployment cannot read {exc.kind} files yet. Nothing is wrong "
                "with your file — please try a different format, or contact support.",
            ),
        ) from exc

    report = outcome.report
    records = await documents.list(limit=LIST_LIMIT)
    stored = next((record for record in records if record.id == outcome.document_id), None)
    if stored is None:  # pragma: no cover - the row was just written under this tenant
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_error("not_recorded", "The document was indexed but not recorded."),
        )

    return UploadResponse(
        document=_out(stored),
        status=report.status,
        chunks_stored=report.chunks_stored,
        chunks_duplicate=report.chunks_duplicate,
        chars_extracted=report.chars_extracted,
        note=report.note,
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and everything retrievable from it",
)
async def delete_document(
    document_id: UUID,
    documents: Annotated[PostgresDocumentStore, Depends(get_documents)],
) -> Response:
    """404 for a document that is not this business's, decided by RLS rather than by
    an ``if``: another tenant's document is simply not found."""
    if not await documents.delete(document_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_error("not_found", "No such document."),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
