"""Observability: the tracing seam.

One import surface, so a call site never reaches into a submodule::

    from backend.app.obs import get_tracer, llm_span_fields

    tracer = get_tracer()
    with tracer.span("llm.complete", **llm_span_fields(...)) as span:
        ...

With no Langfuse credentials this is a free no-op and `tracing_status()` says so
out loud -- the same posture as the model providers, and for the same reason: a
missing credential must never mean a crash, a silent paid call, or a false claim
that traces exist.
"""

from backend.app.obs.tracing import (
    LANGFUSE_CAPTURE_TEXT_ENV,
    LANGFUSE_HOST_ENV,
    LANGFUSE_PACKAGE,
    LANGFUSE_PUBLIC_KEY_ENV,
    LANGFUSE_SECRET_KEY_ENV,
    NOOP_SPAN,
    REDACTED,
    REQUIRED_SPAN_FIELDS,
    TEXT_FIELDS,
    LangfuseClient,
    LangfuseTracer,
    NoopSpan,
    NoopTracer,
    Span,
    Tracer,
    TracingError,
    TracingStatus,
    TracingUnavailableError,
    UsageLike,
    get_tracer,
    llm_span_fields,
    redact_text_fields,
    tracing_status,
)

__all__ = [
    "LANGFUSE_CAPTURE_TEXT_ENV",
    "LANGFUSE_HOST_ENV",
    "LANGFUSE_PACKAGE",
    "LANGFUSE_PUBLIC_KEY_ENV",
    "LANGFUSE_SECRET_KEY_ENV",
    "NOOP_SPAN",
    "REDACTED",
    "REQUIRED_SPAN_FIELDS",
    "TEXT_FIELDS",
    "LangfuseClient",
    "LangfuseTracer",
    "NoopSpan",
    "NoopTracer",
    "Span",
    "Tracer",
    "TracingError",
    "TracingStatus",
    "TracingUnavailableError",
    "UsageLike",
    "get_tracer",
    "llm_span_fields",
    "redact_text_fields",
    "tracing_status",
]
