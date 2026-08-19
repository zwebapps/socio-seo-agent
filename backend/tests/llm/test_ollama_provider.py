"""The Ollama adapter: the path that lets this project run with no paid API.

Two things here are not just "the OpenRouter tests again with a different URL",
and they are the reason the file exists:

* **A local model is unpriced, and that must not raise.** `compute_usd` refuses
  to invent a price, so an adapter that routed a local model through it would
  crash on every call. The cost tests use a model id that is deliberately absent
  from `PRICE_TABLE` -- if the adapter ever priced through the table, they fail
  with `UnknownModelPriceError` rather than passing quietly.
* **A small local model often ignores a `tools` payload and answers in prose.**
  That is not an adapter error and must not be dressed up as one; the test pins
  the honest mapping (`tool_calls=[]`, `is_final=True`) so the caller's own
  structured-output check stays the thing that notices.

**Hermetic by construction.** `openai` v3 sits on `httpx2`, which `respx` cannot
see at all, so a "mocked" request would go to the real network. The adapter is
therefore driven through an injected `httpx2.MockTransport` (the pattern in
`test_providers.py`), and `probe` -- which speaks plain `httpx` -- through an
injected `httpx.MockTransport`. There is no socket to escape through.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import httpx
import httpx2
import pytest
from openai import AsyncOpenAI

from backend.app.llm.contract import (
    Message,
    ModelUnavailableError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    Role,
    ToolSpec,
)
from backend.app.llm.ollama_provider import (
    DEFAULT_OLLAMA_BASE_URL,
    OllamaProvider,
    ollama_root,
    probe,
)
from backend.app.llm.pricing import is_priced

#: A model id that is deliberately NOT in `PRICE_TABLE`, because no local model
#: ever will be. Every cost assertion below leans on that.
LOCAL_MODEL: Final = "llama3.1:8b"

TAGS_URL: Final = "http://localhost:11434/api/tags"

PROMPT: Final = [
    Message(role=Role.SYSTEM, content="Classify the page intent."),
    Message(role=Role.USER, content="Emergency plumber, Koblenz, 24h call-out."),
]

KB_SEARCH: Final = ToolSpec(
    name="kb_search",
    description="Search the customer's indexed documents.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    },
)


def test_the_local_model_is_unpriced_which_is_what_makes_these_tests_meaningful() -> None:
    """Guard the premise: if this id were ever priced, the cost tests go quiet."""
    assert not is_priced(LOCAL_MODEL)


# --------------------------------------------------------------------------- #
# Hermetic transports
# --------------------------------------------------------------------------- #


@dataclass
class OllamaStub:
    """An `OllamaProvider` wired to a transport that cannot reach a network.

    `requests` is the full record of what the adapter tried to send, so "no
    request was made" is a direct assertion rather than an inference.
    """

    provider: OllamaProvider
    requests: list[httpx2.Request] = field(default_factory=list)


def ollama_stub(
    *,
    status: int = 200,
    body: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> OllamaStub:
    """Build an Ollama adapter backed by `httpx2.MockTransport`."""
    stub = OllamaStub(provider=OllamaProvider())

    def handler(request: httpx2.Request) -> httpx2.Response:
        stub.requests.append(request)
        if error is not None:
            raise error
        # A fresh Response per call: a Response body can only be read once.
        return httpx2.Response(status, json=body if body is not None else {})

    stub.provider = OllamaProvider(
        client=AsyncOpenAI(
            api_key="ollama-needs-no-key",
            base_url=DEFAULT_OLLAMA_BASE_URL,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        )
    )
    return stub


def sent_body(stub: OllamaStub) -> dict[str, Any]:
    """The JSON body of the last request the adapter tried to send."""
    parsed: dict[str, Any] = json.loads(stub.requests[-1].content)
    return parsed


@dataclass
class TagsStub:
    """An `httpx` client answering `/api/tags`, plus the URLs it was asked for."""

    client: httpx.AsyncClient
    urls: list[str] = field(default_factory=list)


def tags_stub(
    *,
    status: int = 200,
    body: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> TagsStub:
    """Build an injectable `httpx` client for the `/api/tags` probe."""
    stub = TagsStub(client=httpx.AsyncClient())

    def handler(request: httpx.Request) -> httpx.Response:
        stub.urls.append(str(request.url))
        if error is not None:
            raise error
        return httpx.Response(status, json=body if body is not None else {})

    stub.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return stub


def chat_body(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """An OpenAI-compatible chat completion, as Ollama's `/v1` surface returns it."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": LOCAL_MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": message,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def usage_block(tokens_in: int, tokens_out: int) -> dict[str, int]:
    """OpenAI-style usage, which Ollama's compatibility layer does report."""
    return {
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
    }


