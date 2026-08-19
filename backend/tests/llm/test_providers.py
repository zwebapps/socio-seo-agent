"""The adapters, and the seam they exist to prove.

The centrepiece is `test_both_adapters_map_a_tool_call_onto_identical_fields`:
an OpenRouter-shaped response and an Anthropic-shaped response describing the
same exchange must produce the same `Completion`. An abstraction with one
implementation is an assertion; two is a proof.

**Why two different HTTP fakes.** `anthropic` is built on `httpx`, so `respx`
(which patches `httpx`) mocks it cleanly. `openai` v3 is built on **`httpx2`**, a
different package -- so respx does not see it *at all* and a "mocked" request
goes to the real network. That is not a hypothetical: an early version of this
file leaked a live request to openrouter.ai and got a 401 back. The OpenRouter
adapter is therefore driven through an injected `httpx2.MockTransport`, which is
hermetic by construction rather than by global patching: there is no socket to
escape through, and the recorded request list is a stronger assertion than a
route's call count.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import httpx
import httpx2
import pytest
import respx
from openai import AsyncOpenAI

from backend.app.llm.anthropic_provider import (
    MODELS_REJECTING_SAMPLING,
    AnthropicProvider,
    split_system,
)
from backend.app.llm.contract import (
    RETRYABLE_ERRORS,
    Completion,
    InvalidToolArgumentsError,
    Message,
    ModelUnavailableError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    Role,
    ToolCall,
    ToolSpec,
)
from backend.app.llm.fake_provider import CANNED_RESPONSES, FakeProvider
from backend.app.llm.openrouter_provider import OPENROUTER_BASE_URL, OpenRouterProvider

MESSAGES_URL: Final = "https://api.anthropic.com/v1/messages"

OPENROUTER_MODEL: Final = "openai/gpt-4.1"
ANTHROPIC_MODEL: Final = "claude-opus-5"

PROMPT: Final = [
    Message(role=Role.SYSTEM, content="You choose the biggest opportunity."),
    Message(role=Role.USER, content="43 pages crawled, 9 winnable keywords."),
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

# One logical exchange, described twice. Same tokens, same tool call, same id.
SHARED_TOOL_NAME: Final = "kb_search"
SHARED_CALL_ID: Final = "call_abc123"
SHARED_ARGUMENTS: Final[dict[str, Any]] = {"query": "notdienst klempner", "limit": 4}
SHARED_TOKENS_IN: Final = 412
SHARED_TOKENS_OUT: Final = 37
SHARED_TEXT: Final = "Nine winnable keywords converge on one answer page."


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip real credentials so the suite can never make a paid call."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# --------------------------------------------------------------------------- #
# Hermetic transport for the OpenRouter adapter (openai SDK -> httpx2)
# --------------------------------------------------------------------------- #


@dataclass
class OpenRouterStub:
    """An `OpenRouterProvider` wired to a transport that cannot reach a network.

    `requests` is the complete record of what the adapter tried to send, which
    makes "no HTTP request was made" a direct assertion rather than an inference.
    """

    provider: OpenRouterProvider
    requests: list[httpx2.Request] = field(default_factory=list)


def openrouter_stub(
    *,
    status: int = 200,
    body: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> OpenRouterStub:
    """Build an OpenRouter adapter backed by `httpx2.MockTransport`."""
    stub = OpenRouterStub(provider=OpenRouterProvider("unused"))

    def handler(request: httpx2.Request) -> httpx2.Response:
        stub.requests.append(request)
        if error is not None:
            raise error
        # A fresh Response per call: a Response body can only be read once.
        return httpx2.Response(status, json=body if body is not None else {})

    stub.provider = OpenRouterProvider(
        "test-key-not-real",
        client=AsyncOpenAI(
            api_key="test-key-not-real",
            base_url=OPENROUTER_BASE_URL,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        ),
    )
    return stub


def sent_body(stub: OpenRouterStub) -> dict[str, Any]:
    """The JSON body of the last request the adapter tried to send."""
    parsed: dict[str, Any] = json.loads(stub.requests[-1].content)
    return parsed


# --------------------------------------------------------------------------- #
# Response builders
# --------------------------------------------------------------------------- #


def openrouter_body(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """An OpenAI-compatible chat completion, as OpenRouter returns it."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": OPENROUTER_MODEL,
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
    """OpenAI-style usage."""
    return {
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
    }


