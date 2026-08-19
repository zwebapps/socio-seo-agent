"""The model catalogue that feeds the admin model-picker.

The screen this serves has one hard requirement that shapes every test here:
**it must never fail and never lie.** So two properties get most of the
attention:

* **Degradation is total.** No key, no server, an error status, a malformed body
  -- every one of those returns `known_models(provider)` rather than raising. An
  admin screen that 500s because a local Ollama is switched off is a bad screen.
* **`priced` is honest.** A model absent from `PRICE_TABLE` cannot have its cost
  reported, and the screen must be able to say so. Showing "$0.00" for real spend
  is worse than showing nothing, so `priced` is asserted per provider rather than
  assumed, and priced entries sort first.

Hermetic: every HTTP surface is an injected `httpx.MockTransport`, so there is no
socket to escape through. `backend/tests/conftest.py` strips `OPENROUTER_API_KEY`
before the suite runs, which is why the configured-key cases pass `env={...}`
explicitly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import pytest

from backend.app.llm.catalogue import (
    ANTHROPIC,
    FAKE,
    OLLAMA,
    OPENROUTER,
    OPENROUTER_MODELS_URL,
    CatalogueModel,
    known_models,
    list_models,
)
from backend.app.llm.ollama_provider import DEFAULT_OLLAMA_BASE_URL

CONFIGURED: Final[Mapping[str, str]] = {"OPENROUTER_API_KEY": "test-key-not-real"}
TAGS_URL: Final = "http://localhost:11434/api/tags"

#: In `PRICE_TABLE`; must come back `priced=True`.
PRICED_ID: Final = "openai/gpt-4.1-mini"
#: Real OpenRouter slug, absent from `PRICE_TABLE`; must come back `priced=False`.
UNPRICED_ID: Final = "mistralai/mistral-small-3.2-24b-instruct"


# --------------------------------------------------------------------------- #
# Hermetic transport
# --------------------------------------------------------------------------- #


@dataclass
class HttpStub:
    """An injectable `httpx` client, plus what it was asked for."""

    client: httpx.AsyncClient
    requests: list[httpx.Request] = field(default_factory=list)


def http_stub(
    *,
    status: int = 200,
    body: Any = None,
    error: Exception | None = None,
) -> HttpStub:
    """Build an `httpx.AsyncClient` that cannot reach a network."""
    stub = HttpStub(client=httpx.AsyncClient())

    def handler(request: httpx.Request) -> httpx.Response:
        stub.requests.append(request)
        if error is not None:
            raise error
        return httpx.Response(status, json=body if body is not None else {})

    stub.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return stub


def openrouter_models_body(*model_ids: str) -> dict[str, Any]:
    """The shape `GET /api/v1/models` returns."""
    return {
        "data": [
            {
                "id": model_id,
                "name": f"Vendor: {model_id}",
                "context_length": 131_072,
            }
            for model_id in model_ids
        ]
    }


def ids(models: list[CatalogueModel]) -> list[str]:
    return [model.id for model in models]


# --------------------------------------------------------------------------- #
# known_models: derived from the price table, no I/O
# --------------------------------------------------------------------------- #


def test_known_models_partitions_the_price_table_by_provider() -> None:
    """One rule, applied to the table's own id conventions.

    A `fake/` prefix is the fake provider; any other slashed slug is an
    OpenRouter slug; a bare `claude-*` id is Anthropic first party. The three
    sets must not overlap, or a model would appear under two providers on the
    screen.
    """
    openrouter = set(ids(known_models(OPENROUTER)))
    anthropic = set(ids(known_models(ANTHROPIC)))
    fake = set(ids(known_models(FAKE)))

    assert PRICED_ID in openrouter
    assert "claude-opus-5" in anthropic
    assert "fake/cheap" in fake
    assert openrouter.isdisjoint(anthropic)
    assert openrouter.isdisjoint(fake)
    assert anthropic.isdisjoint(fake)


def test_every_known_model_is_priced_because_the_price_table_is_where_it_came_from() -> None:
    for provider in (OPENROUTER, ANTHROPIC, FAKE):
        assert all(model.priced for model in known_models(provider))
        assert all(model.provider == provider for model in known_models(provider))


def test_no_local_model_is_known_in_advance() -> None:
    """We cannot know what someone has pulled without asking the server.

    An empty list here is the honest answer, and it is why `list_models` for
    Ollama does I/O while Anthropic does not.
    """
    assert known_models(OLLAMA) == []


def test_an_unknown_provider_name_returns_nothing_rather_than_raising() -> None:
    assert known_models("not-a-provider") == []


# --------------------------------------------------------------------------- #
# openrouter
# --------------------------------------------------------------------------- #


async def test_openrouter_reads_the_live_list_and_flags_what_it_cannot_price() -> None:
    stub = http_stub(body=openrouter_models_body(PRICED_ID, UNPRICED_ID))

    models = await list_models(OPENROUTER, env=CONFIGURED, client=stub.client)

    assert ids(models) == [PRICED_ID, UNPRICED_ID]
    priced, unpriced = models
    assert priced.priced is True
    assert priced.note is None
    assert unpriced.priced is False
    # The screen has to be able to warn, so the reason is in the row itself.
    assert unpriced.note is not None
    assert "price table" in unpriced.note
    # Human-facing text comes from the vendor, never invented here.
    assert priced.label == f"Vendor: {PRICED_ID}"
    assert priced.context_tokens == 131_072

    request = stub.requests[-1]
    assert str(request.url) == OPENROUTER_MODELS_URL
    assert request.headers["authorization"] == "Bearer test-key-not-real"


async def test_openrouter_sorts_priced_models_first() -> None:
    """Cost-reportable models are the ones an admin should reach for first."""
    stub = http_stub(body=openrouter_models_body(UNPRICED_ID, PRICED_ID))

    models = await list_models(OPENROUTER, env=CONFIGURED, client=stub.client)

    assert ids(models) == [PRICED_ID, UNPRICED_ID]


async def test_openrouter_without_a_key_falls_back_to_the_price_table() -> None:
    """The list endpoint needs the key, but the screen still needs a list."""
    stub = http_stub(body=openrouter_models_body(UNPRICED_ID))

    models = await list_models(OPENROUTER, env={}, client=stub.client)

    assert models == known_models(OPENROUTER)
    assert stub.requests == []  # no key means no request was even attempted


@pytest.mark.parametrize(
    "make_stub",
    [
        lambda: http_stub(error=httpx.ConnectError("refused")),
        lambda: http_stub(error=httpx.ReadTimeout("too slow")),
        lambda: http_stub(status=500, body={"error": "boom"}),
        lambda: http_stub(status=401, body={"error": "bad key"}),
        lambda: http_stub(body={"unexpected": "shape"}),
    ],
    ids=["refused", "timeout", "5xx", "401", "malformed"],
)
async def test_any_openrouter_failure_degrades_to_the_price_table(
    make_stub: Callable[[], HttpStub],
) -> None:
    """Five different ways to fail, one behaviour: a usable screen."""
    stub = make_stub()
    models = await list_models(OPENROUTER, env=CONFIGURED, client=stub.client)

    assert models == known_models(OPENROUTER)


# --------------------------------------------------------------------------- #
# anthropic
# --------------------------------------------------------------------------- #


async def test_anthropic_is_derived_from_the_price_table_with_no_request() -> None:
    """There is no usable public list endpoint without a key, so we do not pretend."""
    stub = http_stub(body={"should": "not be read"})

    models = await list_models(ANTHROPIC, env=CONFIGURED, client=stub.client)

    assert models == known_models(ANTHROPIC)
    assert models != []
    assert stub.requests == []


# --------------------------------------------------------------------------- #
# ollama
# --------------------------------------------------------------------------- #


async def test_ollama_lists_installed_models_and_marks_them_unmetered() -> None:
    stub = http_stub(
        body={"models": [{"name": "qwen2.5:3b"}, {"name": "llama3.1:8b"}]},
    )

    models = await list_models(OLLAMA, base_url=DEFAULT_OLLAMA_BASE_URL, client=stub.client)

    assert ids(models) == ["llama3.1:8b", "qwen2.5:3b"]  # unpriced, so plain id order
    for model in models:
        assert model.provider == OLLAMA
        assert model.priced is False
        assert model.note == "local; cost is not metered"
        assert model.context_tokens is None
    # `/api/tags`, not the `/v1` chat path.
    assert str(stub.requests[-1].url) == TAGS_URL


async def test_an_unreachable_ollama_returns_an_empty_list_and_never_raises() -> None:
    """The screen renders "no local models found", not a stack trace."""
    stub = http_stub(error=httpx.ConnectError("Connection refused"))

    models = await list_models(OLLAMA, client=stub.client)

    assert models == []


# --------------------------------------------------------------------------- #
# fake
# --------------------------------------------------------------------------- #


async def test_the_fake_provider_is_never_empty() -> None:
    """With no credentials at all this is the only provider that answers.

    An empty picker there would read as "the app is broken" when the truth is
    "the app is running for free".
    """
    models = await list_models(FAKE, env={})

    assert models != []
    assert all(model.provider == FAKE for model in models)
    assert all(model.note is not None for model in models)


async def test_an_unknown_provider_lists_nothing_rather_than_raising() -> None:
    assert await list_models("not-a-provider", env={}) == []
