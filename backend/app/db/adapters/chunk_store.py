"""``ChunkStore`` over ``kb_chunks`` + pgvector.

Implements the port declared in ``backend.app.services.kb_service``. Nothing in
here decides anything about retrieval; it stores rows, and it returns the raw
``<=>`` distance so the service never has to guess which metric produced it.

**The one piece of real behaviour: copy-by-hash on upsert.**

``ingest_document`` asks ``existing_hashes`` which chunk hashes this business has
already embedded, and does not embed those again. That is right -- identical text
has an identical vector, and paying for it twice is waste. What it must *not*
mean is that the second document has no row: ``kb_chunks`` is where a citation
comes from, so a paragraph that exists only under the document that introduced it
gets cited as that document forever. A customer whose 2025 price list repeats a
paragraph from the 2024 one is then told, with a citation, that the claim comes
from the 2024 file. For a product whose entire claim is "grounded in your own
documents", a confident wrong filename is worse than no citation.

So a :class:`~backend.app.services.kb_service.StoredChunk` may arrive with an
empty ``embedding``, meaning *"this business already has a vector for this hash --
use it"*. The copy happens in SQL, from a row the current tenant can already see,
which means the tenant boundary on the copy is enforced by the RLS policy rather
than by a WHERE clause somebody could forget. It has to happen here rather than
in the service because ``kb_chunks.embedding`` is NOT NULL and the service holds
no vector to write.

The copy is a copy, never an invention: no vector and no matching hash is a
:class:`MissingEmbeddingError`, not a zero vector. A zero vector would be
equidistant from everything and would quietly degrade every later search.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, Result, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import EMBEDDING_DIM
from backend.app.db.session import business_session
from backend.app.services.kb_service import RetrievedChunk, StoredChunk

__all__ = [
    "ChunkStoreError",
    "EmbeddingWidthError",
    "MissingEmbeddingError",
    "PgVectorChunkStore",
]


class ChunkStoreError(Exception):
    """Base class for failures raised by the chunk store."""


class EmbeddingWidthError(ChunkStoreError):
    """A vector arrived at a width the column cannot hold.

    Raised before any statement runs, so the message names the chunk and both
    widths instead of surfacing as a driver-level ``DataError`` about a value
    nobody can locate.
    """

    def __init__(self, *, expected: int, received: int, where: str) -> None:
        self.expected = expected
        self.received = received
        super().__init__(
            f"{where}: embedding has {received} dimensions, but kb_chunks.embedding "
            f"is vector({expected}). Changing the width is a migration, not a "
            "configuration change -- see EMBEDDING_DIM in backend/app/db/models.py."
        )


class MissingEmbeddingError(ChunkStoreError):
    """A chunk arrived with no vector, and this business has no vector to copy.

    ``kb_chunks.embedding`` is NOT NULL. Writing zeros instead would store a row
    that is equidistant from every query and would pollute retrieval silently, so
    this fails loudly at the point of the mistake.
    """

    def __init__(self, *, content_hash: str, ordinal: int) -> None:
        self.content_hash = content_hash
        self.ordinal = ordinal
        super().__init__(
            f"Chunk {ordinal} carries no embedding and this business has no stored "
            f"vector for content_hash {content_hash}. A chunk with no vector is only "
            "storable when its text was already embedded for this business; embed it "
            "and pass the vector."
        )


#: One row of the batch, ready to write. ``embedding`` is the pgvector literal, or
#: ``None`` when the row must copy the vector this business already holds.
_INSERT_WITH_VECTOR = text(
    """
    INSERT INTO kb_chunks
        (id, business_id, document_id, ordinal, content, content_hash, embedding, meta)
    VALUES
        (:id, :business_id, :document_id, :ordinal, :content, :content_hash,
         (:embedding)::text::vector, (:meta)::text::jsonb)
    ON CONFLICT (document_id, ordinal) DO UPDATE SET
        content = EXCLUDED.content,
        content_hash = EXCLUDED.content_hash,
        embedding = EXCLUDED.embedding,
        meta = EXCLUDED.meta,
        updated_at = now()
    """
)

#: The copy-by-hash write. The source row is selected from ``kb_chunks`` under the
#: caller's own RLS scope, so a vector can never be copied across tenants: a
#: business that cannot see the row cannot copy it, enforced by the database.
_INSERT_COPYING_VECTOR = text(
    """
    INSERT INTO kb_chunks
        (id, business_id, document_id, ordinal, content, content_hash, embedding, meta)
    SELECT
        :id, :business_id, :document_id, :ordinal, :content, (:content_hash)::varchar,
        source.embedding, (:meta)::text::jsonb
    FROM kb_chunks AS source
    WHERE source.business_id = :business_id
      AND source.content_hash = (:content_hash)::varchar
    ORDER BY source.created_at
    LIMIT 1
    ON CONFLICT (document_id, ordinal) DO UPDATE SET
        content = EXCLUDED.content,
        content_hash = EXCLUDED.content_hash,
        embedding = EXCLUDED.embedding,
        meta = EXCLUDED.meta,
        updated_at = now()
    """
)

_SEARCH = text(
    """
    SELECT
        id,
        document_id,
        ordinal,
        content,
        meta,
        embedding <=> (:embedding)::text::vector AS distance
    FROM kb_chunks
    ORDER BY embedding <=> (:embedding)::text::vector
    LIMIT :limit
    """
)

_EXISTING_HASHES = text(
    "SELECT DISTINCT content_hash FROM kb_chunks WHERE content_hash IN :hashes"
).bindparams(bindparam("hashes", expanding=True))


def _vector_literal(embedding: Sequence[float]) -> str:
    """pgvector's text form: ``[0.1,0.2,...]``.

    Built here and cast in SQL rather than bound as a typed parameter, because the
    asyncpg driver has no codec for the ``vector`` type. ``repr`` of a float is the
    shortest string that round-trips exactly, so this loses no precision.
    """
    return "[" + ",".join(repr(float(value)) for value in embedding) + "]"


class PgVectorChunkStore:
    """The knowledge base's persistence, scoped to one business per call.

    Satisfies ``backend.app.services.kb_service.ChunkStore``. Stateless, so one
    instance is shared safely; each method opens its own tenant-scoped
    transaction.
    """

    async def upsert(
        self,
        business_id: UUID,
        document_id: UUID,
        chunks: Sequence[StoredChunk],
    ) -> int:
        """Store ``chunks`` for one document, and return how many rows landed.

        Idempotent on ``(document_id, ordinal)``: re-ingesting a document rewrites
        its rows rather than doubling every passage in the index.

        A chunk whose ``embedding`` is empty copies the vector this business
        already holds for its ``content_hash`` -- see the module docstring; that is
        what stops a repeated paragraph from being cited as the wrong file.

        Every width is checked before anything is written, and the whole batch
        shares one transaction, so a bad chunk leaves nothing half-stored.
        """
        if not chunks:
            return 0

        for chunk in chunks:
            if chunk.embedding and len(chunk.embedding) != EMBEDDING_DIM:
                raise EmbeddingWidthError(
                    expected=EMBEDDING_DIM,
                    received=len(chunk.embedding),
                    where=f"chunk {chunk.ordinal} of document {document_id}",
                )

        written = 0
        async with business_session(business_id) as db:
            for chunk in chunks:
                written += await self._write_chunk(db, business_id, document_id, chunk)
        return written

    async def _write_chunk(
        self,
        db: AsyncSession,
        business_id: UUID,
        document_id: UUID,
        chunk: StoredChunk,
    ) -> int:
        """Write one chunk, copying an existing vector when none was supplied.

        Chunks are written in order, so a batch that carries the vector once and
        repeats the text later resolves against the row it has just written.
        """
        params: dict[str, Any] = {
            "id": uuid4(),
            "business_id": business_id,
            "document_id": document_id,
            "ordinal": chunk.ordinal,
            "content": chunk.content,
            "content_hash": chunk.content_hash,
            "meta": json.dumps(chunk.meta),
        }

        if chunk.embedding:
            params["embedding"] = _vector_literal(chunk.embedding)
            result = await db.execute(_INSERT_WITH_VECTOR, params)
            return _rowcount(result)

        result = await db.execute(_INSERT_COPYING_VECTOR, params)
        written = _rowcount(result)
        if written == 0:
            raise MissingEmbeddingError(
                content_hash=chunk.content_hash,
                ordinal=chunk.ordinal,
            )
        return written

    async def search(
        self,
        business_id: UUID,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> list[RetrievedChunk]:
        """The ``limit`` nearest chunks for this business, nearest first.

        Ordered by cosine distance (``<=>``), which is the operator class the HNSW
        index is built for -- any other ordering would silently fall back to a
        sequential scan. ``distance`` is returned exactly as pgvector produced it.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        if len(embedding) != EMBEDDING_DIM:
            raise EmbeddingWidthError(
                expected=EMBEDDING_DIM,
                received=len(embedding),
                where="search query vector",
            )

        async with business_session(business_id) as db:
            result = await db.execute(
                _SEARCH,
                {"embedding": _vector_literal(embedding), "limit": limit},
            )
            rows = result.mappings().all()

        return [
            RetrievedChunk(
                chunk_id=row["id"],
                document_id=row["document_id"],
                ordinal=row["ordinal"],
                content=row["content"],
                distance=float(row["distance"]),
                meta=_as_meta(row["meta"]),
            )
            for row in rows
        ]

    async def existing_hashes(self, business_id: UUID, hashes: Sequence[str]) -> set[str]:
        """Which of ``hashes`` this business has already embedded.

        Scoped by RLS, so two businesses that upload the same public paragraph do
        not see each other's hashes -- and the second one pays for its own
        embedding rather than reading a vector it is not entitled to.
        """
        wanted = list(dict.fromkeys(hashes))
        if not wanted:
            return set()

        async with business_session(business_id) as db:
            result = await db.execute(_EXISTING_HASHES, {"hashes": wanted})
            return {str(row[0]) for row in result.all()}


def _rowcount(result: Result[Any]) -> int:
    """How many rows a DML statement touched.

    ``AsyncSession.execute`` is typed as returning ``Result``, but a DML statement
    always yields a ``CursorResult``; the cast is a typing detail, not a runtime
    assumption.
    """
    return int(cast("CursorResult[Any]", result).rowcount)


def _as_meta(value: Any) -> dict[str, Any]:
    """JSONB comes back as a dict, or as text when no codec is registered."""
    if isinstance(value, str):
        decoded: Any = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


if TYPE_CHECKING:  # pragma: no cover - a compile-time conformance check
    from backend.app.services.kb_service import ChunkStore

    def _satisfies_port(store: PgVectorChunkStore) -> ChunkStore:
        """Fails type checking the moment this class drifts from the port."""
        return store