def anthropic_body(
    *,
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """A Messages-API response, as the Anthropic API returns it."""
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": ANTHROPIC_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": SHARED_TOKENS_IN, "output_tokens": SHARED_TOKENS_OUT},
    }


# --------------------------------------------------------------------------- #
# THE SEAM: both vendors' shapes must land on the same Completion
# --------------------------------------------------------------------------- #


def normalise(completion: Completion) -> dict[str, Any]:
    """Everything that must match across vendors -- i.e. not identity or cost."""
    return {
        "text": completion.text,
        "tool_calls": [call.model_dump() for call in completion.tool_calls],
        "is_final": completion.is_final,
        "tokens_in": completion.usage.tokens_in,
        "tokens_out": completion.usage.tokens_out,
        "estimated": completion.usage.estimated,
    }


async def test_both_adapters_map_a_tool_call_onto_identical_fields(
    respx_mock: respx.MockRouter,
) -> None:
    """Same exchange, two wire formats, one `Completion`.

    OpenRouter carries tool arguments as a JSON *string* under
    `function.arguments`; Anthropic carries them as a real object under `input`.
    Both must arrive as the same parsed dict, or every downstream tool needs to
    know which vendor answered -- which is exactly what this module prevents.
    """
    stub = openrouter_stub(
        body=openrouter_body(
            content=None,
            tool_calls=[
                {
                    "id": SHARED_CALL_ID,
                    "type": "function",
                    "function": {
                        "name": SHARED_TOOL_NAME,
                        "arguments": json.dumps(SHARED_ARGUMENTS),
                    },
                }
            ],
            usage=usage_block(SHARED_TOKENS_IN, SHARED_TOKENS_OUT),
        )
    )
    respx_mock.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_body(
                content=[
                    {
                        "type": "tool_use",
                        "id": SHARED_CALL_ID,
                        "name": SHARED_TOOL_NAME,
                        "input": SHARED_ARGUMENTS,
                    }
                ],
                stop_reason="tool_use",
            ),
        )
    )

    via_openrouter = await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL, tools=[KB_SEARCH])
    via_anthropic = await AnthropicProvider("k").complete(
        PROMPT, model=ANTHROPIC_MODEL, tools=[KB_SEARCH]
    )

    expected_call = ToolCall(
        name=SHARED_TOOL_NAME, arguments=SHARED_ARGUMENTS, call_id=SHARED_CALL_ID
    )
    for completion in (via_openrouter, via_anthropic):
        assert completion.text is None
        assert completion.tool_calls == [expected_call]
        assert completion.is_final is False
        assert completion.usage.tokens_in == SHARED_TOKENS_IN
        assert completion.usage.tokens_out == SHARED_TOKENS_OUT
        assert completion.usage.estimated is False

    assert normalise(via_openrouter) == normalise(via_anthropic)

    # Cost and identity are the only things that legitimately differ: different
    # models, each priced from its own row in the table.
    assert via_openrouter.usage.usd != via_anthropic.usage.usd
    assert via_openrouter.usage.provider == "openrouter"
    assert via_anthropic.usage.provider == "anthropic"


async def test_both_adapters_map_a_text_answer_onto_identical_fields(
    respx_mock: respx.MockRouter,
) -> None:
    """The final-answer path, across the same seam.

    Anthropic returns text as a list of blocks that must be joined; OpenRouter
    returns one string. `is_final` must be True on both, since that is the node
    loop's only exit condition.
    """
    stub = openrouter_stub(
        body=openrouter_body(
            content=SHARED_TEXT, usage=usage_block(SHARED_TOKENS_IN, SHARED_TOKENS_OUT)
        )
    )
    respx_mock.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_body(
                # Split across two blocks on purpose: joining them is the work.
                content=[
                    {"type": "text", "text": "Nine winnable keywords converge "},
                    {"type": "text", "text": "on one answer page."},
                ]
            ),
        )
    )

    via_openrouter = await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL)
    via_anthropic = await AnthropicProvider("k").complete(PROMPT, model=ANTHROPIC_MODEL)

    assert normalise(via_openrouter) == normalise(via_anthropic)
    for completion in (via_openrouter, via_anthropic):
        assert completion.text == SHARED_TEXT
        assert completion.tool_calls == []
        assert completion.is_final is True