# --------------------------------------------------------------------------- #
# Construction: there is no credential to be missing
# --------------------------------------------------------------------------- #


def test_a_local_provider_is_constructible_with_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Availability is reachability here, not a key.

    The OpenAI SDK refuses to build a client with no `api_key` at all, so the
    adapter has to supply a placeholder. Forgetting that would make the no-key
    path -- the entire point of this provider -- raise at construction.

    `OPENAI_API_KEY` is stripped explicitly: with a developer's own key in the
    environment the SDK would pick it up and the assertion would pass for the
    wrong reason.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    provider = OllamaProvider()

    assert provider.name == "ollama"


# --------------------------------------------------------------------------- #
# The completion round trip
# --------------------------------------------------------------------------- #


async def test_a_local_completion_maps_onto_the_neutral_completion() -> None:
    stub = ollama_stub(body=chat_body(content="transactional", usage=usage_block(310, 4)))

    completion = await stub.provider.complete(PROMPT, model=LOCAL_MODEL)

    assert completion.text == "transactional"
    assert completion.tool_calls == []
    assert completion.is_final is True
    assert completion.usage.provider == "ollama"
    assert completion.usage.model == LOCAL_MODEL
    assert completion.usage.tokens_in == 310
    assert completion.usage.tokens_out == 4
    assert completion.usage.estimated is False
    assert completion.usage.latency_ms >= 0


async def test_the_request_carries_every_role_and_the_tool_schema() -> None:
    """The wire format is OpenAI's, so the translation must be the same one."""
    stub = ollama_stub(body=chat_body(content="ok", usage=usage_block(1, 1)))

    await stub.provider.complete(PROMPT, model=LOCAL_MODEL, tools=[KB_SEARCH], max_tokens=64)

    sent = sent_body(stub)
    assert sent["model"] == LOCAL_MODEL
    assert [message["role"] for message in sent["messages"]] == ["system", "user"]
    assert sent["tools"][0]["function"]["name"] == "kb_search"
    assert sent["tools"][0]["function"]["parameters"]["required"] == ["query"]
    assert sent["max_tokens"] == 64
    # The base URL is the local server's, not a hosted one.
    assert str(stub.requests[-1].url).startswith("http://localhost:11434/v1")


# --------------------------------------------------------------------------- #
# Cost: zero, and never through the price table
# --------------------------------------------------------------------------- #


async def test_a_local_call_costs_zero_without_a_price_lookup() -> None:
    """`compute_usd` raises on an unknown model, so this must not consult it.

    `LOCAL_MODEL` is absent from `PRICE_TABLE`. If the adapter priced through the
    table, this test would fail with `UnknownModelPriceError` instead of passing
    -- which is exactly the regression worth catching, since a crash here would
    take out the whole no-paid-API path.
    """
    stub = ollama_stub(body=chat_body(content="ok", usage=usage_block(1_000, 2_000)))

    completion = await stub.provider.complete(PROMPT, model=LOCAL_MODEL)

    assert completion.usage.usd == Decimal("0")
    assert completion.usage.tokens_in == 1_000
    assert completion.usage.tokens_out == 2_000


async def test_a_missing_usage_block_is_estimated_flagged_and_still_free() -> None:
    """Tokens are approximated when absent; the cost stays a true zero.

    `estimated` is about the token counts, not the money: the money is zero
    because no invoice exists, not because we could not measure it.
    """
    stub = ollama_stub(body=chat_body(content="a" * 30))

    completion = await stub.provider.complete(PROMPT, model=LOCAL_MODEL)

    assert completion.usage.estimated is True
    assert completion.usage.tokens_out == 10  # 30 chars / 3
    assert completion.usage.usd == Decimal("0")


# --------------------------------------------------------------------------- #
# Tool calling, which local models do inconsistently
# --------------------------------------------------------------------------- #


async def test_a_tool_call_from_a_local_model_is_parsed_like_any_other() -> None:
    stub = ollama_stub(
        body=chat_body(
            content=None,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "kb_search",
                        "arguments": json.dumps({"query": "notdienst klempner", "limit": 4}),
                    },
                }
            ],
            usage=usage_block(400, 20),
        )
    )

    completion = await stub.provider.complete(PROMPT, model=LOCAL_MODEL, tools=[KB_SEARCH])

    assert completion.is_final is False
    assert len(completion.tool_calls) == 1
    call = completion.tool_calls[0]
    assert call.name == "kb_search"
    assert call.arguments == {"query": "notdienst klempner", "limit": 4}
    assert call.call_id == "call_1"


