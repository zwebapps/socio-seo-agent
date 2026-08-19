"""Embedders: the ``Embedder`` port, for real and for tests.

``backend.app.services.kb_service`` declares a one-method port -- "turn these
texts into vectors, in this order" -- and never imports a vendor SDK. This module
is the other side of it.

Two implementations, and the second is not a lesser one:

* :class:`RouterEmbedder` asks the model router for the ``EMBED`` route, so the
  model id lives in ``llm/router.py`` with every other model id
  (docs/ARCHITECTURE.md section 8) rather than in a string here.
* :class:`FakeEmbedder` hashes text into a stable unit vector. It is what makes
  every other test in the repo hermetic, and it is what runs when no provider is
  configured -- the same policy the router applies to completions: missing
  credentials mean the fake, never a paid call and never a crash.

Three rules the real embedder holds to, each because the obvious alternative
corrupts the index rather than failing:

**One request per batch.** A forty-chunk PDF is one call. Per-chunk calls would
multiply latency and rate-limit exposure by the length of the document for no
benefit.

**Vectors are paired to texts by the response's ``index``, never by arrival
order.** The API returns an index per item precisely because order is not
guaranteed, and pairing chunk 1's text with chunk 3's vector produces a store
whose only symptom is confidently wrong retrieval.

**No fallback to a different embedding model.** The router falls back across
vendors for completions, and here that would be actively harmful: vectors from
two different models are not comparable, so a mid-ingest fallback would write
rows into an index they cannot be searched against. A failed embedding call is
raised. The chain for ``EMBED`` is one entry for exactly this reason.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from backend.app.llm.contract import (
    BudgetExceededError,
    BudgetState,
    LlmError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    TaskClass,
    Usage,
)
from backend.app.llm.pricing import compute_usd, conservative_token_estimate
from backend.app.llm.router import (
    OPENROUTER,
    OPENROUTER_KEY_ENV,
    ModelRouter,
    RouteEntry,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "EMBEDDING_PROVIDERS",
    "EmbeddingBatchMismatchError",
    "FakeEmbedder",
    "RouterEmbedder",
]

#: Width of the vectors this application stores. Must equal ``EMBEDDING_DIM`` in
#: ``backend/app/db/models.py`` -- ``kb_chunks.embedding`` is ``vector(1536)`` and
#: NOT NULL, so a drift is an insert failure in production. It is duplicated
#: rather than imported so that the model layer does not drag SQLAlchemy into
#: every process that only wants to call a model; ``tests/llm/test_embedder.py``
#: asserts the two agree.
DEFAULT_EMBEDDING_DIM: Final = 1536

#: Providers with an embeddings endpoint this module can drive. Anthropic has no
#: embeddings API, so an Anthropic-only deployment resolves to the fake -- which
#: `using_fake` reports rather than hiding.
EMBEDDING_PROVIDERS: Final[frozenset[str]] = frozenset({OPENROUTER})

#: Bytes of digest per dimension in the fake. Two gives 65,536 distinct values per
#: component, which is far finer than anything a test can distinguish.
_BYTES_PER_DIMENSION: Final = 2


class EmbeddingBatchMismatchError(LlmError):
    """The provider returned a different number of vectors than texts sent.

    Fatal rather than best-effort. Truncating or padding would pair text with
    somebody else's vector, and the resulting index has no symptom except wrong
    answers -- the same reasoning as ``kb_service.EmbeddingCountMismatchError``,
    enforced one layer earlier so a malformed response never reaches the service.
    """

    def __init__(self, *, model: str, expected: int, received: int) -> None:
        self.model = model
        self.expected = expected
        self.received = received
        super().__init__(
            f"{model} returned {received} vectors for {expected} texts. Nothing was "
            "embedded: pairing a chunk with another chunk's vector would make every "
            "later search silently return the wrong passage."
        )


# --------------------------------------------------------------------------- #
# The client seam
# --------------------------------------------------------------------------- #


class _EmbeddingsResource(Protocol):
    """The one method this module needs from an OpenAI-compatible client."""

    # `input` shadows a builtin, and must: it is the parameter name the
    # OpenAI-compatible embeddings endpoint requires.
    async def create(self, *, model: str, input: Sequence[str]) -> Any:  # noqa: A002
        ...


class _EmbeddingsClient(Protocol):
    """A client exposing an embeddings resource.

    Declared as a read-only PROPERTY rather than a mutable attribute. A mutable
    attribute on a Protocol must match invariantly, which means no test double can
    ever satisfy it -- its `embeddings` would have to be literally
    `_EmbeddingsResource` rather than something that merely implements it. A
    property is covariant, so any object with a compatible `.embeddings` fits,
    which is the whole point of having a seam here.
    """

    @property
    def embeddings(self) -> _EmbeddingsResource: ...


# --------------------------------------------------------------------------- #
# The fake
# --------------------------------------------------------------------------- #


class FakeEmbedder:
    """Deterministic vectors from a hash. No network, ever.

    Identical text yields an identical vector, so a test can assert that the
    chunk it searched for comes back nearest; different text yields a nearly
    orthogonal one, so a test can assert that an irrelevant chunk sorts last.
    Vectors are unit length, which makes cosine distance well behaved and keeps
    the numbers comparable with a real embedding model's output.
    """

    def __init__(self, *, dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        if dim < 1:
            raise ValueError(f"dim must be at least 1, got {dim}")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in the same order."""
        return [self.vector_for(text) for text in texts]

    def vector_for(self, text: str) -> list[float]:
        """The stable unit vector for one string.

        Digest material is stretched with a counter so the vector is as wide as
        the column, and every component depends on the whole text.
        """
        needed = self._dim * _BYTES_PER_DIMENSION
        material = bytearray()
        counter = 0
        while len(material) < needed:
            material += hashlib.sha256(f"{counter}|{text}".encode()).digest()
            counter += 1

        values = [
            int.from_bytes(material[index : index + _BYTES_PER_DIMENSION], "big") / 32767.5 - 1.0
            for index in range(0, needed, _BYTES_PER_DIMENSION)
        ]
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:  # pragma: no cover - unreachable for any real digest
            return [1.0] + [0.0] * (self._dim - 1)
        return [value / norm for value in values]


