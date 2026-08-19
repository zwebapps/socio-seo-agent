"""The tracing seam, tested for the four things that can actually hurt us.

1. **With no credentials it is a working no-op.** Tracing is instrumentation; an
   uninstrumented process must still run. So the no-op path is tested for being
   genuinely free (no allocation, no I/O) and for never raising, whatever it is
   handed.
2. **With credentials but no package it fails loudly, once, at construction.**
   `langfuse` is not a declared dependency yet, so a configured deployment gets
   `TracingUnavailableError` naming the missing package -- not a silent no-op that
   would leave someone believing traces were being written.
3. **Prompt and completion text is not captured by default.** A trace is an
   operational record. Shipping a customer's document text to a third party has to
   be a decision someone made, so it is behind an explicit flag and the default is
   off.
4. **The backend can never fail a run.** A client that raises is swallowed and
   counted, because an observability outage that takes down content generation is
   a worse outage than the one it was watching for.

No network, no package import, no credentials from the environment: every
configured case passes `env={...}` explicitly, the way every provider seam in this
project is tested.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from backend.app.obs.tracing import (
    LANGFUSE_CAPTURE_TEXT_ENV,
    LANGFUSE_PUBLIC_KEY_ENV,
    LANGFUSE_SECRET_KEY_ENV,
    REDACTED,
    REQUIRED_SPAN_FIELDS,
    TEXT_FIELDS,
    LangfuseTracer,
    NoopSpan,
    NoopTracer,
    TracingUnavailableError,
    get_tracer,
    llm_span_fields,
    redact_text_fields,
    tracing_status,
)

CONFIGURED: Mapping[str, str] = {
    LANGFUSE_PUBLIC_KEY_ENV: "pk-lf-test",
    LANGFUSE_SECRET_KEY_ENV: "sk-lf-test",
}


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class RecordingSpan:
    """A span that keeps what it was told, so a test can inspect it."""

    def __init__(self, name: str, metadata: Mapping[str, object]) -> None:
        self.name = name
        self.metadata: dict[str, object] = dict(metadata)
        self.updates: list[dict[str, object]] = []
        self.ended = False
        self.end_fields: dict[str, object] = {}

    def update(self, **fields: object) -> None:
        self.updates.append(dict(fields))

    def end(self, **fields: object) -> None:
        self.ended = True
        self.end_fields = dict(fields)


class RecordingClient:
    """Stands in for the Langfuse SDK client, which is not installed."""

    def __init__(self) -> None:
        self.spans: list[RecordingSpan] = []
        self.scores: list[dict[str, Any]] = []

    def start_span(self, *, name: str, metadata: Mapping[str, object]) -> RecordingSpan:
        span = RecordingSpan(name, metadata)
        self.spans.append(span)
        return span

    def create_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        self.scores.append({"trace_id": trace_id, "name": name, "value": value, "comment": comment})


class ExplodingClient:
    """Every method fails. Models a Langfuse outage or a bad host."""

    def start_span(self, *, name: str, metadata: Mapping[str, object]) -> RecordingSpan:
        raise RuntimeError("langfuse is unreachable")

    def create_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        raise RuntimeError("langfuse is unreachable")


# --------------------------------------------------------------------------- #
# The no-op path
# --------------------------------------------------------------------------- #


def test_get_tracer_with_no_credentials_returns_the_noop() -> None:
    assert isinstance(get_tracer(env={}), NoopTracer)


def test_get_tracer_needs_both_keys() -> None:
    """One key is a misconfiguration, not a configuration.

    Half-configured must degrade to the no-op rather than construct a client that
    would 401 on the first flush.
    """
    assert isinstance(get_tracer(env={LANGFUSE_PUBLIC_KEY_ENV: "pk-lf-test"}), NoopTracer)
    assert isinstance(get_tracer(env={LANGFUSE_SECRET_KEY_ENV: "sk-lf-test"}), NoopTracer)


def test_blank_keys_count_as_absent() -> None:
    """`LANGFUSE_PUBLIC_KEY=` in a .env file is how people unset a variable."""
    env = {LANGFUSE_PUBLIC_KEY_ENV: "  ", LANGFUSE_SECRET_KEY_ENV: ""}
    assert isinstance(get_tracer(env=env), NoopTracer)
    assert tracing_status(env=env).configured is False


def test_noop_span_allocates_nothing_per_span() -> None:
    """The no-op returns shared objects rather than building a new one per call.

    Tracing sits on the hot path of every model and tool call, so "free" has to
    mean free. The tracer is its own context manager and the span is a module
    singleton, so the only allocation left is the kwargs dict Python builds at the
    call site -- this seam adds none.
    """
    tracer = NoopTracer()
    first = tracer.span("llm.complete", run_id="r1")
    second = tracer.span("tool.call", run_id="r2")

    assert first is tracer
    assert second is tracer

    with first as span_a, second as span_b:
        assert span_a is span_b
        assert isinstance(span_a, NoopSpan)


def test_noop_tracer_never_raises_whatever_it_is_handed() -> None:
    """Instrumentation must not be able to fail the thing it instruments."""
    tracer = NoopTracer()

    with tracer.span("outer", run_id=None, weird=object(), nested={"a": [1, 2]}) as span:
        span.update(tokens_in=1, prompt="a customer document")
        with tracer.span("inner") as inner:
            inner.update()
            inner.end(outcome="ok")
        span.end(outcome="ok", usd=Decimal("0.001"))

    tracer.score(trace_id="", name="human_rating", value=float("nan"), comment=None)
    tracer.score(trace_id="t1", name="human_rating", value=1.0, comment="thumbs up")


def test_noop_span_swallows_an_exception_without_hiding_it() -> None:
    """A failing body still fails: the span must not act as an except block."""
    with pytest.raises(ValueError, match="boom"), NoopTracer().span("node"):
        raise ValueError("boom")


def test_tracing_status_reports_noop_when_unconfigured() -> None:
    status = tracing_status(env={})

    assert status.configured is False
    assert status.backend == "noop"
    assert status.capture_text is False
    # The message has to be usable in a UI or an /admin panel: it must say what is
    # happening and which variables would change it.
    assert LANGFUSE_PUBLIC_KEY_ENV in status.message
    assert LANGFUSE_SECRET_KEY_ENV in status.message
    assert "no-op" in status.message.lower()


# --------------------------------------------------------------------------- #
# The honest gap: configured, but the package is not installed
# --------------------------------------------------------------------------- #


def test_configured_without_the_package_raises_a_named_error() -> None:
    """`langfuse` is not in pyproject.toml, and the error has to say so.

    A no-op here would be the worst outcome: someone sets two keys, sees no error,
    and believes there are traces to show a grader.
    """
    with pytest.raises(TracingUnavailableError) as caught:
        get_tracer(env=CONFIGURED)

    message = str(caught.value)
    assert "langfuse" in message
    assert "pyproject.toml" in message


def test_tracing_status_distinguishes_missing_package_from_missing_keys() -> None:
    """Two different failures that a single boolean would conflate."""
    status = tracing_status(env=CONFIGURED)

    assert status.configured is True
    assert status.package_installed is False
    assert status.backend == "unavailable"
    assert "langfuse" in status.message


# --------------------------------------------------------------------------- #
# Text capture is opt-in
# --------------------------------------------------------------------------- #


def test_text_fields_are_redacted_by_default() -> None:
    fields = redact_text_fields(
        {"prompt": "Dear Mr Schmidt, our price list is...", "tokens_in": 42},
        capture_text=False,
    )

    assert fields["prompt"] == REDACTED
    assert fields["tokens_in"] == 42


def test_text_fields_pass_through_when_opted_in() -> None:
    fields = redact_text_fields({"prompt": "the prompt"}, capture_text=True)
    assert fields["prompt"] == "the prompt"


def test_every_declared_text_field_is_actually_redacted() -> None:
    """Guards the list itself: adding a name to TEXT_FIELDS must have an effect."""
    payload: dict[str, object] = dict.fromkeys(TEXT_FIELDS, "customer text")
    fields = redact_text_fields(payload, capture_text=False)

    assert set(fields) == set(TEXT_FIELDS)
    assert all(value == REDACTED for value in fields.values())


def test_redaction_marks_the_omission_rather_than_dropping_the_key() -> None:
    """A viewer must be able to tell "withheld" from "never sent"."""
    fields = redact_text_fields({"completion": "..."}, capture_text=False)
    assert "completion" in fields


def test_tracer_redacts_text_before_it_reaches_the_client() -> None:
    """The chokepoint is the tracer, so no call site can leak by forgetting."""
    client = RecordingClient()
    tracer = LangfuseTracer(client=client)

    with tracer.span("llm.complete", prompt="a customer document", tokens_in=7):
        pass

    span = client.spans[0]
    assert span.metadata["prompt"] == REDACTED
    assert span.metadata["tokens_in"] == 7


def test_tracer_captures_text_when_explicitly_opted_in() -> None:
    client = RecordingClient()
    tracer = LangfuseTracer(client=client, capture_text=True)

    with tracer.span("llm.complete", prompt="a customer document"):
        pass

    assert client.spans[0].metadata["prompt"] == "a customer document"


def test_capture_text_comes_from_the_environment() -> None:
    env = {**CONFIGURED, LANGFUSE_CAPTURE_TEXT_ENV: "true"}
    assert tracing_status(env=env).capture_text is True
    assert tracing_status(env={**CONFIGURED, LANGFUSE_CAPTURE_TEXT_ENV: "no"}).capture_text is False


# --------------------------------------------------------------------------- #
# What a span must carry
# --------------------------------------------------------------------------- #


class _Usage:
    """The subset of `llm.contract.Usage` the seam reads, structurally."""

    provider = "openrouter"
    model = "anthropic/claude-sonnet-4.5"
    tokens_in = 1200
    tokens_out = 340
    usd = Decimal("0.0042")
    latency_ms = 1830


def test_llm_span_fields_carries_every_required_field() -> None:
    """A trace missing any of these cannot answer the question it exists for.

    "Which node, on which model, at which prompt version, cost how much and ended
    how?" is the whole point; a span that drops one of them is decoration.
    """
    fields = llm_span_fields(
        run_id="run-1",
        business_id="biz-1",
        node="GENERATE",
        prompt_version="generate.v2",
        usage=_Usage(),
        outcome="ok",
    )

    missing = [name for name in REQUIRED_SPAN_FIELDS if name not in fields]
    assert not missing, f"span fields missing {missing}"
    assert fields["usd"] == "0.0042", "money must serialise as a string, never a float"
    assert fields["model"] == "anthropic/claude-sonnet-4.5"
    assert fields["tokens_out"] == 340


def test_llm_span_fields_omits_text_unless_given() -> None:
    fields = llm_span_fields(
        run_id="run-1",
        business_id="biz-1",
        node="GENERATE",
        prompt_version="generate.v2",
        usage=_Usage(),
        outcome="ok",
    )
    assert not TEXT_FIELDS & set(fields)


# --------------------------------------------------------------------------- #
# Lifecycle and degradation
# --------------------------------------------------------------------------- #


def test_span_is_ended_on_exit() -> None:
    client = RecordingClient()

    with LangfuseTracer(client=client).span("node") as span:
        span.update(tokens_in=1)

    recorded = client.spans[0]
    assert recorded.ended is True
    assert recorded.updates == [{"tokens_in": 1}]


def test_span_records_the_failure_and_re_raises() -> None:
    """An exception inside a span is both traced and propagated."""
    client = RecordingClient()

    with (
        pytest.raises(ValueError, match="rate limited"),
        LangfuseTracer(client=client).span("node"),
    ):
        raise ValueError("rate limited")

    recorded = client.spans[0]
    assert recorded.ended is True
    assert recorded.end_fields["outcome"] == "error"
    assert "rate limited" in str(recorded.end_fields["error"])


def test_a_broken_backend_cannot_fail_the_run() -> None:
    """Langfuse being down must degrade to the no-op, and say it degraded."""
    tracer = LangfuseTracer(client=ExplodingClient())

    with tracer.span("node") as span:
        span.update(tokens_in=1)
    tracer.score(trace_id="t1", name="human_rating", value=1.0)

    assert tracer.degraded_calls == 2


def test_scores_reach_the_client() -> None:
    """Feedback-as-a-score is the second half of the observability deliverable."""
    client = RecordingClient()

    LangfuseTracer(client=client).score(
        trace_id="trace-1",
        name="human_rating",
        value=0.75,
        comment="on brand, weak CTA",
    )

    assert client.scores == [
        {
            "trace_id": "trace-1",
            "name": "human_rating",
            "value": 0.75,
            "comment": "on brand, weak CTA",
        }
    ]
