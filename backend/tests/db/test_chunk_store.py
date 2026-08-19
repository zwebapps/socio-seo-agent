"""The pgvector chunk store, against a real Postgres with RLS switched on.

Three things are being proved here, and only the first is ordinary:

1. **Round trip.** What is upserted comes back out of ``search``, nearest first,
   with the raw ``<=>`` distance rather than a similarity the service would have
   to guess the metric of.

2. **The wrong-citation bug.** ``kb_service.ingest_document`` skips a chunk whose
   ``content_hash`` this business has already embedded. Skipping the *embedding
   call* is right -- the vector for identical text is identical. Skipping the
   *row* is not: the paragraph then exists only under the document that
   introduced it, so every later retrieval cites that file, and a customer whose
   2025 price list repeats a paragraph from the 2024 one is told the claim is
   grounded in the 2024 document. For a product whose whole promise is "grounded
   in your own documents, with a citation", naming the wrong file is worse than
   naming none. The fix lives here, in the adapter, because ``kb_chunks.embedding``
   is NOT NULL and only the store can copy the existing vector by hash.

3. **Tenancy, asserted positively.** The runtime connects as a restricted role and
   RLS keys off a transaction-local GUC, so a query that forgets to scope itself
   returns ZERO rows *silently* (docs/ARCHITECTURE.md section 6). A test that only
   asserted "business B sees nothing" would therefore pass against a store that is
   simply broken. Every isolation test below also reads a known row back as the
   owning business.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator, Sequence
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.db import session as session_module
from backend.app.db.adapters import PgVectorChunkStore
from backend.app.db.adapters.chunk_store import (
    EmbeddingWidthError,
    MissingEmbeddingError,
)
from backend.app.db.models import EMBEDDING_DIM
from backend.app.db.session import business_session
from backend.app.services.kb_service import StoredChunk

pytestmark = [pytest.mark.db]

#: The paragraph that appears in two different uploads. Realistic on purpose: a
#: repeated boilerplate paragraph across two price lists is the common case, not
#: a contrived one.
SHARED_PARAGRAPH = (
    "Alle Preise verstehen sich inklusive Anfahrt im Umkreis von 30 km um "
    "Koblenz. Notdienst ausserhalb der Geschaeftszeiten wird mit einem "
    "Zuschlag von 50 Prozent berechnet."
)


def content_hash(text_value: str) -> str:
    """The same sha256 hex digest the kb engine puts on a chunk."""
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def unit_vector(axis: int) -> list[float]:
    """A basis vector: exactly one 1.0, everything else 0.0.

    Hand-built rather than embedder-produced so the cosine distances in the
    ordering test are known constants instead of whatever a hash happened to
    yield.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    return vector


def diagonal_vector(first: int, second: int) -> list[float]:
    """The normalised bisector of two axes -- cosine distance 1 - 1/sqrt(2)."""
    value = 2**-0.5
    vector = [0.0] * EMBEDDING_DIM
    vector[first] = value
    vector[second] = value
    return vector


