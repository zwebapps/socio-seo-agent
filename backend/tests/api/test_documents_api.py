"""The document upload path, which the knowledge base did not have.

Every piece of the knowledge base shipped and none of it was reachable:
`kb_service.ingest_document` had twenty tests and no caller, so no business could
ever hold a chunk. These tests are about the route's REFUSALS and its honesty,
because the ingest itself is already covered against the real engine in
`tests/services/test_kb_service.py`.

Hermetic. The document store, the chunk store and the embedder are all overridden,
so there is no database, no vector column and no provider call — the same posture as
every other api test here.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from backend.app.api import documents as documents_api
from backend.app.api.auth import current_user
from backend.app.api.runs import current_business
from backend.app.db.adapters.document_store import DocumentRecord
from backend.app.db.models import User
from backend.app.engines.kb import ExtractorUnavailableError
from backend.app.main import create_app
from backend.app.services import kb_service
from backend.app.services.kb_service import StoredChunk

pytestmark = pytest.mark.anyio

BUSINESS = UUID("11111111-1111-1111-1111-111111111111")
OTHER_BUSINESS = UUID("22222222-2222-2222-2222-222222222222")

PRICE_LIST = (
    b"# Preisliste 2026\n\n"
    b"Rohrreinigung ab 89 EUR. Notdienst nachts und am Wochenende ab 149 EUR.\n"
    b"Heizungswartung 129 EUR inklusive Anfahrt im Stadtgebiet Koblenz.\n"
    b"Badsanierung nach Aufmass, Festpreis nach Besichtigung.\n"
) * 4


def _user() -> User:
    return User(id=uuid4(), email="owner@example.com", is_active=True, role="user")


class FakeDocuments:
    """The document register, in memory. Scoped to one business, like the real one."""

    def __init__(self, business_id: UUID = BUSINESS) -> None:
        self.business_id = business_id
        self.rows: dict[UUID, DocumentRecord] = {}
        self.finished: list[tuple[UUID, str, int, str | None]] = []

    async def create(self, *, filename: str, kind: str) -> UUID:
        from datetime import UTC, datetime

        document_id = uuid4()
        self.rows[document_id] = DocumentRecord(
            id=document_id,
            business_id=self.business_id,
            filename=filename,
            kind=kind,
            status="pending",
            chunk_count=0,
            extraction_note=None,
            created_at=datetime.now(UTC),
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
        self.finished.append((document_id, status, chunk_count, extraction_note))
        row = self.rows.get(document_id)
        if row is None:
            return
        self.rows[document_id] = DocumentRecord(
            id=row.id,
            business_id=row.business_id,
            filename=row.filename,
            kind=row.kind,
            status=status,
            chunk_count=chunk_count,
            extraction_note=extraction_note,
            created_at=row.created_at,
        )

    async def list(self, *, limit: int = 100) -> list[DocumentRecord]:
        return sorted(self.rows.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    async def delete(self, document_id: UUID) -> bool:
        return self.rows.pop(document_id, None) is not None

    async def chunk_count(self) -> int:
        return sum(row.chunk_count for row in self.rows.values())


class FakeChunks:
    """Satisfies `kb_service.ChunkStore`. Records what it was asked to write."""

    def __init__(self) -> None:
        self.written: list[StoredChunk] = []
        self.hashes: set[str] = set()

    async def upsert(self, business_id: UUID, document_id: UUID, chunks: Any) -> int:
        stored = [c for c in chunks if c.content_hash not in self.hashes]
        self.written.extend(stored)
        self.hashes.update(c.content_hash for c in stored)
        return len(stored)

    async def search(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def existing_hashes(self, business_id: UUID, hashes: Any) -> set[str]:
        return {h for h in hashes if h in self.hashes}


class FakeEmbedder:
    """Deterministic vectors. Arithmetic over a hash, exactly like the real fake."""

    dimensions = 8

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[float(len(text) % 7)] * self.dimensions for text in texts]


class ExplodingEmbedder:
    async def embed(self, texts: Any) -> list[list[float]]:
        raise RuntimeError("the embeddings endpoint refused the request")


def _client(
    documents: FakeDocuments,
    *,
    chunks: Any = None,
    embedder: Any = None,
    authenticated: bool = True,
    business_id: UUID = BUSINESS,
) -> httpx.AsyncClient:
    app = create_app()
    if authenticated:
        app.dependency_overrides[current_user] = _user
        app.dependency_overrides[current_business] = lambda: business_id
    app.dependency_overrides[documents_api.get_documents] = lambda: documents
    app.dependency_overrides[documents_api.get_chunks] = lambda: chunks or FakeChunks()
    app.dependency_overrides[documents_api.get_embedder] = lambda: embedder or FakeEmbedder()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _upload(name: str, data: bytes) -> dict[str, Any]:
    return {"files": {"file": (name, data, "application/octet-stream")}}


# --------------------------------------------------------------------------- #
# The gap this closes
# --------------------------------------------------------------------------- #


async def test_a_document_is_indexed_and_the_chunks_are_reported() -> None:
    """The whole point: a customer's own material becomes retrievable, and the
    response says how much of it did — not merely that the upload worked."""
    documents = FakeDocuments()
    chunks = FakeChunks()

    async with _client(documents, chunks=chunks) as client:
        response = await client.post("/api/v1/documents", **_upload("preisliste.md", PRICE_LIST))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "indexed"
    assert body["chunksStored"] >= 1
    assert body["document"]["status"] == "indexed"
    assert body["document"]["chunkCount"] == body["chunksStored"]
    assert chunks.written, "the chunks must actually reach the store"


async def test_re_uploading_the_same_document_reports_duplicates_rather_than_re_embedding() -> None:
    """Dedup by content hash is `docs/ARCHITECTURE.md` §6's promise. A customer who
    re-uploads last month's price list should be told nothing changed."""
    documents = FakeDocuments()
    chunks = FakeChunks()

    async with _client(documents, chunks=chunks) as client:
        first = await client.post("/api/v1/documents", **_upload("p.md", PRICE_LIST))
        second = await client.post("/api/v1/documents", **_upload("p.md", PRICE_LIST))

    assert first.json()["chunksStored"] >= 1
    assert second.json()["chunksStored"] == 0
    assert second.json()["chunksDuplicate"] >= 1


