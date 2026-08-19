"""A deterministic, hermetic provider. No network, ever.

This is what runs when no API key is configured, and what every test that needs
"a model" uses. Two properties make it useful rather than merely present:

* **Deterministic.** The same messages in produce a byte-identical `Completion`
  out, including token counts and latency. The response is chosen by hashing
  the input, so a test can assert on output without pinning a magic string.
* **It can call a tool.** When the caller passes tools, the fake asks for the
  first one -- then, once it sees the result come back as a `TOOL` turn, it
  answers. That is the minimum behaviour needed to exercise the whole node loop
  in docs/AGENT_RUNTIME.md section 4 without a live model.

Latency is reported as 0 ms rather than measured, because a measured value would
break determinism for the sake of a number that means nothing here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from backend.app.llm.contract import (
    Completion,
    Message,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)
from backend.app.llm.pricing import (
    compute_usd,
    conservative_token_estimate,
    is_priced,
)

#: Canned answers, picked by input hash. Deliberately in the product's own voice
#: so a fake-provider run reads as plausibly wrong rather than as broken.
CANNED_RESPONSES: Final[tuple[str, ...]] = (
    "Fake provider response: no model was called. Configure OPENROUTER_API_KEY "
    "or ANTHROPIC_API_KEY to use a real model.",
    "Fake provider response: this text is generated locally from a hash of the "
    "prompt and is not model output.",
    "Fake provider response: deterministic placeholder. Wire a provider key to replace it.",
    "Fake provider response: running without credentials, so nothing was sent over the network.",
)


def _canonical(
    messages: Sequence[Message],
    model: str,
    tools: Sequence[ToolSpec] | None,
) -> str:
    """Serialise the request into a stable string for hashing.

    `sort_keys=True` matters: dict iteration order must not leak into the
    digest, or "deterministic" would hold only within a single process.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": str(message.role),
                "content": message.content,
                "tool_call_id": message.tool_call_id,
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments} for call in message.tool_calls
                ],
            }
            for message in messages
        ],
        "tools": [tool.name for tool in (tools or ())],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def _placeholder_for(schema: Mapping[str, Any], seed: str) -> Any:
    """Build a deterministic value satisfying one JSON-Schema property."""
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    match schema.get("type"):
        case "integer":
            return 1
        case "number":
            return 1.0
        case "boolean":
            return True
        case "array":
            # ONE element, not an empty list.
            #
            # An empty array is schema-valid and useless: any tool whose payload is a
            # list is silently defeated by it. The knowledge-base grader is exactly
            # that shape — it returns `grades: [...]`, so an empty list made every
            # retrieved passage read as ungraded, retrieval always reported
            # "insufficient", and the whole RAG path was unexercisable without a paid
            # key. Found by the eval harness reporting an identical score for
            # RAG-on and RAG-off.
            items = schema.get("items")
            if isinstance(items, dict):
                return [_placeholder_for(items, f"{seed}-0")]
            return [f"fake-{seed}-0"]
        case "object":
            # Recurse into the declared properties. An empty object has the same defect
            # as an empty array one level down: schema-valid, and useless to any caller
            # that reads a field out of it.
            properties = schema.get("properties")
            if isinstance(properties, dict):
                return {
                    name: _placeholder_for(prop, f"{seed}-{name}")
                    if isinstance(prop, dict)
                    else f"fake-{seed}-{name}"
                    for name, prop in properties.items()
                }
            return {}
        case _:
            return f"fake-{seed}"


def _arguments_for(tool: ToolSpec, seed: str) -> dict[str, Any]:
    """Fill every required property of `tool`'s schema with a placeholder.

    Only required properties are filled. An argument the schema does not demand
    is an argument the real model might well omit, and the fake should not train
    a tool to expect more than its contract promises.
    """
    properties = tool.parameters.get("properties")
    required = tool.parameters.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return {}

    arguments: dict[str, Any] = {}
    for name in required:
        if not isinstance(name, str):
            continue
        schema = properties.get(name)
        arguments[name] = (
            _placeholder_for(schema, seed) if isinstance(schema, dict) else f"fake-{seed}"
        )
    return arguments


class FakeProvider:
    """Hermetic stand-in for a real model. Satisfies `contract.Provider`."""

    name = "fake"

    def __init__(self, *, responses: Sequence[str] | None = None) -> None:
        # `responses is None` rather than `responses or ...`: an explicitly empty
        # sequence is a caller mistake worth reporting, not a request for the
        # defaults, and `or` would silently swallow it.
        chosen = CANNED_RESPONSES if responses is None else tuple(responses)
        if not chosen:
            raise ValueError("FakeProvider needs at least one canned response.")
        self._responses: tuple[str, ...] = chosen

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Return a deterministic completion. Makes no network call."""
        # temperature and max_tokens are accepted to satisfy the Provider
        # protocol; a hash cannot be made hotter or longer.
        del temperature, max_tokens

        digest = hashlib.sha256(_canonical(messages, model, tools).encode("utf-8")).hexdigest()
        seed = digest[:8]
        tokens_in = sum(conservative_token_estimate(message.content) for message in messages)

        # A tool result already came back, so answer. Otherwise, if tools are on
        # offer, ask for one. This is what lets a node loop terminate.
        awaiting_tool_result = tools and not any(message.role is Role.TOOL for message in messages)

        if awaiting_tool_result and tools:
            tool = tools[0]
            call = ToolCall(
                name=tool.name,
                arguments=_arguments_for(tool, seed),
                call_id=f"fake_call_{seed}",
            )
            tokens_out = conservative_token_estimate(json.dumps(call.arguments, sort_keys=True))
            return Completion(
                text=None,
                tool_calls=[call],
                usage=self._usage(model, tokens_in, tokens_out),
                is_final=False,
            )

        index = int(digest, 16) % len(self._responses)
        text = self._responses[index]
        return Completion(
            text=text,
            tool_calls=[],
            usage=self._usage(model, tokens_in, conservative_token_estimate(text)),
            is_final=True,
        )

    def _usage(self, model: str, tokens_in: int, tokens_out: int) -> Usage:
        """Price the fake call through the real table when the model is known.

        Under the fake's own `fake/*` models the table says zero, which is the
        honest answer -- nothing was spent. When a test points the fake at a
        real model id, it prices the same way production would, so a budget or
        ledger test exercises the real arithmetic rather than a special case.
        """
        usd = compute_usd(model, tokens_in, tokens_out) if is_priced(model) else Decimal(0)
        return Usage(
            provider=self.name,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd=usd,
            latency_ms=0,
        )