# --------------------------------------------------------------------------- #
# The real one
# --------------------------------------------------------------------------- #


class RouterEmbedder:
    """The ``Embedder`` port over the router's ``EMBED`` route.

    Constructed once and shared: the client is built lazily on first use, so a
    process that never embeds never imports the vendor SDK.
    """

    def __init__(
        self,
        *,
        router: ModelRouter | None = None,
        client: _EmbeddingsClient | None = None,
        budget: BudgetState | None = None,
        env: Mapping[str, str] | None = None,
        task: TaskClass = TaskClass.EMBED,
        fake: FakeEmbedder | None = None,
    ) -> None:
        self._router = router if router is not None else ModelRouter(env=env)
        self._task = task
        self._env = env if env is not None else os.environ
        self._budget = budget
        self._client = client
        self._fake = fake if fake is not None else FakeEmbedder()

        route = self._router.resolve(task)
        entry = _first_supported(route.chain)
        self._entry = entry
        self._using_fake = route.using_fake or entry is None

    @property
    def task(self) -> TaskClass:
        return self._task

    @property
    def using_fake(self) -> bool:
        """True when no provider with an embeddings endpoint is configured.

        Exposed rather than hidden: vectors from the fake are arithmetic over a
        hash, and a UI that reports "running on the fake provider" is the
        difference between a known limitation and an unexplained bad answer.
        """
        return self._using_fake

    @property
    def model(self) -> str:
        """The model id this embedder will use, from the routing table."""
        if self._entry is None:
            return "fake/embed"
        return self._entry.model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a whole batch in one request, preserving the caller's order."""
        batch = list(texts)
        if not batch:
            return []
        if self._using_fake or self._entry is None:
            return await self._fake.embed(batch)

        entry = self._entry
        if self._budget is not None:
            estimate = compute_usd(entry.model, _tokens_in(batch), 0)
            if not self._budget.can_afford(estimate):
                raise BudgetExceededError(
                    model=entry.model,
                    limit_usd=self._budget.limit_usd,
                    spent_usd=self._budget.spent_usd,
                    estimated_usd=estimate,
                )

        client = self._resolve_client(entry)
        try:
            response = await client.embeddings.create(model=entry.model, input=batch)
        except ProviderError:
            raise
        except Exception as exc:  # translated below, never swallowed
            raise _translate(entry, exc) from exc

        vectors = _ordered_vectors(response, model=entry.model, expected=len(batch))
        self._record(entry, response, batch)
        return vectors

    def _resolve_client(self, entry: RouteEntry) -> _EmbeddingsClient:
        """Build the OpenAI-compatible client on first use.

        Imported here rather than at module scope so that importing this module
        does not import a vendor SDK -- the same reason ``router.build_providers``
        imports its adapters locally.
        """
        if self._client is not None:
            return self._client

        from openai import AsyncOpenAI

        from backend.app.llm.openrouter_provider import (
            DEFAULT_TIMEOUT_S,
            OPENROUTER_BASE_URL,
        )

        key = self._env.get(OPENROUTER_KEY_ENV, "").strip()
        if not key:  # pragma: no cover - resolve() already routes to the fake
            raise ProviderRequestError(
                entry.provider,
                entry.model,
                f"{OPENROUTER_KEY_ENV} is not set, so no embedding client can be built",
            )

        self._client = cast(
            "_EmbeddingsClient",
            AsyncOpenAI(api_key=key, base_url=OPENROUTER_BASE_URL, timeout=DEFAULT_TIMEOUT_S),
        )
        return self._client

    def _record(self, entry: RouteEntry, response: Any, batch: Sequence[str]) -> None:
        """Charge the budget with the provider's own token count where it gave one.

        An embedding call has no output tokens, so cost is input-only. When the
        response carries no usage block the estimate is marked ``estimated`` in the
        same way the completion adapters mark theirs, rather than recording a
        silent zero that would make the ceiling unenforceable.
        """
        if self._budget is None:
            return

        reported = getattr(getattr(response, "usage", None), "prompt_tokens", None)
        tokens_in = int(reported) if isinstance(reported, int) else _tokens_in(batch)
        self._budget.record(
            Usage(
                provider=entry.provider,
                model=entry.model,
                tokens_in=tokens_in,
                tokens_out=0,
                usd=compute_usd(entry.model, tokens_in, 0),
                latency_ms=0,
                estimated=not isinstance(reported, int),
            )
        )


