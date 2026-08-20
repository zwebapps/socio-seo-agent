"""Postgres-backed document rows: the register of what a business has uploaded.

The `documents` table has existed since the Phase 1-6 schema and nothing wrote to
it. `kb_service.ingest_document` was called only from tests, so there was no upload
route, no UI, and no row -- the knowledge base was built, tested, and unreachable in
the running product. This adapter is the missing half of that path.

The business id is a constructor argument, not a per-call one, for the reason
`PostgresRunStore`'s docstring records at length: every route reaching this store
already depends on `current_business`, so the tenant is known before the question is
asked, and taking it here means row-level security does the isolation rather than an
``if`` in application code. A document belonging to another tenant is simply not
found.

`chunk_count` and `extraction_note` are stored rather than derived, and the reason is
the product's own rule about honesty: a scanned PDF yields no text, and a document
row saying `indexed` with zero chunks would tell a customer their price list is
searchable when none of it is. The status IS the answer to "what happened to my
file", so it is written by the one code path that knows.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update

from backend.app.db.models import Document, KbChunk
from backend.app.db.session import business_session

__all__ = ["DocumentRecord", "PostgresDocumentStore"]


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """One uploaded document, as the API and the UI see it.

    Deliberately not the ORM entity. `storage_key` is absent because nothing stores
    the bytes yet (see `document_service`), and returning a column that is always
    ``None`` invites a screen to render a download link that cannot work.
    """

    id: UUID
    business_id: UUID
    filename: str
    kind: str
    status: str
    chunk_count: int
    extraction_note: str | None
    created_at: datetime


class PostgresDocumentStore:
    """Document rows for one business. One instance per request."""

    def __init__(self, business_id: UUID) -> None:
        self._business_id = business_id

    async def create(self, *, filename: str, kind: str) -> UUID:
        """Register a document as `pending` and return its id.

        The row is written BEFORE extraction, on purpose. Ingestion embeds and can
        take seconds, and a file whose row appears only on success is a file that
        vanishes when it fails -- leaving a customer who watched an upload finish
        with nothing on the screen and nothing to report.
        """
        document_id = uuid4()
        async with business_session(self._business_id) as session:
            session.add(
                Document(
                    id=document_id,
                    business_id=self._business_id,
                    filename=filename,
                    kind=kind,
                    status="pending",
                    chunk_count=0,
                )
            )
        return document_id

    async def finish(
        self,
        document_id: UUID,
        *,
        status: str,
        chunk_count: int,
        extraction_note: str | None,
    ) -> None:
        """Record what ingestion concluded. Never raises for an unknown id.

        A missing row means the document was deleted while it was being indexed,
        which is a race a customer is allowed to win: they asked for it to be gone.
        """
        async with business_session(self._business_id) as session:
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(
                    status=status,
                    chunk_count=chunk_count,
                    extraction_note=extraction_note,
                )
            )

    async def list(self, *, limit: int = 100) -> Sequence[DocumentRecord]:
        """This business's documents, newest first.

        No ``WHERE business_id`` and that is the design: the session is opened under
        this request's tenant, so RLS decides whose documents come back. A filter here
        as well would work today and would quietly become the thing doing the
        isolation.
        """
        async with business_session(self._business_id) as session:
            rows = (
                await session.execute(
                    select(
                        Document.id,
                        Document.business_id,
                        Document.filename,
                        Document.kind,
                        Document.status,
                        Document.chunk_count,
                        Document.extraction_note,
                        Document.created_at,
                    )
                    # `id` breaks the tie so two documents uploaded in the same
                    # microsecond come back in a stable order across requests.
                    .order_by(Document.created_at.desc(), Document.id.desc())
                    .limit(limit)
                )
            ).all()

        return [
            DocumentRecord(
                id=row.id,
                business_id=row.business_id,
                filename=row.filename,
                kind=row.kind,
                status=row.status,
                chunk_count=row.chunk_count,
                extraction_note=row.extraction_note,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def delete(self, document_id: UUID) -> bool:
        """Remove a document and its chunks. Returns whether anything was removed.

        `kb_chunks.document_id` is ``ON DELETE CASCADE``, so the chunks would go with
        the row -- but they are deleted explicitly first anyway, because a cascade is
        a schema fact and this is the one operation where leaving retrievable text
        behind would be actively harmful: an owner deleting a document is often
        deleting it because its contents should stop being used.
        """
        async with business_session(self._business_id) as session:
            await session.execute(delete(KbChunk).where(KbChunk.document_id == document_id))
            # `returning` rather than `rowcount`: the row count on an async result is
            # driver-dependent, and "did this delete anything" is the difference
            # between a 204 and a 404.
            removed = (
                await session.execute(
                    delete(Document).where(Document.id == document_id).returning(Document.id)
                )
            ).scalar_one_or_none()
        return removed is not None

    async def chunk_count(self) -> int:
        """How many retrievable chunks this business has, across every document.

        Read on the way into a run: with nothing indexed, retrieval is left UNWIRED so
        HARVEST records "uploaded documents" as a gap. A wired retriever over an empty
        store returns "nothing relevant", which reads as a business whose own material
        had nothing to say about it.
        """
        async with business_session(self._business_id) as session:
            total = (await session.execute(select(func.count()).select_from(KbChunk))).scalar_one()
        return int(total)