# --------------------------------------------------------------------------- #
# Request translation
# --------------------------------------------------------------------------- #


async def test_openrouter_renders_every_role_and_the_tool_schema() -> None:
    """System, user, assistant-with-tool-calls and tool all become own turns."""
    stub = openrouter_stub(body=openrouter_body(content="done", usage=usage_block(1, 1)))
    conversation = [
        Message(role=Role.SYSTEM, content="ROLE"),
        Message(role=Role.USER, content="facts"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(name="kb_search", arguments={"query": "x"}, call_id="c1")],
        ),
        Message(role=Role.TOOL, content="4 chunks", tool_call_id="c1"),
    ]

    await stub.provider.complete(conversation, model=OPENROUTER_MODEL, tools=[KB_SEARCH])

    sent = sent_body(stub)
    assert [message["role"] for message in sent["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    # Arguments go out as a JSON string, which is the wire contract.
    assert sent["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"query": "x"}'
    assert sent["messages"][3]["tool_call_id"] == "c1"
    assert sent["tools"][0]["function"]["name"] == "kb_search"
    assert sent["tools"][0]["function"]["parameters"]["required"] == ["query"]


def test_anthropic_lifts_system_prompts_out_of_the_message_list() -> None:
    """System is a parameter on the Messages API, not a role."""
    system, params = split_system(
        [
            Message(role=Role.SYSTEM, content="ROLE"),
            Message(role=Role.SYSTEM, content="BRAND"),
            Message(role=Role.USER, content="facts"),
        ]
    )

    assert system == "ROLE\n\nBRAND"
    assert [param["role"] for param in params] == ["user"]


def test_anthropic_coalesces_consecutive_tool_results_into_one_user_turn() -> None:
    """All results from one assistant turn must arrive in a single user message.

    Splitting them across several user messages is a documented way to train the
    model out of making parallel tool calls -- a silent capability regression
    that no error would ever surface.
    """
    _, params = split_system(
        [
            Message(role=Role.USER, content="facts"),
            Message(
                role=Role.ASSISTANT,
                content="Looking two things up.",
                tool_calls=[
                    ToolCall(name="kb_search", arguments={"query": "a"}, call_id="c1"),
                    ToolCall(name="kb_search", arguments={"query": "b"}, call_id="c2"),
                ],
            ),
            Message(role=Role.TOOL, content="result a", tool_call_id="c1"),
            Message(role=Role.TOOL, content="result b", tool_call_id="c2"),
        ]
    )

    assert [param["role"] for param in params] == ["user", "assistant", "user"]

    assistant_blocks = params[1]["content"]
    assert isinstance(assistant_blocks, list)
    assert [block["type"] for block in assistant_blocks] == ["text", "tool_use", "tool_use"]

    results = params[2]["content"]
    assert isinstance(results, list)
    assert [block["type"] for block in results] == ["tool_result", "tool_result"]
    assert [block["tool_use_id"] for block in results] == ["c1", "c2"]


@pytest.mark.parametrize("renderer", ["openrouter", "anthropic"])
def test_a_tool_result_without_an_id_is_refused_by_both_adapters(renderer: str) -> None:
    """An unmatched tool result is a caller bug; name it here, not in a 400."""
    from backend.app.llm.openrouter_provider import _to_message_params

    orphan = [Message(role=Role.TOOL, content="result", tool_call_id=None)]
    render: Callable[[list[Message]], object] = (
        split_system if renderer == "anthropic" else _to_message_params
    )
    with pytest.raises(ValueError, match="tool_call_id"):
        render(orphan)


# --------------------------------------------------------------------------- #
# Usage and pricing at the adapter boundary
# --------------------------------------------------------------------------- #


async def test_openrouter_prices_from_reported_tokens() -> None:
    stub = openrouter_stub(body=openrouter_body(content="ok", usage=usage_block(1_000, 2_000)))
    completion = await stub.provider.complete(PROMPT, model="openai/gpt-4.1-mini")
    # 1000 * 0.40/1e6 + 2000 * 1.60/1e6
    assert completion.usage.usd == Decimal("0.0036")


async def test_a_missing_usage_block_is_estimated_and_flagged() -> None:
    """Booking a call as free would quietly under-count the whole run.

    OpenRouter sometimes omits `usage`. The adapter substitutes a conservative
    estimate and sets `estimated`, so the ledger row is visibly approximate
    instead of invisibly wrong.
    """
    stub = openrouter_stub(body=openrouter_body(content="a" * 30))
    completion = await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL)

    assert completion.usage.estimated is True
    assert completion.usage.tokens_out == 10  # 30 chars / 3
    assert completion.usage.usd > Decimal(0)


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, ProviderRateLimitError),
        (500, ProviderServerError),
        (503, ProviderServerError),
        (529, ProviderServerError),
        (400, ProviderRequestError),
        (401, ProviderRequestError),
        # 403/404 stay ProviderRequestError by type -- ModelUnavailableError is a
        # subclass -- but the retry policy keys on that subclass, asserted below.
        (403, ModelUnavailableError),
        (404, ModelUnavailableError),
    ],
)
async def test_openrouter_maps_http_status_to_a_typed_error(
    status: int, expected: type[Exception]
) -> None:
    """Retryable (429, 5xx, 403/404) and not (other 4xx) must be distinguishable."""
    stub = openrouter_stub(status=status, body={"error": {"message": "nope"}})
    with pytest.raises(expected):
        await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL)


