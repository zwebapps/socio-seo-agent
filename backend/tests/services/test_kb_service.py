"""Tests for the kb service: ingest, and the agentic retrieval loop.

Hermetic. No network, no database, no real model: the ports are an in-memory
:class:`InMemoryChunkStore`, a hash-based :class:`RecordingEmbedder`, and a
:class:`ScriptedProvider` that returns pre-written tool calls. That is the whole
reason ``kb_service`` talks to a ``ChunkStore`` Protocol instead of a session --
the pgvector adapter is wired in behind it later and none of these tests change.

What the retrieval tests are really asserting is that the loop makes the four
decisions a plain retriever cannot (docs/DIAGRAMS.md section 6): *whether* to
retrieve, *what* to ask, *whether the result is good enough*, and *what to do
when it isn't*. Each of those is asserted by observing a consequence, not by
reading a flag:

* "whether" -- when the decision says no, the embedder and the store are never
  touched at all. A pure-flag assertion would pass even if we searched anyway.
* "what to ask" -- the query that reaches the embedder is the rewritten one, and
  the second attempt's query differs from the first.
* "good enough" -- grading is structured tool output, and it happens on the CHEAP
  tier: every other tier in the router points at a provider that raises.
* "what to do" -- exhausting the attempts yields ``fallback_to_web``, and the
  service never searches the web itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.engines.kb import ExtractorUnavailableError, content_hash
from backend.app.llm import (
    Completion,
    FakeProvider,
    Message,
    ModelRouter,
    ModelTier,
    RouteEntry,
    ToolCall,
    ToolSpec,
    Usage,
)
from backend.app.services.kb_service import (
    GRADE_TOOL,
    MAX_RETRIEVAL_ATTEMPTS,
    RETRIEVAL_PROMPT_VERSION,
    EmbeddingCountMismatchError,
    IngestReport,
    RetrievalTrace,
    RetrievedChunk,
    StoredChunk,
    ingest_document,
    retrieve,
)

BUSINESS = UUID("11111111-1111-1111-1111-111111111111")
OTHER_BUSINESS = UUID("22222222-2222-2222-2222-222222222222")
DOCUMENT = UUID("33333333-3333-3333-3333-333333333333")

QUESTION = "Was kostet ein Notdienst am Wochenende?"

PRICE_LIST = (
    "# Preisliste 2026\n\n"
    "Der Notdienst kostet am Wochenende 149 EUR Anfahrt plus Material.\n\n"
    "Eine normale Wartung kostet 89 EUR und dauert etwa eine Stunde.\n"
)


# --------------------------------------------------------------------------- #
# Fake ports
# --------------------------------------------------------------------------- #


class RecordingEmbedder:
    """Deterministic embeddings from a hash, and a record of every call.

    The record is the point: "the duplicate chunk was not re-embedded" is only
    provable by looking at what the embedder was actually asked for.
    """

    def __init__(self, *, dimensions: int = 8) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    @property
    def embedded_texts(self) -> list[str]:
        return [text for call in self.calls for text in call]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index] / 255.0 for index in range(self.dimensions)]


class BrokenEmbedder(RecordingEmbedder):
    """Returns the wrong number of vectors, which must never be stored."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        await super().embed(texts)
        return []


