"""The embedders. No database, no network, no vendor SDK -- in either direction.

``RouterEmbedder`` is exercised against a stub client rather than a mocked HTTP
layer, because the OpenAI SDK v3 is built on httpx2 and respx cannot intercept it
(see the note in pyproject.toml). A stub is also the honest shape here: what is
worth testing is the adapter's own rules -- one call per batch, order restored
from the response's ``index``, a count mismatch refused rather than zipped, no
credentials meaning the fake rather than a crash -- and none of those are HTTP
concerns.

``FakeEmbedder`` is not a convenience. It is what keeps every other test in the
repo hermetic, so its determinism is a contract with those tests and is asserted
here directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from backend.app.db.models import EMBEDDING_DIM
from backend.app.llm import (
    BudgetExceededError,
    BudgetState,
    ModelRouter,
    ModelTier,
    ProviderRequestError,
    RouteEntry,
    TaskClass,
)
from backend.app.llm.embedder import (
    DEFAULT_EMBEDDING_DIM,
    EmbeddingBatchMismatchError,
    FakeEmbedder,
    RouterEmbedder,
)

# --------------------------------------------------------------------------- #
# Stub client
# --------------------------------------------------------------------------- #


class StubEmbedding:
    def __init__(self, index: int, vector: list[float]) -> None:
        self.index = index
        self.embedding = vector


class StubResponse:
    def __init__(self, data: list[StubEmbedding], *, prompt_tokens: int = 12) -> None:
        self.data = data
        self.usage = type("Usage", (), {"prompt_tokens": prompt_tokens, "total_tokens": 12})()


class StubEmbeddings:
    """Records every call so "one call per batch" is provable, not assumed."""

    def __init__(self, response: StubResponse | Exception, *, width: int = EMBEDDING_DIM) -> None:
        self._response = response
        self._width = width
        self.calls: list[dict[str, Any]] = []

    async def create(self, *, model: str, input: Sequence[str], **extra: Any) -> StubResponse:
        self.calls.append({"model": model, "input": list(input), **extra})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class StubClient:
    def __init__(self, embeddings: StubEmbeddings) -> None:
        self.embeddings = embeddings


def vector(seed: float, width: int = EMBEDDING_DIM) -> list[float]:
    return [seed] * width


def stub_client(vectors: Sequence[list[float]], *, shuffled: bool = False) -> StubClient:
    indices = list(range(len(vectors)))
    if shuffled:
        indices.reverse()
    data = [StubEmbedding(index, vectors[index]) for index in indices]
    return StubClient(StubEmbeddings(StubResponse(data)))


def keyed_router() -> ModelRouter:
    """A router with a real-looking EMBED chain, so it does not fall back to fake."""
    return ModelRouter(
        providers={"openrouter": object()},  # type: ignore[dict-item]
        chains={ModelTier.EMBED: (RouteEntry("openrouter", "openai/text-embedding-3-small"),)},
    )


# --------------------------------------------------------------------------- #
# FakeEmbedder
# --------------------------------------------------------------------------- #


async def test_fake_embedder_is_deterministic_and_unit_length() -> None:
    embedder = FakeEmbedder()

    first = await embedder.embed(["Badsanierung ab 8.500 Euro."])
    second = await embedder.embed(["Badsanierung ab 8.500 Euro."])

    assert first == second
    assert len(first[0]) == DEFAULT_EMBEDDING_DIM
    norm = sum(value * value for value in first[0]) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-9)


async def test_fake_embedder_separates_different_texts() -> None:
    """Identical text must collide; different text must not.

    Both halves matter: a retrieval test that expects the nearest chunk to be the
    one whose text it searched for depends on the first, and a test that expects
    an *irrelevant* chunk to sort last depends on the second.
    """
    embedder = FakeEmbedder()

    vectors = await embedder.embed(
        ["Heizungswartung kostet 149 Euro.", "Wir sanieren Baeder in Koblenz."]
    )

    assert vectors[0] != vectors[1]
    cosine = sum(a * b for a, b in zip(vectors[0], vectors[1], strict=True))
    assert abs(cosine) < 0.5, "unrelated texts should not be near-parallel"


async def test_fake_embedder_returns_one_vector_per_text_in_order() -> None:
    embedder = FakeEmbedder()
    texts = ["eins", "zwei", "drei"]

    vectors = await embedder.embed(texts)

    assert len(vectors) == 3
    singles = [(await embedder.embed([text]))[0] for text in texts]
    assert vectors == singles


async def test_fake_embedder_accepts_an_empty_batch() -> None:
    assert await FakeEmbedder().embed([]) == []


def test_the_fake_width_matches_the_column_it_has_to_fit() -> None:
    """A drift here is a NOT NULL vector(1536) insert failing in production."""
    assert DEFAULT_EMBEDDING_DIM == EMBEDDING_DIM


# --------------------------------------------------------------------------- #
# RouterEmbedder
# --------------------------------------------------------------------------- #


async def test_router_embedder_sends_one_request_per_batch() -> None:
    """Not one per chunk. A 40-chunk PDF is one request, or ingest is 40x slower."""
    client = stub_client([vector(0.1), vector(0.2), vector(0.3)])
    embedder = RouterEmbedder(router=keyed_router(), client=client)

    vectors = await embedder.embed(["a", "b", "c"])

    assert len(client.embeddings.calls) == 1
    assert client.embeddings.calls[0]["input"] == ["a", "b", "c"]
    assert client.embeddings.calls[0]["model"] == "openai/text-embedding-3-small"
    assert [row[0] for row in vectors] == [0.1, 0.2, 0.3]


async def test_router_embedder_restores_the_response_order() -> None:
    """Vectors are paired to texts by ``index``, never by arrival order.

    The API is documented to return an ``index`` per item precisely because the
    order is not guaranteed. Trusting arrival order would pair chunk 1's text with
    chunk 3's vector -- a corruption whose only symptom is wrong answers.
    """
    client = stub_client([vector(0.1), vector(0.2), vector(0.3)], shuffled=True)
    embedder = RouterEmbedder(router=keyed_router(), client=client)

    vectors = await embedder.embed(["a", "b", "c"])

    assert [row[0] for row in vectors] == [0.1, 0.2, 0.3]


async def test_router_embedder_refuses_a_short_response() -> None:
    """Two vectors for three texts is never zipped into a plausible answer."""
    client = StubClient(StubEmbeddings(StubResponse([StubEmbedding(0, vector(0.1))])))
    embedder = RouterEmbedder(router=keyed_router(), client=client)

    with pytest.raises(EmbeddingBatchMismatchError) as caught:
        await embedder.embed(["a", "b"])

    assert "2" in str(caught.value)


async def test_router_embedder_skips_the_call_for_an_empty_batch() -> None:
    client = stub_client([])
    embedder = RouterEmbedder(router=keyed_router(), client=client)

    assert await embedder.embed([]) == []
    assert client.embeddings.calls == []


async def test_router_embedder_falls_back_to_the_fake_with_no_credentials() -> None:
    """No key means deterministic local vectors, never a crash and never a call.

    Same policy the router itself applies to completions: a deployment with no
    credentials degrades visibly instead of failing at the first ingest.
    """
    embedder = RouterEmbedder(router=ModelRouter(providers={}))

    vectors = await embedder.embed(["Badsanierung ab 8.500 Euro."])

    assert vectors == await FakeEmbedder().embed(["Badsanierung ab 8.500 Euro."])
    assert embedder.using_fake is True


async def test_router_embedder_reports_the_route_it_would_use() -> None:
    embedder = RouterEmbedder(router=keyed_router(), client=stub_client([vector(0.5)]))

    assert embedder.using_fake is False
    assert embedder.model == "openai/text-embedding-3-small"


async def test_router_embedder_charges_the_budget_it_was_given() -> None:
    budget = BudgetState(limit_usd=Decimal("0.50"))
    client = stub_client([vector(0.1), vector(0.2)])
    embedder = RouterEmbedder(router=keyed_router(), client=client, budget=budget)

    await embedder.embed(["a", "b"])

    assert budget.spent_usd > Decimal(0)


async def test_router_embedder_refuses_before_the_call_when_the_budget_is_gone() -> None:
    """Checked before the request, like every other call in this codebase.

    Checking afterwards is accounting, not control -- the tokens are already
    spent by then.
    """
    budget = BudgetState(limit_usd=Decimal("0.000000001"))
    client = stub_client([vector(0.1)])
    embedder = RouterEmbedder(router=keyed_router(), client=client, budget=budget)

    with pytest.raises(BudgetExceededError):
        await embedder.embed(["a"])

    assert client.embeddings.calls == []


async def test_router_embedder_translates_a_provider_failure() -> None:
    """A vendor exception must arrive as this codebase's typed error."""
    client = StubClient(StubEmbeddings(RuntimeError("boom")))
    embedder = RouterEmbedder(router=keyed_router(), client=client)

    with pytest.raises(ProviderRequestError) as caught:
        await embedder.embed(["a"])

    assert "openai/text-embedding-3-small" in str(caught.value)


async def test_router_embedder_uses_the_embed_task_class() -> None:
    """The model id lives in the routing table, never in this module."""
    router = keyed_router()

    assert RouterEmbedder(router=router, client=stub_client([])).task is TaskClass.EMBED
    assert router.resolve(TaskClass.EMBED).tier is ModelTier.EMBED