async def test_a_document_with_no_extractable_text_is_not_reported_as_indexed() -> None:
    """A scan indexes nothing. Saying "indexed" would tell a customer their price
    list is searchable when none of it is, which is the failure this path is shaped
    around."""
    documents = FakeDocuments()

    async with _client(documents) as client:
        response = await client.post("/api/v1/documents", **_upload("scan.txt", b"   \n\t  \n"))

    assert response.status_code == 201
    assert response.json()["status"] == "no_text"
    assert response.json()["document"]["chunkCount"] == 0


# --------------------------------------------------------------------------- #
# Refusals, and whose problem each one is
# --------------------------------------------------------------------------- #


async def test_an_unreadable_format_is_refused_before_any_parser_sees_it() -> None:
    """An allowlist, checked on the SUFFIX. A PDF parser is a third-party reader of
    hostile-by-default input, and the cheapest defence is not handing it a file
    nobody asked us to read."""
    documents = FakeDocuments()

    async with _client(documents) as client:
        response = await client.post("/api/v1/documents", **_upload("payload.exe", b"MZ\x90\x00"))

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_kind"
    assert not documents.rows, "nothing may be registered for a file we refuse to read"


async def test_a_missing_extractor_is_reported_as_our_problem_and_not_the_customers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503, not 4xx. Telling a customer their PDF is broken because we did not install
    `pypdf` is a lie with a support ticket attached.

    The extractor is stubbed rather than the dependency uninstalled, because the point
    under test is the ROUTE's reading of `ExtractorUnavailableError` -- and a test that
    only passes on a machine without `pypdf` would pass for the wrong reason.
    """
    documents = FakeDocuments()

    def exploding(*args: Any, **kwargs: Any) -> Any:
        raise ExtractorUnavailableError("pdf", "pypdf")

    monkeypatch.setattr(kb_service, "extract_text", exploding)

    async with _client(documents) as client:
        response = await client.post("/api/v1/documents", **_upload("brochure.pdf", b"%PDF-1.7"))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "extractor_unavailable"
    # The row still exists, marked failed, with the package named: an operator has to
    # be able to see this in the product rather than only in a log.
    assert documents.finished[-1][1] == "failed"
    assert "pypdf" in (documents.finished[-1][3] or "")


async def test_an_empty_file_is_refused() -> None:
    documents = FakeDocuments()

    async with _client(documents) as client:
        response = await client.post("/api/v1/documents", **_upload("empty.md", b""))

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "empty_file"


async def test_an_ingest_that_fails_unexpectedly_never_leaves_a_pending_row() -> None:
    """A `pending` row nothing will come back to renders as "still indexing" forever,
    which is the one state this path must not leave behind."""
    documents = FakeDocuments()

    async with _client(documents, embedder=ExplodingEmbedder()) as client:
        response = await client.post("/api/v1/documents", **_upload("p.md", PRICE_LIST))

    # 502: an embeddings endpoint that did not answer is upstream's failure, and the
    # same reading `onboarding.py` applies to an unreachable website.
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "indexing_failed"
    assert documents.finished[-1][1] == "failed"
    assert all(row.status != "pending" for row in documents.rows.values())


async def test_the_upload_route_needs_a_session() -> None:
    documents = FakeDocuments()

    async with _client(documents, authenticated=False) as client:
        response = await client.post("/api/v1/documents", **_upload("p.md", PRICE_LIST))

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Listing and deleting
# --------------------------------------------------------------------------- #


async def test_the_list_reports_the_total_the_agent_can_actually_retrieve() -> None:
    """`totalChunks` is the number that decides whether retrieval is wired into a run
    at all, so the screen shows it rather than a document count."""
    documents = FakeDocuments()

    async with _client(documents) as client:
        await client.post("/api/v1/documents", **_upload("p.md", PRICE_LIST))
        response = await client.get("/api/v1/documents")

    assert response.status_code == 200
    body = response.json()
    assert len(body["documents"]) == 1
    assert body["totalChunks"] == body["documents"][0]["chunkCount"]
    assert response.headers["cache-control"] == "no-store"


async def test_deleting_a_document_that_is_not_ours_is_a_404_not_a_403() -> None:
    """Decided by row-level security rather than by an ``if``: another tenant's
    document is simply not found, and a 403 would confirm it exists."""
    documents = FakeDocuments()

    async with _client(documents) as client:
        response = await client.delete(f"/api/v1/documents/{uuid4()}")

    assert response.status_code == 404


async def test_deleting_a_document_removes_it_from_the_list() -> None:
    documents = FakeDocuments()

    async with _client(documents) as client:
        created = await client.post("/api/v1/documents", **_upload("p.md", PRICE_LIST))
        document_id = created.json()["document"]["id"]
        deleted = await client.delete(f"/api/v1/documents/{document_id}")
        remaining = await client.get("/api/v1/documents")

    assert deleted.status_code == 204
    assert remaining.json()["documents"] == []