async def test_prose_instead_of_a_tool_call_is_reported_as_a_final_answer() -> None:
    """Many gguf models ignore `tools` entirely and just talk.

    The adapter must not invent a tool call, and must not raise: it reports what
    the model actually did. The caller already treats "prose where a tool call
    was required" as a failure, and that is the right place for the judgement --
    an adapter that guessed would hide a real capability gap.
    """
    stub = ollama_stub(
        body=chat_body(
            content="I would search the knowledge base for notdienst klempner.",
            usage=usage_block(400, 14),
        )
    )

    completion = await stub.provider.complete(PROMPT, model=LOCAL_MODEL, tools=[KB_SEARCH])

    assert completion.tool_calls == []
    assert completion.is_final is True
    assert completion.text is not None


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


async def test_connection_refused_is_a_retryable_server_error() -> None:
    """The single most likely failure: the server simply is not running.

    Retryable, because the next entry in the chain (a hosted provider, or the
    fake) may well answer -- and because a refused socket says nothing about
    whether our request was valid.
    """
    stub = ollama_stub(error=httpx2.ConnectError("Connection refused"))

    with pytest.raises(ProviderServerError):
        await stub.provider.complete(PROMPT, model=LOCAL_MODEL)


async def test_a_slow_local_model_that_never_answers_is_a_timeout() -> None:
    stub = ollama_stub(error=httpx2.ReadTimeout("too slow"))

    with pytest.raises(ProviderTimeoutError):
        await stub.provider.complete(PROMPT, model=LOCAL_MODEL)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # Still a ProviderRequestError by type (ModelUnavailableError subclasses
        # it) but retryable: the next chain entry names a *different* model,
        # which may well be pulled on this host.
        (404, ModelUnavailableError),  # the model was never `ollama pull`ed
        (400, ProviderRequestError),
        (500, ProviderServerError),
    ],
)
async def test_http_status_maps_to_the_same_typed_errors_as_a_hosted_provider(
    status: int, expected: type[Exception]
) -> None:
    stub = ollama_stub(status=status, body={"error": {"message": "nope"}})

    with pytest.raises(expected):
        await stub.provider.complete(PROMPT, model=LOCAL_MODEL)


async def test_a_response_with_no_choices_is_a_server_error() -> None:
    stub = ollama_stub(body={"id": "x", "object": "chat.completion", "created": 1, "choices": []})

    with pytest.raises(ProviderServerError):
        await stub.provider.complete(PROMPT, model=LOCAL_MODEL)


# --------------------------------------------------------------------------- #
# probe: availability is reachability
# --------------------------------------------------------------------------- #


def test_the_tags_endpoint_hangs_off_the_root_not_the_openai_path() -> None:
    """`/api/tags` is Ollama's own API, one level above the `/v1` shim."""
    assert ollama_root(DEFAULT_OLLAMA_BASE_URL) == "http://localhost:11434"
    assert ollama_root("http://box.local:11434/v1/") == "http://box.local:11434"
    assert ollama_root("http://box.local:11434") == "http://box.local:11434"


async def test_probe_reports_a_reachable_server_and_the_models_it_has() -> None:
    stub = tags_stub(
        body={
            "models": [
                {"name": "llama3.1:8b", "size": 4_700_000_000},
                {"name": "qwen2.5:3b", "size": 1_900_000_000},
            ]
        }
    )

    status = await probe(DEFAULT_OLLAMA_BASE_URL, client=stub.client)

    assert status.reachable is True
    assert status.models == ("llama3.1:8b", "qwen2.5:3b")
    assert status.detail is None
    assert stub.urls == [TAGS_URL]


async def test_probe_reports_an_unreachable_server_instead_of_raising() -> None:
    """A caller asking "is Ollama available?" must not be handed an exception.

    Availability is a question, not an error condition -- and an admin screen
    that 500s because a local server is off is a bad screen.
    """
    stub = tags_stub(error=httpx.ConnectError("Connection refused"))

    status = await probe(DEFAULT_OLLAMA_BASE_URL, client=stub.client)

    assert status.reachable is False
    assert status.models == ()
    assert status.detail is not None
    assert "Connect" in status.detail


async def test_probe_treats_an_error_status_as_unreachable() -> None:
    """Something answered on the port, but it was not an Ollama that can serve us."""
    stub = tags_stub(status=404, body={"error": "not found"})

    status = await probe(DEFAULT_OLLAMA_BASE_URL, client=stub.client)

    assert status.reachable is False
    assert status.detail is not None
    assert "404" in status.detail


async def test_probe_survives_a_response_that_is_not_the_shape_we_expect() -> None:
    """A different service on port 11434 answers 200 with something else entirely."""
    stub = tags_stub(body={"unexpected": True})

    status = await probe(DEFAULT_OLLAMA_BASE_URL, client=stub.client)

    assert status.reachable is True
    assert status.models == ()