@pytest.mark.parametrize("status", [401, 402])
async def test_openrouter_account_failures_are_not_model_unavailable(status: int) -> None:
    """The boundary of the retryable set, pinned from the other side.

    A bad key or an empty balance must NOT be mistaken for "try another model":
    every entry would fail the same way, and the operator needs the real reason.
    """
    stub = openrouter_stub(status=status, body={"error": {"message": "nope"}})
    with pytest.raises(ProviderRequestError) as caught:
        await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL)
    assert not isinstance(caught.value, ModelUnavailableError)


async def test_the_retryable_set_contains_model_unavailable_but_not_its_parent() -> None:
    """`except RETRYABLE_ERRORS` matches listed classes, so the grain matters."""
    assert ModelUnavailableError in RETRYABLE_ERRORS
    assert ProviderRequestError not in RETRYABLE_ERRORS


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, ProviderRateLimitError),
        (500, ProviderServerError),
        (529, ProviderServerError),
        (400, ProviderRequestError),
    ],
)
async def test_anthropic_maps_http_status_to_the_same_typed_errors(
    respx_mock: respx.MockRouter, status: int, expected: type[Exception]
) -> None:
    """The second half of the seam: identical error taxonomy from a different SDK."""
    respx_mock.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "nope"}}
        )
    )
    with pytest.raises(expected):
        await AnthropicProvider("k").complete(PROMPT, model=ANTHROPIC_MODEL)


async def test_a_transport_timeout_becomes_a_provider_timeout() -> None:
    stub = openrouter_stub(error=httpx2.ReadTimeout("too slow"))
    with pytest.raises(ProviderTimeoutError):
        await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL)


async def test_a_connection_failure_is_retryable_not_a_bad_request() -> None:
    """A socket that never connected says nothing about our request."""
    stub = openrouter_stub(error=httpx2.ConnectError("refused"))
    with pytest.raises(ProviderServerError):
        await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL)


