"""Anthropic adapter, over the `anthropic` SDK's Messages API.

The second real adapter exists to *prove* the seam rather than assert it. Four
places where the Messages API genuinely differs from the OpenAI-compatible one,
and how each is reconciled onto the same `Completion`/`Usage`:

1. **System prompts are a separate parameter**, not a role. Every `SYSTEM`
   message is lifted out and joined into `system=`.
2. **Tool results are content blocks on a user turn**, not their own role. And
   *all* results from one assistant turn must arrive in a **single** user
   message -- splitting them teaches the model to stop making parallel calls --
   so consecutive `TOOL` messages are coalesced.
3. **`input` on a `tool_use` block is already a dict**, where the OpenAI shape
   carries a JSON string that must be parsed. Same `ToolCall.arguments` either
   way; one side just needs less work.
4. **`max_tokens` is required**, and current models **reject `temperature`**
   outright (400). See `MODELS_REJECTING_SAMPLING`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Final

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    RateLimitError,
    omit,
)
from anthropic.types import (
    Message as AnthropicMessage,
)
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUnionParam,
    ToolUseBlockParam,
)

from backend.app.llm.contract import (
    Completion,
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
from backend.app.llm.sampling import (
    MODELS_REJECTING_SAMPLING as _MODELS_REJECTING_SAMPLING,
)

#: Bounded wait, matching the OpenRouter adapter (docs/ARCHITECTURE.md section 9).
DEFAULT_TIMEOUT_S: Final = 60.0

#: The Messages API requires `max_tokens`; there is no "model decides" mode.
DEFAULT_MAX_TOKENS: Final = 2048

#: Models that return HTTP 400 if `temperature` (or `top_p`/`top_k`) is sent at
#: all. The parameter was removed on these, and prompting is the supported way
#: to steer them. `complete()` refuses such a request locally rather than
#: shipping a 400 -- and rather than silently dropping the parameter, which
#: would change a GENERATE call's character with nobody told.
#:
#: The set MOVED to `llm/sampling.py` and is re-exported here so this module's
#: readers and importers still find it under this name. It moved because the admin
#: sampling screen needs the same fact in order to warn an operator BEFORE they save
#: a temperature onto a route that cannot accept one -- and reading that fact must not
#: cost an `import anthropic`, which is the whole reason every SDK import in this
#: package is either lazy or confined to an adapter.
MODELS_REJECTING_SAMPLING: Final[frozenset[str]] = _MODELS_REJECTING_SAMPLING


def _to_tool_param(tool: ToolSpec) -> ToolUnionParam:
    """Render a `ToolSpec` as a Messages-API tool definition."""
    param: ToolParam = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.parameters),
    }
    return param


def _assistant_blocks(
    message: Message,
) -> list[TextBlockParam | ToolUseBlockParam]:
    """Replay an assistant turn as text plus `tool_use` blocks."""
    blocks: list[TextBlockParam | ToolUseBlockParam] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return blocks


def split_system(
    messages: Sequence[Message],
) -> tuple[str | None, list[MessageParam]]:
    """Lift system prompts out, and translate the rest into `MessageParam`s.

    Consecutive `TOOL` messages are coalesced into one user turn carrying
    several `tool_result` blocks, because that is how the Messages API expresses
    "here are the results of the calls you just made" -- and because emitting
    one user turn per result is a documented way to suppress the model's
    parallel tool use.

    Exposed (not private) so the mapping can be tested directly: it is the part
    of this adapter most likely to break silently.
    """
    system_parts: list[str] = []
    params: list[MessageParam] = []
    pending_results: list[ToolResultBlockParam] = []

    def flush_results() -> None:
        if pending_results:
            params.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if message.role is Role.TOOL:
            if message.tool_call_id is None:
                raise ValueError(
                    "A TOOL-role message requires tool_call_id so the provider "
                    "can match the result to its request."
                )
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            )
            continue

        flush_results()

        # `Role.TOOL` is handled by the branch above, so it is deliberately
        # absent here -- mypy narrows it out and would flag a case for it as
        # unreachable.
        match message.role:
            case Role.SYSTEM:
                if message.content:
                    system_parts.append(message.content)
            case Role.USER:
                params.append({"role": "user", "content": message.content})
            case Role.ASSISTANT:
                params.append({"role": "assistant", "content": _assistant_blocks(message)})

    flush_results()
    return ("\n\n".join(system_parts) if system_parts else None), params


#: 4xx codes that mean "not this model", not "not this request".
_MODEL_ACCESS_STATUSES = frozenset({403, 404})


class AnthropicProvider:
    """Anthropic Messages API. Satisfies `contract.Provider`."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._client = client or AsyncAnthropic(
            api_key=api_key,
            timeout=timeout_s,
            max_retries=0,  # retry and fallback policy belongs to the router
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
        """Run one Messages call and normalise it onto `Completion`."""
        if temperature is not None and model in MODELS_REJECTING_SAMPLING:
            raise ProviderRequestError(
                self.name,
                model,
                f"model {model!r} rejects `temperature`; the parameter was "
                "removed on this model family. Steer it by prompt instead, and "
                "pass temperature=None on this route.",
                status_code=400,
            )

        system, message_params = split_system(messages)
        started = time.perf_counter()
        try:
            response = await self._client.messages.create(
                model=model,
                messages=message_params,
                max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
                system=system if system is not None else omit,
                tools=[_to_tool_param(tool) for tool in tools] if tools else omit,
                temperature=temperature if temperature is not None else omit,
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
        """Split a non-2xx three ways: retryable 5xx/529, retryable 403/404, fatal 4xx.

        403/404 mean *this model* is closed to this credential (an unreleased
        or un-entitled model id), which says nothing about the next entry in the
        chain, so they fall through as `ModelUnavailableError`. 401/402 stay
        fatal: a bad key or an empty balance fails every entry identically.
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

    def _to_completion(self, model: str, response: AnthropicMessage, latency_ms: int) -> Completion:
        """Map content blocks and usage onto the neutral `Completion`."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments: dict[str, Any] = dict(block.input)
                tool_calls.append(
                    ToolCall(
                        name=block.name,
                        arguments=arguments,
                        call_id=block.id,
                    )
                )
            # Thinking, server-tool and other block types carry no text or tool
            # request for a caller of this module, so they are skipped rather
            # than flattened into the answer.

        # A refusal is a content outcome, not an error: HTTP 200 with an empty
        # content array. `text` is None in that case, which is the honest
        # answer -- the model produced nothing to return.
        text = "".join(text_parts) if text_parts else None

        tokens_in, tokens_out, estimated = self._token_counts(response, text, tool_calls)
        usage = Usage(
            provider=self.name,
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
            # Computed the same way as the OpenRouter adapter, deliberately: a
            # tool request means not final, anything else means final. Equivalent
            # to `stop_reason == "tool_use"` and independent of new stop reasons
            # this adapter has not been taught about.
            is_final=not tool_calls,
        )

    def _token_counts(
        self,
        response: AnthropicMessage,
        text: str | None,
        tool_calls: Sequence[ToolCall],
    ) -> tuple[int, int, bool]:
        """Prefer the provider's counts; estimate only if they are absent.

        `usage` is always present on a Messages response, so the estimate branch
        is a belt-and-braces guard against a future shape change rather than an
        observed case -- but a zero here would book the call as free, so it is
        worth the four lines.
        """
        usage = response.usage
        tokens_in = usage.input_tokens
        tokens_out = usage.output_tokens
        if tokens_in or tokens_out:
            # Cache reads and writes are billed at different rates; folding them
            # into `tokens_in` at the standard rate would over-state a cached
            # call's cost. Left out until the ledger models cache tiers.
            return tokens_in, tokens_out, False

        rendered = text or ""
        for call in tool_calls:
            rendered += str(call.arguments)
        return 0, conservative_token_estimate(rendered), True
