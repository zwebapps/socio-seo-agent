"""OpenRouter adapter, over the `openai` SDK's chat-completions surface.

OpenRouter is OpenAI-compatible, so one SDK reaches many vendors' models behind
one integration and one fallback story (docs/ARCHITECTURE.md section 14).

The real work here is translation in both directions:

* out: `Message` -> role-specific `ChatCompletion*MessageParam`, `ToolSpec` ->
  a `function` tool with a JSON Schema;
* in: `choices[0].message` -> `Completion`, with `tool_calls[].function
  .arguments` parsed from its JSON *string* into a real dict;
* errors: the SDK's exception hierarchy -> this module's typed errors, split by
  whether falling back to another provider could plausibly help.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, Final

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
    omit,
)
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallUnionParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolUnionParam,
    ChatCompletionUserMessageParam,
)

from backend.app.llm.contract import (
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
    Usage,
)
from backend.app.llm.pricing import compute_usd, conservative_token_estimate

OPENROUTER_BASE_URL: Final = "https://openrouter.ai/api/v1"

#: Provider responses get a bounded wait (docs/ARCHITECTURE.md section 9:
#: "Provider responses -> size caps, timeouts, typed parsing"). A generation
#: node legitimately runs tens of seconds; a minute is the outer bound before
#: the router should be trying someone else.
DEFAULT_TIMEOUT_S: Final = 60.0

#: Bound output tokens per call when the caller does not. Section 8's cost
#: guidance lists "cap output tokens per node" as a first-order lever.
DEFAULT_MAX_TOKENS: Final = 2048


def _to_tool_param(tool: ToolSpec) -> ChatCompletionToolUnionParam:
    """Render a `ToolSpec` as an OpenAI-style function tool."""
    param: ChatCompletionFunctionToolParam = {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }
    return param


def _to_tool_call_params(
    calls: Sequence[ToolCall],
) -> list[ChatCompletionMessageToolCallUnionParam]:
    """Replay assistant tool calls in the wire shape the SDK expects."""
    params: list[ChatCompletionMessageToolCallUnionParam] = []
    for call in calls:
        param: ChatCompletionMessageFunctionToolCallParam = {
            "id": call.call_id,
            "type": "function",
            "function": {
                "name": call.name,
                # The wire format carries arguments as a JSON *string*, not an
                # object. Sorted keys keep a replayed prompt byte-stable, which
                # is what makes provider-side prompt caching hit.
                "arguments": json.dumps(call.arguments, sort_keys=True),
            },
        }
        params.append(param)
    return params


def _to_message_params(
    messages: Sequence[Message],
) -> list[ChatCompletionMessageParam]:
    """Translate neutral messages into the SDK's role-tagged params."""
    params: list[ChatCompletionMessageParam] = []
    for message in messages:
        match message.role:
            case Role.SYSTEM:
                system: ChatCompletionSystemMessageParam = {
                    "role": "system",
                    "content": message.content,
                }
                params.append(system)
            case Role.USER:
                user: ChatCompletionUserMessageParam = {
                    "role": "user",
                    "content": message.content,
                }
                params.append(user)
            case Role.ASSISTANT:
                assistant: ChatCompletionAssistantMessageParam = {"role": "assistant"}
                if message.content:
                    assistant["content"] = message.content
                if message.tool_calls:
                    assistant["tool_calls"] = _to_tool_call_params(message.tool_calls)
                params.append(assistant)
            case Role.TOOL:
                # A tool result with no id cannot be matched to its request, and
                # the provider rejects it. Fail here, with a message naming the
                # bug, rather than shipping a 400 upstream.
                if message.tool_call_id is None:
                    raise ValueError(
                        "A TOOL-role message requires tool_call_id so the "
                        "provider can match the result to its request."
                    )
                tool: ChatCompletionToolMessageParam = {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
                params.append(tool)
    return params


# --------------------------------------------------------------------------- #
# OpenAI-wire-format response parsing
#
# Module-level and provider-parameterised on purpose: this is the OpenAI wire format,
# not an OpenRouter detail, and Ollama speaks the same shape. Two copies of it would
# drift, and the way that drift shows up is a model that quietly stopped calling tools —
# no error, just structured-output nodes failing for no visible reason.
# --------------------------------------------------------------------------- #


def parse_tool_arguments(
    *, provider: str, model: str, tool_name: str, raw: str | None
) -> dict[str, Any]:
    """Turn the wire-format JSON string into a dict, or raise."""
    if raw is None or raw.strip() == "":
        return {}
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidToolArgumentsError(
            provider, model, tool_name, f"arguments were not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise InvalidToolArgumentsError(
            provider,
            model,
            tool_name,
            f"arguments must be a JSON object, got {type(parsed).__name__}",
        )
    return parsed


def parse_tool_calls(*, provider: str, model: str, raw: Sequence[Any] | None) -> list[ToolCall]:
    """Parse tool calls, narrowing the SDK's tool-call union explicitly.

    `tool_calls` is a union of function calls and (newer) custom calls. Only the
    function variant carries `.function.arguments`, so the variant is checked rather
    than assumed -- an unexpected shape is reported, not silently dropped, because a
    dropped tool call looks to the caller like a model that answered when it actually
    asked for something.
    """
    if not raw:
        return []

    calls: list[ToolCall] = []
    for entry in raw:
        call_type = getattr(entry, "type", None)
        if call_type != "function":
            raise InvalidToolArgumentsError(
                provider,
                model,
                str(getattr(entry, "id", "<unknown>")),
                f"unsupported tool-call type {call_type!r}; only 'function' is mapped",
            )
        function = entry.function
        name = str(function.name)
        arguments = parse_tool_arguments(
            provider=provider, model=model, tool_name=name, raw=function.arguments
        )
        calls.append(ToolCall(name=name, arguments=arguments, call_id=str(entry.id)))
    return calls


#: 4xx codes that mean "not this model", not "not this request".
_MODEL_ACCESS_STATUSES = frozenset({403, 404})


class OpenRouterProvider:
    """OpenRouter, via the OpenAI-compatible SDK. Satisfies `contract.Provider`."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: AsyncOpenAI | None = None,
        app_title: str = "Social Marketing Agent",
    ) -> None:
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
            max_retries=0,  # retry and fallback policy belongs to the router
            default_headers={"X-Title": app_title},
        )

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Run one chat completion and normalise it onto `Completion`."""
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=_to_message_params(messages),
                tools=[_to_tool_param(tool) for tool in tools] if tools else omit,
                temperature=temperature if temperature is not None else omit,
                max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
            )
        except RateLimitError as exc:  # 429 -- subclass of APIStatusError
            raise ProviderRateLimitError(self.name, model, f"rate limited: {exc}") from exc
        except APITimeoutError as exc:  # subclass of APIConnectionError
            raise ProviderTimeoutError(self.name, model, f"timed out: {exc}") from exc
        except APIConnectionError as exc:
            raise ProviderServerError(self.name, model, f"connection failed: {exc}") from exc
        except APIStatusError as exc:
            raise self._status_error(model, exc) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._to_completion(model, response, latency_ms)

    def _status_error(
        self, model: str, exc: APIStatusError
    ) -> ProviderServerError | ProviderRequestError:
        """Split a non-2xx three ways: retryable 5xx, retryable 403/404, fatal 4xx.

        403/404 mean *this model* is closed to this credential, which says
        nothing about the next model in the chain, so they are raised as
        `ModelUnavailableError` and fall through. 401/402 stay fatal: a bad key
        or an empty balance fails every entry identically.
        """
        status = exc.status_code
        if status >= 500:
            return ProviderServerError(
                self.name, model, f"server error {status}: {exc}", status_code=status
            )
        if status in _MODEL_ACCESS_STATUSES:
            return ModelUnavailableError(
                self.name, model, f"model unavailable ({status}): {exc}", status_code=status
            )
        return ProviderRequestError(
            self.name, model, f"request rejected ({status}): {exc}", status_code=status
        )

    def _to_completion(self, model: str, response: ChatCompletion, latency_ms: int) -> Completion:
        """Map the SDK response object onto the neutral `Completion`."""
        if not response.choices:
            raise ProviderServerError(self.name, model, "response contained no choices")

        message = response.choices[0].message
        tool_calls = parse_tool_calls(provider=self.name, model=model, raw=message.tool_calls)
        text = message.content

        tokens_in, tokens_out, estimated = self._token_counts(response, text, tool_calls)
        usage = Usage(
            provider=self.name,
            # Report the model the provider actually served, which OpenRouter
            # may normalise or re-route away from what we asked for. Pricing
            # follows the requested id, since that is the row we can price.
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd=compute_usd(model, tokens_in, tokens_out),
            latency_ms=latency_ms,
            estimated=estimated,
        )
        return Completion(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            is_final=not tool_calls,
        )

    def _token_counts(
        self, response: ChatCompletion, text: str | None, tool_calls: Sequence[ToolCall]
    ) -> tuple[int, int, bool]:
        """Prefer the provider's own counts; estimate only if it sent none.

        OpenRouter occasionally omits the usage block. Recording zero tokens
        there would book the call as free and quietly under-count the run, so
        the counts are estimated and flagged instead.
        """
        usage = response.usage
        if usage is not None:
            return usage.prompt_tokens, usage.completion_tokens, False

        rendered = text or ""
        for call in tool_calls:
            rendered += json.dumps(call.arguments, sort_keys=True)
        return 0, conservative_token_estimate(rendered), True