class InMemoryChunkStore:
    """An in-memory stand-in for the pgvector adapter.

    Business-scoped like the real thing, so a test that leaked across tenants
    would fail here rather than in production behind RLS. ``search`` returns the
    business's chunks in insertion order, honouring ``limit`` -- which is what
    lets a test prove that *widening* the second attempt surfaces a chunk the
    first attempt never saw.
    """

    def __init__(self) -> None:
        self.rows: dict[UUID, list[RetrievedChunk]] = {}
        self.searches: list[tuple[UUID, int]] = []
        self.upserts: list[tuple[UUID, UUID, list[StoredChunk]]] = []
        self._hashes: dict[UUID, set[str]] = {}

    async def upsert(
        self,
        business_id: UUID,
        document_id: UUID,
        chunks: Sequence[StoredChunk],
    ) -> int:
        self.upserts.append((business_id, document_id, list(chunks)))
        stored = self.rows.setdefault(business_id, [])
        known = self._hashes.setdefault(business_id, set())
        for chunk in chunks:
            stored.append(
                RetrievedChunk(
                    chunk_id=uuid4(),
                    document_id=document_id,
                    ordinal=chunk.ordinal,
                    content=chunk.content,
                    distance=0.1 * (len(stored) + 1),
                    meta=dict(chunk.meta),
                )
            )
            known.add(chunk.content_hash)
        return len(chunks)

    async def search(
        self,
        business_id: UUID,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> list[RetrievedChunk]:
        assert embedding, "an empty embedding would match everything equally"
        self.searches.append((business_id, limit))
        return list(self.rows.get(business_id, []))[:limit]

    async def existing_hashes(self, business_id: UUID, hashes: Sequence[str]) -> set[str]:
        known = self._hashes.get(business_id, set())
        return {digest for digest in hashes if digest in known}

    def seed(self, business_id: UUID, contents: Sequence[str]) -> list[RetrievedChunk]:
        """Put retrievable chunks in place without going through ingest."""
        rows = self.rows.setdefault(business_id, [])
        known = self._hashes.setdefault(business_id, set())
        for ordinal, content in enumerate(contents):
            rows.append(
                RetrievedChunk(
                    chunk_id=uuid4(),
                    document_id=DOCUMENT,
                    ordinal=ordinal,
                    content=content,
                    distance=0.1 * (ordinal + 1),
                    meta={},
                )
            )
            known.add(content_hash(content))
        return rows


class ScriptedProvider:
    """A model that returns exactly the tool calls a test queues for it.

    Satisfies ``llm.Provider`` structurally. Every call is recorded with the
    tools it was offered, so a test can assert that the grade arrived as
    structured tool arguments rather than being parsed out of prose.
    """

    name = "scripted"

    def __init__(self, script: Sequence[tuple[str, dict[str, Any]] | str]) -> None:
        self.script = list(script)
        self.calls: list[tuple[list[Message], tuple[str, ...]]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        del temperature, max_tokens
        self.calls.append((list(messages), tuple(tool.name for tool in tools or ())))
        if not self.script:
            raise AssertionError(
                f"ScriptedProvider ran out of script on call {len(self.calls)}; "
                "the loop made more model calls than the test expected."
            )
        step = self.script.pop(0)
        usage = Usage(
            provider=self.name,
            model=model,
            tokens_in=10,
            tokens_out=5,
            usd=Decimal("0.0001"),
            latency_ms=1,
        )
        if isinstance(step, str):
            # Prose instead of a tool call: the degradation path.
            return Completion(text=step, tool_calls=[], usage=usage, is_final=True)
        name, arguments = step
        call = ToolCall(name=name, arguments=arguments, call_id=f"call_{len(self.calls)}")
        return Completion(text=None, tool_calls=[call], usage=usage, is_final=False)

    @property
    def offered_tools(self) -> list[tuple[str, ...]]:
        return [tools for _, tools in self.calls]

    def prompt_text(self, index: int) -> str:
        return "\n".join(message.content for message in self.calls[index][0])


class ExplodingProvider:
    """Any use of this provider is a routing bug, so it says so."""

    name = "exploding"

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        raise AssertionError(
            f"retrieval routed a task to {model!r}. Grading and rewriting are "
            "cheap-tier work; anything else is a cost regression."
        )


def cheap_tier_router(provider: ScriptedProvider | FakeProvider) -> ModelRouter:
    """A router where ONLY the cheap tier is reachable.

    Every other tier points at :class:`ExplodingProvider`, so "grading uses the
    cheap tier" is enforced by the test rather than asserted about a constant.
    """
    return ModelRouter(
        providers={"scripted": provider, "exploding": ExplodingProvider()},
        chains={
            ModelTier.CHEAP: (RouteEntry("scripted", "fake/cheap"),),
            ModelTier.MID: (RouteEntry("exploding", "fake/mid"),),
            ModelTier.STRONG: (RouteEntry("exploding", "fake/strong"),),
            ModelTier.EMBED: (RouteEntry("exploding", "fake/embed"),),
        },
    )


def decision(
    *, needed: bool, query: str = "notdienst wochenende preis"
) -> tuple[str, dict[str, Any]]:
    return (
        "record_retrieval_decision",
        {
            "needs_business_facts": needed,
            "retrieval_query": query,
            "reasoning": "The question asks about this business's own prices.",
        },
    )


def rewrite(query: str) -> tuple[str, dict[str, Any]]:
    return (
        "record_retrieval_rewrite",
        {"retrieval_query": query, "reasoning": "First query used the customer's words."},
    )


def grades(*pairs: tuple[int, str]) -> tuple[str, dict[str, Any]]:
    return (
        "record_chunk_grades",
        {
            "grades": [
                {"label": label, "grade": grade, "reason": f"graded {grade}"}
                for label, grade in pairs
            ]
        },
    )


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


class TestIngestDocument:
    async def test_markdown_document_is_chunked_embedded_and_stored(self) -> None:
        store = InMemoryChunkStore()
        embedder = RecordingEmbedder()

        report = await ingest_document(
            PRICE_LIST.encode(),
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="md",
            filename="preise.md",
            embedder=embedder,
            store=store,
        )

        assert isinstance(report, IngestReport)
        assert report.status == "indexed"
        assert report.extraction_status == "ok"
        assert report.chunks_total >= 1
        assert report.chunks_stored == report.chunks_total
        assert report.chunks_duplicate == 0
        assert len(store.upserts) == 1
        business_id, document_id, chunks = store.upserts[0]
        assert business_id == BUSINESS
        assert document_id == DOCUMENT
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    async def test_every_stored_chunk_carries_its_own_hash_and_embedding(self) -> None:
        store = InMemoryChunkStore()
        embedder = RecordingEmbedder()

        await ingest_document(
            PRICE_LIST.encode(),
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="md",
            embedder=embedder,
            store=store,
        )

        _, _, chunks = store.upserts[0]
        for chunk in chunks:
            assert chunk.content_hash == content_hash(chunk.content)
            assert len(chunk.embedding) == embedder.dimensions

    async def test_stored_meta_says_where_the_chunk_came_from(self) -> None:
        """A retrieved chunk has to be citable, which means naming its source."""
        store = InMemoryChunkStore()

        await ingest_document(
            PRICE_LIST.encode(),
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="md",
            filename="preise.md",
            embedder=RecordingEmbedder(),
            store=store,
        )

        _, _, chunks = store.upserts[0]
        assert chunks[0].meta["filename"] == "preise.md"
        assert chunks[0].meta["kind"] == "md"
        assert chunks[0].meta["token_estimate"] > 0

    async def test_embeddings_are_requested_in_one_batch(self) -> None:
        """One call per document, not one per chunk: latency and rate limits."""
        store = InMemoryChunkStore()
        embedder = RecordingEmbedder()

        report = await ingest_document(
            (PRICE_LIST * 40).encode(),
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="md",
            embedder=embedder,
            store=store,
            target_tokens=64,
            overlap_tokens=8,
        )

        assert report.chunks_total > 3
        assert len(embedder.calls) == 1

    async def test_scanned_document_is_reported_no_text_and_stores_nothing(self) -> None:
        """The failure mode that looks like success. Nothing may be written."""
        store = InMemoryChunkStore()
        embedder = RecordingEmbedder()

        report = await ingest_document(
            b"   \n\n  ",
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="txt",
            filename="scan.txt",
            embedder=embedder,
            store=store,
        )

        assert report.status == "no_text"
        assert report.chunks_total == 0
        assert report.chunks_stored == 0
        assert report.note is not None
        assert "OCR" in report.note
        assert store.upserts == []
        assert embedder.calls == []

    async def test_undecodable_document_is_failed_not_no_text(self) -> None:
        store = InMemoryChunkStore()

        report = await ingest_document(
            b"\x81\x8d\x8f\x90\x9d",
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="txt",
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert report.status == "failed"
        assert report.note is not None
        assert store.upserts == []

    async def test_identical_chunks_are_not_embedded_twice(self) -> None:
        """The cheapest cost saving in the ingest path, so it gets a test."""
        store = InMemoryChunkStore()
        embedder = RecordingEmbedder()
        await ingest_document(
            PRICE_LIST.encode(),
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="md",
            embedder=embedder,
            store=store,
        )
        first_pass = len(embedder.embedded_texts)

        second = await ingest_document(
            PRICE_LIST.encode(),
            business_id=BUSINESS,
            document_id=uuid4(),
            kind="md",
            embedder=embedder,
            store=store,
        )

        assert first_pass > 0
        assert len(embedder.embedded_texts) == first_pass, "re-embedded known text"
        assert second.chunks_duplicate == second.chunks_total
        # CHANGED DELIBERATELY. This previously asserted chunks_stored == 0, which
        # was the duplicate-citation bug written down as an expectation: the text
        # then existed only under the FIRST document, so a retrieved passage cited
        # a file the reader never uploaded in that context. The saving that matters
        # is the embedding call, not the row -- so the row is stored and the
        # embedder is still not called again.
        assert second.chunks_stored == second.chunks_total
        # (no re-embedding assertion here: the embedder is shared across both
        # calls, so `len(...) == first_pass` above is what proves it.)
        assert second.note is not None
        assert "already indexed" in second.note

    async def test_partly_duplicate_document_embeds_only_the_new_chunks(self) -> None:
        """A re-upload with one paragraph added must only pay for that paragraph.

        The small target is deliberate: at the default 512 tokens this whole
        fixture is one chunk, so appending to it would change that single chunk's
        text and legitimately produce no duplicate at all. The interesting case --
        earlier chunks byte-identical, one new chunk at the end -- needs a document
        that is more than one chunk long.
        """
        store = InMemoryChunkStore()
        embedder = RecordingEmbedder()
        await ingest_document(
            PRICE_LIST.encode(),
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="md",
            embedder=embedder,
            store=store,
            target_tokens=32,
            overlap_tokens=0,
        )
        embedder.calls.clear()

        extended = PRICE_LIST + "\n\nNeu ab Maerz: Rohrreinigung ab 129 EUR pro Einsatz.\n"
        report = await ingest_document(
            extended.encode(),
            business_id=BUSINESS,
            document_id=uuid4(),
            kind="md",
            embedder=embedder,
            store=store,
            target_tokens=32,
            overlap_tokens=0,
        )

        assert report.chunks_duplicate > 0
        assert report.chunks_stored > 0
        # CHANGED DELIBERATELY alongside the fix above: stored now counts every
        # chunk, while only the NEW ones are embedded, so the gap between them is
        # exactly the duplicate count.
        assert len(embedder.embedded_texts) == report.chunks_stored - report.chunks_duplicate

    async def test_dedup_is_scoped_to_one_business(self) -> None:
        """One customer's uploads must never suppress another's embeddings."""
        store = InMemoryChunkStore()
        embedder = RecordingEmbedder()
        await ingest_document(
            PRICE_LIST.encode(),
            business_id=BUSINESS,
            document_id=DOCUMENT,
            kind="md",
            embedder=embedder,
            store=store,
        )
        embedder.calls.clear()

        report = await ingest_document(
            PRICE_LIST.encode(),
            business_id=OTHER_BUSINESS,
            document_id=uuid4(),
            kind="md",
            embedder=embedder,
            store=store,
        )

        assert report.chunks_duplicate == 0
        assert report.chunks_stored == report.chunks_total
        assert len(embedder.embedded_texts) == report.chunks_total

    async def test_missing_extractor_propagates_rather_than_marking_the_document(self) -> None:
        """A missing dependency is an operator problem, not a document problem.

        Recording it as `failed` would tell the customer their file is broken.
        """
        with pytest.raises(ExtractorUnavailableError):
            await ingest_document(
                b"%PDF-1.7",
                business_id=BUSINESS,
                document_id=DOCUMENT,
                kind="pdf",
                embedder=RecordingEmbedder(),
                store=InMemoryChunkStore(),
            )

    async def test_embedder_returning_the_wrong_count_stores_nothing(self) -> None:
        """Mismatched vectors would pair chunk 1's text with chunk 2's embedding.

        That is unfindable later: every search silently returns the wrong passage.
        """
        store = InMemoryChunkStore()

        with pytest.raises(EmbeddingCountMismatchError):
            await ingest_document(
                PRICE_LIST.encode(),
                business_id=BUSINESS,
                document_id=DOCUMENT,
                kind="md",
                embedder=BrokenEmbedder(),
                store=store,
            )

        assert store.upserts == []


# --------------------------------------------------------------------------- #
# The agentic retrieval loop
# --------------------------------------------------------------------------- #


class TestRetrieveNotNeeded:
    async def test_no_business_facts_needed_means_no_search_at_all(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Der Notdienst kostet 149 EUR."])
        embedder = RecordingEmbedder()
        provider = ScriptedProvider([decision(needed=False)])

        trace = await retrieve(
            "Write a friendly closing paragraph.",
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=embedder,
            store=store,
        )

        assert trace.outcome == "not_needed"
        assert trace.needed is False
        assert trace.attempts == []
        assert trace.attempt_count == 0
        assert trace.chunks == []
        assert embedder.calls == [], "embedded a query it had decided not to ask"
        assert store.searches == []


class TestRetrieveFirstTry:
    async def test_relevant_on_the_first_attempt_is_sufficient(self) -> None:
        store = InMemoryChunkStore()
        rows = store.seed(
            BUSINESS,
            [
                "Der Notdienst kostet am Wochenende 149 EUR Anfahrt.",
                "Unsere Wartung kostet 89 EUR.",
            ],
        )
        provider = ScriptedProvider(
            [decision(needed=True), grades((1, "relevant"), (2, "irrelevant"))]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
            limit=2,
        )

        assert isinstance(trace, RetrievalTrace)
        assert trace.outcome == "sufficient"
        assert trace.attempt_count == 1
        assert trace.attempts[0].decision == "sufficient"
        assert [chunk.chunk_id for chunk in trace.chunks] == [rows[0].chunk_id]
        assert trace.grounding_chunk_ids == [rows[0].chunk_id]

    async def test_the_embedded_query_is_the_rewrite_not_the_users_words(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Notdienst 149 EUR."])
        embedder = RecordingEmbedder()
        provider = ScriptedProvider(
            [
                decision(needed=True, query="notdienst wochenende anfahrt preis"),
                grades((1, "relevant")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=embedder,
            store=store,
        )

        assert embedder.embedded_texts == ["notdienst wochenende anfahrt preis"]
        assert trace.attempts[0].query == "notdienst wochenende anfahrt preis"
        assert trace.question == QUESTION

    async def test_grading_is_structured_tool_output_not_prose(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Notdienst 149 EUR."])
        provider = ScriptedProvider([decision(needed=True), grades((1, "relevant"))])

        await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert provider.offered_tools == [
            ("record_retrieval_decision",),
            ("record_chunk_grades",),
        ]
        item_schema = GRADE_TOOL.parameters["properties"]["grades"]["items"]
        assert item_schema["properties"]["grade"]["enum"] == ["relevant", "partial", "irrelevant"]

    async def test_the_grader_is_shown_labelled_chunks_it_can_reference(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Notdienst 149 EUR am Wochenende."])
        provider = ScriptedProvider([decision(needed=True), grades((1, "relevant"))])

        await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        grading_prompt = provider.prompt_text(1)
        assert "[1]" in grading_prompt
        assert "Notdienst 149 EUR am Wochenende." in grading_prompt


class TestRetrieveRewritesAndRetries:
    async def test_irrelevant_first_then_a_different_query_succeeds(self) -> None:
        store = InMemoryChunkStore()
        rows = store.seed(
            BUSINESS,
            [
                "Wir sind ein Familienbetrieb seit 1987.",
                "Unser Team besteht aus sechs Monteuren.",
                "Wir arbeiten in Koblenz und Umgebung.",
                "Notdiensteinsatz am Sonntag: 149 EUR Anfahrtspauschale.",
            ],
        )
        provider = ScriptedProvider(
            [
                decision(needed=True, query="notdienst preis"),
                grades((1, "irrelevant"), (2, "irrelevant")),
                rewrite("anfahrtspauschale sonntag notdiensteinsatz"),
                grades((1, "irrelevant"), (2, "irrelevant"), (3, "partial"), (4, "relevant")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
            limit=2,
        )

        assert trace.outcome == "sufficient"
        assert trace.attempt_count == 2
        assert trace.attempts[0].decision == "retry"
        assert trace.attempts[1].decision == "sufficient"
        assert trace.attempts[0].query != trace.attempts[1].query
        assert trace.attempts[1].query == "anfahrtspauschale sonntag notdiensteinsatz"
        assert trace.grounding_chunk_ids == [rows[3].chunk_id]

    async def test_the_retry_widens_the_search(self) -> None:
        """A rewrite alone would re-ask the same narrow window of the index."""
        store = InMemoryChunkStore()
        store.seed(BUSINESS, [f"Absatz {index}." for index in range(1, 7)])
        provider = ScriptedProvider(
            [
                decision(needed=True),
                grades((1, "irrelevant"), (2, "irrelevant")),
                rewrite("andere formulierung"),
                grades((3, "relevant")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
            limit=2,
        )

        assert [limit for _, limit in store.searches] == [2, 4]
        assert trace.attempts[0].limit == 2
        assert trace.attempts[1].limit == 4

    async def test_partial_grades_alone_do_not_count_as_sufficient(self) -> None:
        """A partial match is a hint the query was close, not evidence to cite."""
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Wir haben einen Notdienst.", "Preise auf Anfrage."])
        provider = ScriptedProvider(
            [
                decision(needed=True),
                grades((1, "partial"), (2, "partial")),
                rewrite("zweite formulierung"),
                grades((1, "partial"), (2, "partial")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
            limit=2,
        )

        assert trace.outcome == "fallback_to_web"
        assert trace.attempts[0].partial == 2

    async def test_min_relevant_raises_the_bar(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Notdienst 149 EUR.", "Wartung 89 EUR."])
        provider = ScriptedProvider(
            [
                decision(needed=True),
                grades((1, "relevant"), (2, "irrelevant")),
                rewrite("zweite formulierung"),
                grades((1, "relevant"), (2, "relevant")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
            min_relevant=2,
        )

        assert trace.attempts[0].decision == "retry"
        assert trace.outcome == "sufficient"
        assert len(trace.grounding_chunk_ids) == 2


class TestRetrieveFallsBack:
    async def test_exhausted_attempts_signal_the_web_search(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Wir sind ein Familienbetrieb seit 1987."])
        provider = ScriptedProvider(
            [
                decision(needed=True),
                grades((1, "irrelevant")),
                rewrite("zweite formulierung"),
                grades((1, "irrelevant")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert trace.outcome == "fallback_to_web"
        assert trace.attempt_count == MAX_RETRIEVAL_ATTEMPTS
        assert trace.attempts[-1].decision == "exhausted"
        assert trace.grounding_chunk_ids == []
        assert trace.outcome_reason != ""

    async def test_an_empty_index_does_not_spend_a_grading_call(self) -> None:
        """Nothing to grade is not a judgement call, so it must not cost tokens."""
        store = InMemoryChunkStore()
        provider = ScriptedProvider([decision(needed=True), rewrite("zweite formulierung")])

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert trace.outcome == "fallback_to_web"
        assert trace.attempt_count == 2
        assert all(attempt.grades == [] for attempt in trace.attempts)
        assert provider.offered_tools == [
            ("record_retrieval_decision",),
            ("record_retrieval_rewrite",),
        ]

    async def test_the_service_never_searches_the_web_itself(self) -> None:
        """The boundary: it reports the need and lets the caller decide."""
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Irrelevanter Absatz."])
        provider = ScriptedProvider(
            [
                decision(needed=True),
                grades((1, "irrelevant")),
                rewrite("zweite formulierung"),
                grades((1, "irrelevant")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert trace.outcome == "fallback_to_web"
        assert "web" in trace.outcome_reason.lower()

    async def test_more_than_two_attempts_cannot_be_requested(self) -> None:
        with pytest.raises(ValueError):
            await retrieve(
                QUESTION,
                business_id=BUSINESS,
                router=cheap_tier_router(ScriptedProvider([])),
                embedder=RecordingEmbedder(),
                store=InMemoryChunkStore(),
                max_attempts=MAX_RETRIEVAL_ATTEMPTS + 1,
            )


class TestRetrieveDegradesHonestly:
    async def test_a_grader_that_answers_in_prose_grounds_nothing(self) -> None:
        """The safe direction: never claim evidence the grader did not confirm."""
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Der Notdienst kostet 149 EUR."])
        provider = ScriptedProvider(
            [
                decision(needed=True),
                "Chunk one looks quite relevant to me, I would say.",
                rewrite("zweite formulierung"),
                "Still fairly relevant.",
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert trace.outcome == "fallback_to_web"
        assert [grade.grade for grade in trace.attempts[0].grades] == ["irrelevant"]
        assert any("structured" in note for note in trace.attempts[0].notes)

    async def test_a_decision_in_prose_falls_open_to_retrieving(self) -> None:
        """Failing to ground a claim is worse than one wasted cheap search."""
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Der Notdienst kostet 149 EUR."])
        provider = ScriptedProvider(["I think we should look it up.", grades((1, "relevant"))])

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert trace.needed is True
        assert trace.outcome == "sufficient"
        assert trace.attempts[0].query == QUESTION
        assert any("structured" in note for note in trace.notes)

    async def test_a_grade_for_a_chunk_that_was_never_offered_is_ignored(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Der Notdienst kostet 149 EUR."])
        provider = ScriptedProvider(
            [decision(needed=True), grades((1, "relevant"), (99, "relevant"))]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert len(trace.attempts[0].grades) == 1
        assert any("99" in note for note in trace.attempts[0].notes)

    async def test_the_real_fake_provider_terminates_at_the_fallback(self) -> None:
        """With no credentials the whole loop must still end, and end honestly.

        FakeProvider fills the grading schema with an empty grade list, so nothing
        is confirmed relevant -- and the loop must fall back rather than spin.
        """
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Der Notdienst kostet 149 EUR."])

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(FakeProvider()),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert trace.outcome == "fallback_to_web"
        assert trace.attempt_count == MAX_RETRIEVAL_ATTEMPTS


class TestTraceIsRenderable:
    async def test_the_trace_holds_everything_a_ui_needs_to_show_the_loop(self) -> None:
        store = InMemoryChunkStore()
        rows = store.seed(
            BUSINESS,
            ["Familienbetrieb seit 1987.", "Notdienst am Sonntag: 149 EUR."],
        )
        provider = ScriptedProvider(
            [
                decision(needed=True, query="notdienst preis"),
                grades((1, "irrelevant")),
                rewrite("notdienst sonntag anfahrt"),
                grades((1, "irrelevant"), (2, "relevant")),
            ]
        )

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
            limit=1,
        )

        rendered = trace.model_dump(mode="json")
        assert rendered["question"] == QUESTION
        assert rendered["outcome"] == "sufficient"
        assert rendered["attempt_count"] == 2
        assert rendered["prompt_version"] == RETRIEVAL_PROMPT_VERSION
        # Every rewritten query is on the record, in order.
        assert [attempt["query"] for attempt in rendered["attempts"]] == [
            "notdienst preis",
            "notdienst sonntag anfahrt",
        ]
        # Every graded chunk carries its id, its grade and a reason.
        second = rendered["attempts"][1]
        assert {grade["grade"] for grade in second["grades"]} == {"irrelevant", "relevant"}
        for grade in second["grades"]:
            assert UUID(grade["chunk_id"])
            assert grade["reason"]
            assert grade["excerpt"]
        # And the decision at each turn, with a reason a human can read.
        assert [attempt["decision"] for attempt in rendered["attempts"]] == [
            "retry",
            "sufficient",
        ]
        assert all(attempt["decision_reason"] for attempt in rendered["attempts"])
        assert rendered["grounding_chunk_ids"] == [str(rows[1].chunk_id)]

    async def test_the_trace_reports_what_the_loop_cost(self) -> None:
        store = InMemoryChunkStore()
        store.seed(BUSINESS, ["Notdienst 149 EUR."])
        provider = ScriptedProvider([decision(needed=True), grades((1, "relevant"))])

        trace = await retrieve(
            QUESTION,
            business_id=BUSINESS,
            router=cheap_tier_router(provider),
            embedder=RecordingEmbedder(),
            store=store,
        )

        assert trace.model_calls == 2
        assert trace.cost_usd == Decimal("0.0002")


# --------------------------------------------------------------------------- #
# Duplicate text must still be stored, or a citation names the wrong document
# --------------------------------------------------------------------------- #


class RecordingStore:
    """A ChunkStore that records exactly what upsert was asked to write.

    The bug this catches is invisible at the adapter level: the adapter can copy a
    vector by hash perfectly, and still never be asked to, because the service
    filtered the duplicate out first.
    """

    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known or set()
        self.written: list[tuple[UUID, list[StoredChunk]]] = []

    async def upsert(
        self, business_id: UUID, document_id: UUID, chunks: Sequence[StoredChunk]
    ) -> int:
        self.written.append((document_id, list(chunks)))
        return len(chunks)

    async def search(
        self, business_id: UUID, embedding: Sequence[float], *, limit: int
    ) -> list[RetrievedChunk]:
        return []

    async def existing_hashes(self, business_id: UUID, hashes: Sequence[str]) -> set[str]:
        return {h for h in hashes if h in self.known}


class CountingEmbedder:
    """Counts how many texts were actually embedded, so the cost saving is proven."""

    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [[float(len(t) % 7)] * 4 for t in texts]


async def test_a_wholly_duplicate_document_still_gets_its_own_rows() -> None:
    """Otherwise the text is only searchable through the FIRST document that had
    it, and a citation names a file the reader never uploaded to that context."""
    business_id, doc_two = uuid4(), uuid4()
    text = "Unsere Anfahrtspauschale betraegt 39 Euro. " * 40

    first = await ingest_document(
        text.encode(),
        business_id=business_id,
        document_id=uuid4(),
        kind="txt",
        embedder=CountingEmbedder(),
        store=(store := RecordingStore()),
        filename="preise-2024.txt",
    )
    assert first.chunks_stored > 0

    # Second document, same paragraph: every hash is now known.
    store.known = {c.content_hash for _, chunks in store.written for c in chunks}
    embedder = CountingEmbedder()
    second = await ingest_document(
        text.encode(),
        business_id=business_id,
        document_id=doc_two,
        kind="txt",
        embedder=embedder,
        store=store,
        filename="preise-2025.txt",
    )

    assert second.status == "indexed"
    assert second.chunks_stored > 0, "a fully-duplicate document was stored as nothing"

    written_for_second = [c for doc, chunks in store.written if doc == doc_two for c in chunks]
    assert written_for_second, "upsert was never called for the second document"
    assert all(c.embedding == [] for c in written_for_second), (
        "a known chunk must be sent with an empty embedding so the adapter copies "
        "the existing vector -- not re-embedded"
    )
    assert embedder.embedded == [], "no duplicate text should be embedded again"


async def test_a_partly_duplicate_document_embeds_only_the_new_passages() -> None:
    """Mixed document: some passages already known, some new.

    `known` is set directly rather than by ingesting an overlapping document,
    because appending text shifts every chunk boundary after the join, so no hash
    would match and the test would prove nothing about the branch it targets.
    """
    business_id, doc_id = uuid4(), uuid4()
    text = " ".join(f"Absatz {i} ueber Sanitaer und Heizung in Koblenz." * 12 for i in range(6))

    # First pass with an empty store: learn what this document chunks into.
    probe = RecordingStore()
    await ingest_document(
        text.encode(),
        business_id=business_id,
        document_id=uuid4(),
        kind="txt",
        embedder=CountingEmbedder(),
        store=probe,
        filename="probe.txt",
    )
    all_hashes = [c.content_hash for _, chunks in probe.written for c in chunks]
    assert len(all_hashes) >= 2, "need at least two chunks for a partial-duplicate case"

    # Mark only the first as already known.
    store = RecordingStore(known={all_hashes[0]})
    embedder = CountingEmbedder()
    report = await ingest_document(
        text.encode(),
        business_id=business_id,
        document_id=doc_id,
        kind="txt",
        embedder=embedder,
        store=store,
        filename="mixed.txt",
    )

    written = [c for doc, chunks in store.written if doc == doc_id for c in chunks]
    copied = [c for c in written if c.embedding == []]
    embedded = [c for c in written if c.embedding != []]

    assert len(copied) == 1, "the known passage should be copied by hash, not re-embedded"
    assert embedded, "the new passages should be embedded"
    assert len(embedder.embedded) == len(embedded), "only the new passages were embedded"
    assert report.chunks_duplicate == len(copied)
    assert report.chunks_stored == len(written), "every passage is stored, duplicate or not"