def _first_supported(chain: Sequence[RouteEntry]) -> RouteEntry | None:
    """The first route entry whose provider actually has an embeddings endpoint."""
    for entry in chain:
        if entry.provider in EMBEDDING_PROVIDERS:
            return entry
    return None


def _tokens_in(batch: Sequence[str]) -> int:
    return sum(conservative_token_estimate(text) for text in batch)


def _ordered_vectors(response: Any, *, model: str, expected: int) -> list[list[float]]:
    """Pull the vectors out in the caller's order, or refuse the response."""
    data = list(getattr(response, "data", []) or [])
    if len(data) != expected:
        raise EmbeddingBatchMismatchError(model=model, expected=expected, received=len(data))

    ordered: list[list[float] | None] = [None] * expected
    for position, item in enumerate(data):
        index = getattr(item, "index", position)
        slot = index if isinstance(index, int) and 0 <= index < expected else position
        ordered[slot] = [float(value) for value in item.embedding]

    if any(vector is None for vector in ordered):
        raise EmbeddingBatchMismatchError(
            model=model, expected=expected, received=sum(1 for v in ordered if v is not None)
        )
    return [vector for vector in ordered if vector is not None]


def _translate(entry: RouteEntry, exc: Exception) -> ProviderError:
    """Map a vendor exception onto this codebase's typed errors.

    Matched on shape rather than on class, so the SDK stays out of this module's
    import graph. The split is the same one the completion adapters make: a 429
    or a 5xx is the provider's problem, anything else is ours.
    """
    status = getattr(exc, "status_code", None)
    detail = f"{type(exc).__name__}: {exc}"

    if status == 429:
        return ProviderRateLimitError(entry.provider, entry.model, detail)
    if isinstance(status, int) and status >= 500:
        return ProviderServerError(entry.provider, entry.model, detail, status_code=status)
    if "timeout" in type(exc).__name__.lower():
        return ProviderTimeoutError(entry.provider, entry.model, detail)
    return ProviderRequestError(
        entry.provider,
        entry.model,
        detail,
        status_code=status if isinstance(status, int) else None,
    )


if TYPE_CHECKING:  # pragma: no cover - compile-time conformance checks
    from backend.app.services.kb_service import Embedder

    def _router_satisfies_port(embedder: RouterEmbedder) -> Embedder:
        return embedder

    def _fake_satisfies_port(embedder: FakeEmbedder) -> Embedder:
        return embedder