@pytest.fixture
def scoped_sessions(app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the process-wide session factory at this test's engine.

    The adapter deliberately offers no way to inject a session: every method goes
    through ``business_session`` so it is impossible to call one without the
    tenant GUC set. That leaves exactly one seam for a test, which is the factory
    itself -- and patching it keeps the RLS scoping under test rather than
    replacing it with a hand-rolled copy that could differ.

    The engine is function-scoped because an asyncpg pool belongs to the event
    loop that created it (see this package's conftest).
    """
    monkeypatch.setattr(
        session_module,
        "_session_factory",
        async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False),
    )
    yield


@pytest.fixture
def store(scoped_sessions: None) -> PgVectorChunkStore:
    return PgVectorChunkStore()


async def make_document(business_id: UUID, filename: str) -> UUID:
    """Insert a ``documents`` row as the business itself, and return its id."""
    document_id = uuid4()
    async with business_session(business_id) as db:
        await db.execute(
            text(
                "INSERT INTO documents (id, business_id, filename, kind, status) "
                "VALUES (:id, :business_id, :filename, 'md', 'indexed')"
            ),
            {"id": document_id, "business_id": business_id, "filename": filename},
        )
    return document_id


@pytest.fixture
async def business_a(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[0]


@pytest.fixture
async def business_b(two_businesses: tuple[UUID, UUID]) -> AsyncIterator[UUID]:
    yield two_businesses[1]


def chunk(
    *,
    ordinal: int,
    content: str,
    embedding: Sequence[float],
    filename: str,
) -> StoredChunk:
    return StoredChunk(
        ordinal=ordinal,
        content=content,
        content_hash=content_hash(content),
        embedding=list(embedding),
        meta={"filename": filename, "kind": "md"},
    )


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


async def test_upsert_then_search_returns_the_stored_chunk(
    store: PgVectorChunkStore, business_a: UUID
) -> None:
    document_id = await make_document(business_a, "preisliste-2024.md")
    stored = await store.upsert(
        business_a,
        document_id,
        [
            chunk(
                ordinal=0,
                content="Badsanierung ab 8.500 Euro.",
                embedding=unit_vector(0),
                filename="preisliste-2024.md",
            ),
            chunk(
                ordinal=1,
                content="Heizungswartung kostet 149 Euro.",
                embedding=unit_vector(1),
                filename="preisliste-2024.md",
            ),
        ],
    )
    assert stored == 2

    results = await store.search(business_a, unit_vector(1), limit=5)

    assert [result.content for result in results] == [
        "Heizungswartung kostet 149 Euro.",
        "Badsanierung ab 8.500 Euro.",
    ]
    nearest = results[0]
    assert nearest.document_id == document_id
    assert nearest.ordinal == 1
    assert nearest.meta == {"filename": "preisliste-2024.md", "kind": "md"}
    assert nearest.distance == pytest.approx(0.0, abs=1e-6)
    assert isinstance(nearest.chunk_id, UUID)


async def test_reingesting_the_same_document_updates_rather_than_duplicates(
    store: PgVectorChunkStore, business_a: UUID
) -> None:
    """A re-upload must not double every passage in the index.

    ``kb_chunks`` is unique on (document_id, ordinal), so this is the store's own
    idempotency: the second write lands on the same row.
    """
    document_id = await make_document(business_a, "leistungen.md")
    first = chunk(
        ordinal=0,
        content="Wir arbeiten in Koblenz.",
        embedding=unit_vector(0),
        filename="leistungen.md",
    )
    await store.upsert(business_a, document_id, [first])

    corrected = chunk(
        ordinal=0,
        content="Wir arbeiten in Koblenz und Umgebung.",
        embedding=unit_vector(2),
        filename="leistungen-v2.md",
    )
    await store.upsert(business_a, document_id, [corrected])

    results = await store.search(business_a, unit_vector(2), limit=10)
    assert len(results) == 1
    assert results[0].content == "Wir arbeiten in Koblenz und Umgebung."
    assert results[0].meta["filename"] == "leistungen-v2.md"


# --------------------------------------------------------------------------- #
# The wrong-citation bug
# --------------------------------------------------------------------------- #


async def test_duplicate_paragraph_is_cited_as_the_document_it_came_from(
    store: PgVectorChunkStore, business_a: UUID
) -> None:
    """The bug this adapter exists to close.

    The 2024 price list and the 2025 price list share a boilerplate paragraph.
    The service embeds it once -- ``existing_hashes`` reports the hash on the
    second ingest, so no second embedding is paid for and the chunk arrives with
    no vector of its own. The store must still give the 2025 document its own
    row, copying the vector it already holds for that hash.

    Without that, the paragraph exists only under the 2024 document, and a
    retrieval that grounds a claim in the 2025 price list cites the wrong file.
    """
    older = await make_document(business_a, "preisliste-2024.md")
    newer = await make_document(business_a, "preisliste-2025.md")
    shared_hash = content_hash(SHARED_PARAGRAPH)
    vector = diagonal_vector(3, 4)

    await store.upsert(
        business_a,
        older,
        [
            chunk(
                ordinal=0,
                content=SHARED_PARAGRAPH,
                embedding=vector,
                filename="preisliste-2024.md",
            )
        ],
    )

    # Exactly what the service hands over on the second document: the hash is
    # already known, so nothing was embedded and `embedding` is empty.
    duplicate = StoredChunk(
        ordinal=0,
        content=SHARED_PARAGRAPH,
        content_hash=shared_hash,
        embedding=[],
        meta={"filename": "preisliste-2025.md", "kind": "md"},
    )
    written = await store.upsert(business_a, newer, [duplicate])
    assert written == 1, "the duplicate paragraph must get its own row, not be dropped"

    results = await store.search(business_a, vector, limit=10)

    cited = {result.document_id: result.meta["filename"] for result in results}
    assert cited == {
        older: "preisliste-2024.md",
        newer: "preisliste-2025.md",
    }, "each document must cite itself, not the document that introduced the text"

    assert {result.content for result in results} == {SHARED_PARAGRAPH}
    # The copied vector is the stored one, so both rows sit at the same distance.
    assert results[0].distance == pytest.approx(results[1].distance, abs=1e-9)
    assert results[0].distance == pytest.approx(0.0, abs=1e-6)


async def test_chunk_without_a_vector_and_without_a_known_hash_is_refused(
    store: PgVectorChunkStore, business_a: UUID
) -> None:
    """Copy-by-hash is a copy, never an invention.

    ``kb_chunks.embedding`` is NOT NULL. A chunk with no vector and no stored
    vector for its hash cannot be written, and must fail loudly rather than land
    as a zero vector -- which would be equidistant from nothing and would quietly
    pollute every search.
    """
    document_id = await make_document(business_a, "neu.md")
    orphan = StoredChunk(
        ordinal=0,
        content="Ein Absatz, den es hier noch nie gab.",
        content_hash=content_hash("Ein Absatz, den es hier noch nie gab."),
        embedding=[],
        meta={},
    )

    with pytest.raises(MissingEmbeddingError) as caught:
        await store.upsert(business_a, document_id, [orphan])

    assert orphan.content_hash in str(caught.value)


async def test_a_hash_known_only_to_another_business_is_not_copied(
    store: PgVectorChunkStore, business_a: UUID, business_b: UUID
) -> None:
    """Copy-by-hash may never reach across the tenant boundary.

    Two businesses can legitimately upload the same public paragraph. Copying B's
    vector into A's row would be a cross-tenant read dressed up as an
    optimisation.
    """
    a_document = await make_document(business_a, "a.md")
    b_document = await make_document(business_b, "b.md")
    shared = chunk(
        ordinal=0,
        content=SHARED_PARAGRAPH,
        embedding=unit_vector(7),
        filename="a.md",
    )
    await store.upsert(business_a, a_document, [shared])

    with pytest.raises(MissingEmbeddingError):
        await store.upsert(
            business_b,
            b_document,
            [
                StoredChunk(
                    ordinal=0,
                    content=SHARED_PARAGRAPH,
                    content_hash=shared.content_hash,
                    embedding=[],
                    meta={},
                )
            ],
        )

    # Positive half: business A still holds the row RLS hid from B.
    owner_view = await store.search(business_a, unit_vector(7), limit=5)
    assert [result.document_id for result in owner_view] == [a_document]


# --------------------------------------------------------------------------- #
# Ordering, width, hashes
# --------------------------------------------------------------------------- #


async def test_search_orders_by_cosine_distance_nearest_first(
    store: PgVectorChunkStore, business_a: UUID
) -> None:
    """Known-distance fixture: 0.0, 1 - 1/sqrt(2), and 1.0 against axis 0."""
    document_id = await make_document(business_a, "distanzen.md")
    await store.upsert(
        business_a,
        document_id,
        [
            chunk(ordinal=0, content="far", embedding=unit_vector(1), filename="d.md"),
            chunk(ordinal=1, content="near", embedding=unit_vector(0), filename="d.md"),
            chunk(ordinal=2, content="middle", embedding=diagonal_vector(0, 1), filename="d.md"),
        ],
    )

    results = await store.search(business_a, unit_vector(0), limit=3)

    assert [result.content for result in results] == ["near", "middle", "far"]
    assert [result.distance for result in results] == [
        pytest.approx(0.0, abs=1e-6),
        pytest.approx(1 - 2**-0.5, abs=1e-6),
        pytest.approx(1.0, abs=1e-6),
    ]


async def test_search_honours_the_limit(store: PgVectorChunkStore, business_a: UUID) -> None:
    document_id = await make_document(business_a, "viele.md")
    await store.upsert(
        business_a,
        document_id,
        [
            chunk(
                ordinal=index,
                content=f"chunk {index}",
                embedding=unit_vector(index),
                filename="v.md",
            )
            for index in range(5)
        ],
    )

    results = await store.search(business_a, unit_vector(0), limit=2)
    assert len(results) == 2
    assert results[0].content == "chunk 0"


async def test_a_wrong_width_vector_is_refused_before_postgres_sees_it(
    store: PgVectorChunkStore, business_a: UUID
) -> None:
    """A 1536-column type would raise its own error; ours says which chunk.

    Checked in the adapter so the message names the ordinal and both widths,
    rather than surfacing as an opaque ``DataError`` from the driver.
    """
    document_id = await make_document(business_a, "schmal.md")
    good = chunk(ordinal=0, content="ok", embedding=unit_vector(0), filename="s.md")
    await store.upsert(business_a, document_id, [good])

    narrow = StoredChunk(
        ordinal=1,
        content="zu schmal",
        content_hash=content_hash("zu schmal"),
        embedding=[0.1, 0.2, 0.3],
        meta={},
    )
    with pytest.raises(EmbeddingWidthError) as caught:
        await store.upsert(business_a, document_id, [narrow])
    assert "3" in str(caught.value)
    assert str(EMBEDDING_DIM) in str(caught.value)

    with pytest.raises(EmbeddingWidthError):
        await store.search(business_a, [0.1, 0.2, 0.3], limit=1)

    # Nothing was half-written, and the good row is still there.
    results = await store.search(business_a, unit_vector(0), limit=10)
    assert [result.content for result in results] == ["ok"]


async def test_existing_hashes_returns_only_hashes_this_business_has(
    store: PgVectorChunkStore, business_a: UUID, business_b: UUID
) -> None:
    a_document = await make_document(business_a, "a.md")
    b_document = await make_document(business_b, "b.md")
    known = chunk(ordinal=0, content="bekannt", embedding=unit_vector(0), filename="a.md")
    only_b = chunk(ordinal=0, content="nur bei B", embedding=unit_vector(1), filename="b.md")
    await store.upsert(business_a, a_document, [known])
    await store.upsert(business_b, b_document, [only_b])

    unknown = content_hash("nie gesehen")

    assert await store.existing_hashes(
        business_a, [known.content_hash, only_b.content_hash, unknown]
    ) == {known.content_hash}
    assert await store.existing_hashes(business_b, [known.content_hash, only_b.content_hash]) == {
        only_b.content_hash
    }
    assert await store.existing_hashes(business_a, []) == set()


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_one_business_cannot_search_another_businesss_chunks(
    store: PgVectorChunkStore, business_a: UUID, business_b: UUID
) -> None:
    """The zero-rows trap, asserted from both sides.

    RLS returns nothing when the GUC does not match, and nothing is also what a
    completely broken store returns -- so the negative assertion alone proves
    nothing. The positive read is the control.
    """
    document_id = await make_document(business_a, "vertraulich.md")
    secret = chunk(
        ordinal=0,
        content="Interne Marge: 42 Prozent.",
        embedding=unit_vector(9),
        filename="vertraulich.md",
    )
    await store.upsert(business_a, document_id, [secret])

    assert await store.search(business_b, unit_vector(9), limit=10) == []

    owner_view = await store.search(business_a, unit_vector(9), limit=10)
    assert [result.content for result in owner_view] == ["Interne Marge: 42 Prozent."]
    assert owner_view[0].document_id == document_id