async def test_unparseable_tool_arguments_raise_at_the_adapter_boundary() -> None:
    """The tool never sees malformed input, so no tool needs defensive parsing."""
    stub = openrouter_stub(
        body=openrouter_body(
            content=None,
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "kb_search", "arguments": "{not json"},
                }
            ],
            usage=usage_block(1, 1),
        )
    )
    with pytest.raises(InvalidToolArgumentsError) as caught:
        await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL, tools=[KB_SEARCH])
    assert caught.value.tool_name == "kb_search"


async def test_empty_tool_arguments_are_an_empty_dict_not_an_error() -> None:
    """A no-argument tool legitimately serialises as "" on the wire."""
    stub = openrouter_stub(
        body=openrouter_body(
            content=None,
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "kb_search", "arguments": ""},
                }
            ],
            usage=usage_block(1, 1),
        )
    )
    completion = await stub.provider.complete(PROMPT, model=OPENROUTER_MODEL, tools=[KB_SEARCH])
    assert completion.tool_calls[0].arguments == {}


async def test_anthropic_refuses_temperature_on_models_that_reject_it(
    respx_mock: respx.MockRouter,
) -> None:
    """These models 400 on `temperature`; fail locally and say what to do.

    Silently dropping it would change a GENERATE call's character with nobody
    told, which is the failure mode this codebase is built to avoid.
    """
    route = respx_mock.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=anthropic_body(content=[]))
    )
    assert ANTHROPIC_MODEL in MODELS_REJECTING_SAMPLING

    with pytest.raises(ProviderRequestError) as caught:
        await AnthropicProvider("k").complete(PROMPT, model=ANTHROPIC_MODEL, temperature=0.2)

    assert route.call_count == 0
    assert "temperature" in str(caught.value)


async def test_anthropic_accepts_temperature_on_models_that_allow_it(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, json=anthropic_body(content=[{"type": "text", "text": "ok"}])
        )
    )
    assert "claude-haiku-4-5" not in MODELS_REJECTING_SAMPLING

    await AnthropicProvider("k").complete(PROMPT, model="claude-haiku-4-5", temperature=0.2)

    sent: dict[str, Any] = json.loads(route.calls.last.request.content)
    assert sent["temperature"] == 0.2
    assert sent["system"] == "You choose the biggest opportunity."


async def test_anthropic_survives_a_refusal_with_no_content(
    respx_mock: respx.MockRouter,
) -> None:
    """A refusal is HTTP 200 with an empty content array. `text` is None."""
    respx_mock.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=anthropic_body(content=[], stop_reason="refusal"))
    )
    completion = await AnthropicProvider("k").complete(PROMPT, model=ANTHROPIC_MODEL)
    assert completion.text is None
    assert completion.tool_calls == []
    assert completion.is_final is True


async def test_anthropic_skips_thinking_blocks(respx_mock: respx.MockRouter) -> None:
    """Thinking blocks are not the answer and must not be flattened into it."""
    respx_mock.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_body(
                content=[
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": "the answer"},
                ]
            ),
        )
    )
    completion = await AnthropicProvider("k").complete(PROMPT, model=ANTHROPIC_MODEL)
    assert completion.text == "the answer"


# --------------------------------------------------------------------------- #
# The fake provider
# --------------------------------------------------------------------------- #


async def test_fake_provider_is_deterministic() -> None:
    """Same messages in, same completion out -- usage and latency included."""
    provider = FakeProvider()

    first = await provider.complete(PROMPT, model="fake/strong")
    second = await provider.complete(PROMPT, model="fake/strong")
    third = await FakeProvider().complete(PROMPT, model="fake/strong")

    assert first.model_dump() == second.model_dump()
    assert first.model_dump() == third.model_dump()
    assert first.text in CANNED_RESPONSES
    assert first.usage.latency_ms == 0  # measured latency would break determinism


async def test_fake_provider_varies_with_its_input() -> None:
    """Deterministic must not mean constant, or it tests nothing."""
    provider = FakeProvider(responses=["A", "B", "C", "D", "E", "F", "G", "H"])
    texts = {
        (
            await provider.complete(
                [Message(role=Role.USER, content=f"prompt {index}")], model="fake/cheap"
            )
        ).text
        for index in range(8)
    }
    assert len(texts) > 1


