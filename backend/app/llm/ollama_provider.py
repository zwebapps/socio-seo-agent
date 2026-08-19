"""Ollama adapter: a model server on the user's own machine.

This is the provider that makes "run the whole product without a paid API" true
rather than aspirational. Ollama exposes an OpenAI-compatible endpoint at
`http://localhost:11434/v1`, so structurally this is the OpenRouter adapter
pointed somewhere else -- the request-side translation is literally reused from
it, because two copies of that translation would drift and nothing would notice.

Four things genuinely differ from a hosted provider, and each has a deliberate
answer here rather than an inherited one:

1. **There is no API key.** So the project's "a missing credential means the fake
   provider" rule does not apply: availability is a *reachability* question. Ask
   `probe()`, which answers in about two seconds and never raises -- a caller
   checking whether Ollama is up must not be made to wait out a generation
   timeout, and must not be handed an exception for asking.
2. **Cost is zero, and that is a claim about invoices, not about physics.** The
   electricity is real; we cannot price it, and a fabricated number would corrupt
   the ledger that every budget guard reads. So `Usage.usd` is exactly
   `Decimal("0")` and the price table is never consulted -- it *must not* be, as
   `compute_usd` raises `UnknownModelPriceError` for any model outside
   `PRICE_TABLE`, which no local model will ever be in.
3. **Tool calling is inconsistent across local models.** Many gguf builds ignore
   a `tools` payload and answer in prose. That is reported honestly (`text` set,
   `tool_calls=[]`, `is_final=True`) and never patched over: the caller already
   treats "prose where a tool call was required" as a failure, and that is where
   the judgement belongs. The practical consequence is worth stating outright --
   **a small local model will often fail structured-output nodes**, so point this
   provider at cheap classification and extraction work, not at GENERATE.
4. **Timeouts are long.** A 7B model on CPU can legitimately take a minute, so
   the default is 120 s where a hosted provider gets 60 s. The number is not
   caution, it is the hardware: there is no queue to jump and no GPU fleet
   behind it, and a fallback fired at 60 s would abandon a call that was working.

Errors land on the same tree as every other adapter, split by whether trying the
next provider could plausibly help: connection refused -> `ProviderServerError`
(retryable, and by far the most likely failure -- the server is not running); a
timeout -> `ProviderTimeoutError`; a 4xx -> `ProviderRequestError` (a 404 here
usually means the model was never `ollama pull`ed, which the next provider cannot
fix either).
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
    omit,
)
from openai.types.chat import ChatCompletion
from pydantic import BaseModel

from backend.app.llm.contract import (
    Completion,
    Message,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    ToolCall,
    ToolSpec,
    Usage,
)

# The wire format is OpenAI's, so the request-side translation is OpenRouter's.
# Importing it rather than copying it is the point: a divergence between two
# copies of this translation would show up as a model that silently stopped
# calling tools, with no error anywhere.
from backend.app.llm.openrouter_provider import (
    _to_message_params,
    _to_tool_param,
    parse_tool_calls,
)
from backend.app.llm.pricing import conservative_token_estimate

DEFAULT_OLLAMA_BASE_URL: Final = "http://localhost:11434/v1"

#: A local 7B model on CPU can legitimately take a minute to answer, so this is
#: double the hosted default. See point 4 in the module docstring.
DEFAULT_TIMEOUT_S: Final = 120.0

#: Availability must be quick to establish. Two seconds is generous for a
#: loopback GET, and a caller asking "is Ollama up?" cannot afford
#: `DEFAULT_TIMEOUT_S` to find out that it is not.
PROBE_TIMEOUT_S: Final = 2.0

#: Bound output tokens when the caller does not, matching the other adapters so
#: the router's pre-call estimate and the actual request cannot disagree.
DEFAULT_MAX_TOKENS: Final = 2048

#: The OpenAI SDK refuses to construct a client without an `api_key`, and Ollama
#: ignores the `Authorization` header entirely. This placeholder is what keeps
#: the no-credential path -- the entire reason this provider exists -- working.
_PLACEHOLDER_KEY: Final = "ollama-needs-no-key"

#: A local call costs zero *dollars*. Not an estimate and not a missing value:
#: no invoice exists, so any other number would be invented. The real cost is
#: electricity and wall-clock time, which we cannot meter and therefore do not
#: claim to. `PRICE_TABLE` is deliberately not consulted -- see point 2.
LOCAL_CALL_USD: Final = Decimal("0")


def ollama_root(base_url: str) -> str:
    """Strip the OpenAI-compatibility suffix to reach Ollama's own API.

    `/v1/chat/completions` is the compatibility shim; `/api/tags` is Ollama's
    native API one level above it. Accepts either form of `base_url` so a caller
    configures one URL rather than two that must be kept in step.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed


class OllamaStatus(BaseModel):
    """Whether a local Ollama answered, and what it has installed.

    `reachable` is the availability answer a caller wants; `detail` carries why
    not, so a UI can say "connection refused" instead of "unavailable".
    """

    reachable: bool
    base_url: str
    models: tuple[str, ...] = ()
    detail: str | None = None


def installed_model_names(payload: object) -> tuple[str, ...]:
    """Read model names out of an `/api/tags` body, tolerating a strange one.

    Parsed defensively on purpose: port 11434 might be answering with something
    that is not Ollama at all, and "reachable but no models" is a better report
    than a `TypeError` from an admin screen.
    """
    if not isinstance(payload, dict):
        return ()
    entries = payload.get("models")
    if not isinstance(entries, list):
        return ()

    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # `name` is the tag ("llama3.1:8b"); `model` appears on newer builds.
        raw = entry.get("name") or entry.get("model")
        if isinstance(raw, str) and raw:
            names.append(raw)
    return tuple(names)