async def test_fake_provider_returns_a_tool_call_when_tools_are_offered() -> None:
    """The node loop cannot be tested without a model that asks for a tool."""
    completion = await FakeProvider().complete(PROMPT, model="fake/mid", tools=[KB_SEARCH])

    assert completion.is_final is False
    assert completion.text is None
    assert len(completion.tool_calls) == 1

    call = completion.tool_calls[0]
    assert call.name == "kb_search"
    # Every required property is present, so the tool's own schema validation
    # passes -- a fake that produces invalid arguments tests nothing useful.
    assert set(call.arguments) == {"query"}
    assert isinstance(call.arguments["query"], str)
    assert call.call_id.startswith("fake_call_")


async def test_fake_provider_answers_once_the_tool_result_comes_back() -> None:
    """Otherwise every node loop under test would run to its turn limit."""
    provider = FakeProvider()
    asked = await provider.complete(PROMPT, model="fake/mid", tools=[KB_SEARCH])
    call = asked.tool_calls[0]

    answered = await provider.complete(
        [
            *PROMPT,
            Message(role=Role.ASSISTANT, tool_calls=[call]),
            Message(role=Role.TOOL, content="4 chunks", tool_call_id=call.call_id),
        ],
        model="fake/mid",
        tools=[KB_SEARCH],
    )

    assert answered.is_final is True
    assert answered.text is not None


async def test_fake_provider_uses_enum_and_typed_placeholders() -> None:
    """Placeholders satisfy the schema rather than being 'string' everywhere."""
    tool = ToolSpec(
        name="score",
        description="Score a draft.",
        parameters={
            "type": "object",
            "properties": {
                "surface": {"type": "string", "enum": ["google", "ai_answers"]},
                "count": {"type": "integer"},
                "strict": {"type": "boolean"},
            },
            "required": ["surface", "count", "strict"],
        },
    )
    completion = await FakeProvider().complete(PROMPT, model="fake/cheap", tools=[tool])

    assert completion.tool_calls[0].arguments == {
        "surface": "google",
        "count": 1,
        "strict": True,
    }


async def test_fake_provider_spends_nothing_under_its_own_models() -> None:
    """Zero here is the truth: no request left the process."""
    completion = await FakeProvider().complete(PROMPT, model="fake/strong")
    assert completion.usage.usd == Decimal("0")
    assert completion.usage.provider == "fake"


async def test_fake_provider_prices_a_real_model_through_the_real_table() -> None:
    """So a budget or ledger test exercises production arithmetic, not a stub."""
    completion = await FakeProvider().complete(PROMPT, model="openai/gpt-4.1-mini")
    assert completion.usage.usd > Decimal(0)


def test_fake_provider_rejects_an_empty_response_set() -> None:
    """An explicitly empty list is a caller mistake, not a request for defaults."""
    with pytest.raises(ValueError, match="canned response"):
        FakeProvider(responses=[])


def test_the_fake_provider_fills_an_array_argument_with_one_element() -> None:
    """An empty array is schema-valid and useless.

    Any tool whose payload is a list — the knowledge-base grader returns `grades: [...]`
    — was silently defeated by `[]`: every passage read as ungraded, retrieval always
    reported insufficient, and the RAG path could not be exercised without a paid key.
    The eval harness found it by scoring RAG-on and RAG-off identically.
    """
    from backend.app.llm.fake_provider import _placeholder_for

    grades = _placeholder_for(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "integer"},
                    "grade": {"type": "string", "enum": ["relevant", "partial", "irrelevant"]},
                },
            },
        },
        "seed",
    )

    assert isinstance(grades, list)
    assert len(grades) == 1, "an empty list defeats every list-shaped tool"
    assert grades[0]["grade"] == "relevant", "the first enum value, deterministically"


def test_a_scalar_array_still_gets_one_element() -> None:
    from backend.app.llm.fake_provider import _placeholder_for

    value = _placeholder_for({"type": "array", "items": {"type": "string"}}, "s")
    assert len(value) == 1 and isinstance(value[0], str)