async def probe(
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    *,
    timeout_s: float = PROBE_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> OllamaStatus:
    """Ask a local Ollama whether it is there, and what it has. Never raises.

    Returning a status rather than raising is the whole design: a caller deciding
    whether to offer local models is asking a question, not handling an error,
    and every plausible failure here (nothing listening, wrong service, garbage
    body) has the same answer -- "not available, here is why".

    `client` exists so tests inject a transport; production passes nothing.
    """
    url = f"{ollama_root(base_url)}/api/tags"
    http = client if client is not None else httpx.AsyncClient(timeout=timeout_s)
    try:
        try:
            response = await http.get(url, timeout=timeout_s)
        except httpx.HTTPError as exc:
            return OllamaStatus(
                reachable=False,
                base_url=base_url,
                detail=f"{type(exc).__name__}: {exc}",
            )

        if response.status_code >= 400:
            return OllamaStatus(
                reachable=False,
                base_url=base_url,
                detail=f"{url} answered HTTP {response.status_code}",
            )

        try:
            payload: object = response.json()
        except ValueError as exc:  # json.JSONDecodeError subclasses ValueError
            return OllamaStatus(
                reachable=False,
                base_url=base_url,
                detail=f"{url} answered HTTP 200 with a non-JSON body: {exc}",
            )
    finally:
        if client is None:
            await http.aclose()

    return OllamaStatus(
        reachable=True,
        base_url=base_url,
        models=installed_model_names(payload),
    )


class OllamaProvider:
    """A local Ollama server. Satisfies `contract.Provider`."""

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: AsyncOpenAI | None = None,
    ) -> None:
        """Build the adapter. There is no credential parameter, by design."""
        self._client = client or AsyncOpenAI(
            api_key=_PLACEHOLDER_KEY,
            base_url=base_url,
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
        """Run one local chat completion and normalise it onto `Completion`."""
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=_to_message_params(messages),
                tools=[_to_tool_param(tool) for tool in tools] if tools else omit,
                temperature=temperature if temperature is not None else omit,
                max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
            )
        except RateLimitError as exc:
            # Ollama does not rate-limit, but a reverse proxy in front of it can.
            raise ProviderRateLimitError(self.name, model, f"rate limited: {exc}") from exc
        except APITimeoutError as exc:  # subclass of APIConnectionError
            raise ProviderTimeoutError(self.name, model, f"timed out: {exc}") from exc
        except APIConnectionError as exc:
            # The likeliest failure of all: `ollama serve` is not running.
            raise ProviderServerError(
                self.name,
                model,
                f"could not reach a local Ollama server: {exc}. Is `ollama serve` running?",
            ) from exc
        except APIStatusError as exc:
            raise self._status_error(model, exc) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._to_completion(model, response, latency_ms)

    def _status_error(
        self, model: str, exc: APIStatusError
    ) -> ProviderServerError | ProviderRequestError:
        """Split a non-2xx into retryable (5xx) and not (other 4xx).

        A 404 from Ollama almost always means the model has not been pulled. That
        is our request being wrong about the world, not a transient fault, so it
        is a `ProviderRequestError`: the next provider would reject the same id
        just as fast.
        """
        status = exc.status_code
        if status >= 500:
            return ProviderServerError(
                self.name, model, f"server error {status}: {exc}", status_code=status
            )
        return ProviderRequestError(
            self.name,
            model,
            f"request rejected ({status}): {exc}. A 404 usually means the model "
            f"is not installed -- try `ollama pull {model}`.",
            status_code=status,
        )

    def _to_completion(self, model: str, response: ChatCompletion, latency_ms: int) -> Completion:
        """Map the response onto the neutral `Completion`, at zero cost."""
        if not response.choices:
            raise ProviderServerError(self.name, model, "response contained no choices")

        message = response.choices[0].message
        # Shared with the OpenRouter adapter: one implementation of the OpenAI wire
        # format, so the two cannot drift apart.
        tool_calls = parse_tool_calls(provider=self.name, model=model, raw=message.tool_calls)
        text = message.content

        tokens_in, tokens_out, estimated = self._token_counts(response, text, tool_calls)
        usage = Usage(
            provider=self.name,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            # Not `compute_usd`: a local model has no price-table row and that
            # call would raise. See LOCAL_CALL_USD.
            usd=LOCAL_CALL_USD,
            latency_ms=latency_ms,
            estimated=estimated,
        )
        # `is_final` is derived from what the model DID, not from what was asked
        # of it. A local model that ignored `tools` and answered in prose lands
        # here as a final answer -- honest, and the caller's structured-output
        # check is what turns that into a failure.
        return Completion(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            is_final=not tool_calls,
        )

    def _token_counts(
        self, response: ChatCompletion, text: str | None, tool_calls: Sequence[ToolCall]
    ) -> tuple[int, int, bool]:
        """Prefer the server's own counts; estimate only if it sent none.

        Ollama's compatibility layer does report usage, but the counts still feed
        the ledger's token columns, so a missing block is estimated and *flagged*
        rather than recorded as zero. Note this flag is about tokens only: the
        cost is a true zero either way.
        """
        usage = response.usage
        if usage is not None:
            return usage.prompt_tokens, usage.completion_tokens, False

        rendered = text or ""
        for call in tool_calls:
            rendered += json.dumps(call.arguments, sort_keys=True)
        return 0, conservative_token_estimate(rendered), True
