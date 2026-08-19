"""The tracing seam: Langfuse when it is configured, a free no-op when it is not.

Same posture as every other provider in this project (`docs/ARCHITECTURE.md` §14):
**a missing credential means the fallback, plus a status that says so** -- never a
crash, never a silent paid call, and never a pretence that traces are being
written when they are not.

Three decisions carry the design.

* **The no-op is genuinely free.** Tracing sits on the hot path of every model and
  tool call, so an uninstrumented process must pay nothing for the instrumentation
  it is not using. :class:`NoopTracer` is its own context manager and yields a
  module-level singleton span, so it allocates nothing per span beyond the kwargs
  dict Python builds at the call site. Nothing it is handed is inspected, copied,
  formatted or serialised.

* **Configured-but-unavailable is a loud failure, once, at construction.**
  ``langfuse`` is deliberately *not* a declared dependency yet (see
  ``pyproject.toml``: "Still phase-gated"), so setting the two keys today raises
  :class:`TracingUnavailableError` naming the missing package. Degrading silently
  to the no-op would be the worst of the three options: someone sets the keys,
  sees no error, and believes there is a trace to show. A registry with an honest
  gap beats a fake implementation.

* **Prompt and completion TEXT is not captured by default.** A trace is an
  *operational* record -- who ran what, on which model, at what cost, ending how.
  A customer's uploaded price list or draft copy is not operational data, and
  sending it to a third-party processor is a data-protection decision that has to
  be made deliberately rather than inherited from a default. So every field in
  :data:`TEXT_FIELDS` is replaced with :data:`REDACTED` unless
  ``LANGFUSE_CAPTURE_TEXT`` is explicitly truthy. The redaction happens *inside
  the tracer*, which is the only chokepoint no call site can forget.

**A fourth property, learned from the provider seams:** an observability backend
must never be able to fail the thing it observes. A client that raises is
swallowed and counted in :attr:`LangfuseTracer.degraded_calls`, so a Langfuse
outage costs you traces, not content.

Wiring this into the router is one line at the call site -- see
:func:`llm_span_fields` -- and this module imports nothing from
``backend.app.llm``, so the dependency runs one way only.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from contextlib import AbstractContextManager, suppress
from decimal import Decimal
from types import TracebackType
from typing import Final, Literal, Protocol

from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

LANGFUSE_PUBLIC_KEY_ENV: Final = "LANGFUSE_PUBLIC_KEY"
# The NAME of an environment variable, not a secret: nothing in this module ever
# holds a credential value beyond passing it to the SDK constructor.
LANGFUSE_SECRET_KEY_ENV: Final = "LANGFUSE_SECRET_KEY"  # noqa: S105
LANGFUSE_HOST_ENV: Final = "LANGFUSE_HOST"

#: Opt-in for capturing prompt and completion text. Off unless explicitly truthy.
LANGFUSE_CAPTURE_TEXT_ENV: Final = "LANGFUSE_CAPTURE_TEXT"

#: The package that carries the real implementation. Not a declared dependency:
#: adding it is a deliberate act, gated on the Langfuse keys arriving (BACKLOG
#: Phase 12).
LANGFUSE_PACKAGE: Final = "langfuse"

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})

#: What a span must carry. A trace missing any of these cannot answer the
#: question it exists for -- "which node, on which model, at which prompt
#: version, cost how much, and ended how?" -- so the list is a published contract
#: with a test behind it rather than a convention.
REQUIRED_SPAN_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "business_id",
    "node",
    "model",
    "provider",
    "prompt_version",
    "tokens_in",
    "tokens_out",
    "usd",
    "latency_ms",
    "outcome",
)

#: Field names whose values are customer or model TEXT rather than operational
#: metadata. Redacted unless text capture is explicitly enabled.
TEXT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "prompt",
        "completion",
        "input",
        "output",
        "messages",
        "chunk_text",
        "document_text",
    }
)

#: Substituted for a withheld text value. A marker rather than a dropped key, so
#: a reader of the trace can tell "deliberately withheld" from "never recorded".
REDACTED: Final = "<omitted: text capture disabled>"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class TracingError(Exception):
    """Base class for every failure raised by ``backend.app.obs``."""


class TracingUnavailableError(TracingError):
    """Tracing is configured, but the backend package is not installed.

    Raised at construction, not at the first span: a misconfiguration should
    surface where it can be fixed, and exactly once.
    """

    def __init__(self, package: str = LANGFUSE_PACKAGE) -> None:
        self.package = package
        super().__init__(
            f"Tracing is configured ({LANGFUSE_PUBLIC_KEY_ENV} and "
            f"{LANGFUSE_SECRET_KEY_ENV} are set) but the {package!r} package is "
            "not installed. It is phase-gated in pyproject.toml and is added "
            "together with the Langfuse credentials (BACKLOG Phase 12). Either "
            f"install {package!r} or unset the keys to run on the no-op tracer. "
            "This is raised rather than silently degraded because a no-op here "
            "would leave you believing traces were being written."
        )


# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #


class Span(Protocol):
    """One unit of traced work.

    Structural, not inherited: a backend's own span object satisfies this by
    shape, so nothing here has to wrap it.
    """

    def update(self, **fields: object) -> None:
        """Attach or overwrite fields on an open span."""
        ...

    def end(self, **fields: object) -> None:
        """Close the span, optionally attaching final fields."""
        ...


class Tracer(Protocol):
    """What a call site depends on. Satisfied by the no-op and by Langfuse."""

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Open a span. Ends on exit, including on an exception."""
        ...

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        """Attach a numeric score to a trace -- human feedback, or a rubric result."""
        ...


class UsageLike(Protocol):
    """The subset of ``llm.contract.Usage`` this module reads.

    Declared structurally so ``backend.app.obs`` imports nothing from
    ``backend.app.llm``: the router may depend on the tracer, and a dependency
    that runs both ways is a dependency that eventually becomes a cycle.
    """

    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    usd: Decimal
    latency_ms: int


class LangfuseClient(Protocol):
    """The two client calls the real backend needs.

    **Honest gap.** This shape is written against the Langfuse Python SDK's
    documented surface (a ``start_span`` returning an object with ``update`` and
    ``end``, and a ``create_score``), but the package is not installed here, so it
    is **unverified against a real client**. Whoever installs ``langfuse`` must
    check these two signatures against the version they pin and adapt this
    Protocol -- which is a five-line change confined to this module, and is
    exactly why the client is injectable.
    """

    def start_span(self, *, name: str, metadata: Mapping[str, object]) -> Span:
        """Begin a span on the backend."""
        ...

    def create_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        """Attach a score to an existing trace."""
        ...


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def redact_text_fields(
    fields: Mapping[str, object],
    *,
    capture_text: bool,
) -> dict[str, object]:
    """Replace every :data:`TEXT_FIELDS` value with :data:`REDACTED`.

    A pure function so the policy can be tested directly, and so the same rule
    can be reused by any future backend adapter. ``capture_text=True`` returns the
    fields unchanged -- the decision is the caller's to make once, in
    :func:`get_tracer`, from the environment.
    """
    if capture_text:
        return dict(fields)
    return {name: (REDACTED if name in TEXT_FIELDS else value) for name, value in fields.items()}


def llm_span_fields(
    *,
    run_id: str,
    business_id: str,
    node: str,
    prompt_version: str,
    usage: UsageLike,
    outcome: str,
    **extra: object,
) -> dict[str, object]:
    """Build the span payload for one model call.

    This is the helper that makes instrumenting the router a one-liner::

        with tracer.span("llm.complete", **llm_span_fields(...)) as span:
            ...

    ``usd`` is stringified, not floated: money is ``Decimal`` in this codebase and
    a trace that rounds it is a trace that disagrees with the ledger it is meant
    to explain.

    Text is not included unless a caller passes it explicitly through ``extra``,
    and even then the tracer redacts it unless capture is enabled. Two gates, and
    the effective one is the tracer's.
    """
    return {
        "run_id": run_id,
        "business_id": business_id,
        "node": node,
        "provider": usage.provider,
        "model": usage.model,
        "prompt_version": prompt_version,
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "usd": str(usage.usd),
        "latency_ms": usage.latency_ms,
        "outcome": outcome,
        **extra,
    }


# --------------------------------------------------------------------------- #
# The no-op
# --------------------------------------------------------------------------- #


class NoopSpan:
    """A span that does nothing at all, and does it without allocating.

    Both methods take their arguments and drop them unread. Nothing is copied,
    formatted or serialised, so passing a large prompt to a no-op span costs the
    kwargs dict and no more.
    """

    __slots__ = ()

    def update(self, **fields: object) -> None:
        """Discard the fields."""

    def end(self, **fields: object) -> None:
        """Discard the fields."""


#: The single span every no-op call yields. Stateless, so sharing it is safe
#: across nesting, threads and concurrent tasks.
NOOP_SPAN: Final = NoopSpan()


class NoopTracer:
    """The tracer used when Langfuse is not configured.

    It is its own context manager: :meth:`span` returns ``self``, so a traced
    block allocates nothing. That is safe precisely because there is no state --
    nested and concurrent spans cannot interfere when nobody is recording.
    """

    __slots__ = ()

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Return a context manager that yields the shared no-op span."""
        return self

    def __enter__(self) -> Span:
        return NOOP_SPAN

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        # False, never True: a tracer that suppressed an exception would be
        # silently swallowing the failures it exists to record.
        return False

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        """Discard the score."""


# --------------------------------------------------------------------------- #
# The Langfuse backend
# --------------------------------------------------------------------------- #


class _SpanScope:
    """Ties a backend span's lifetime to a ``with`` block.

    The SDK's span object is not itself a context manager in every version, and a
    span that is never ended is a trace that never appears -- so the scoping is
    owned here rather than assumed of the client.

    An exception inside the block is recorded (``outcome="error"``) and then
    re-raised. Recording *and* propagating is the point: the trace explains the
    failure, it does not absorb it.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Span) -> None:
        self._span = span

    def __enter__(self) -> Span:
        return self._span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        # Ending a span must not be able to raise into a customer's run, so the
        # backend call is suppressed rather than allowed to replace the real
        # exception with a tracing one.
        with suppress(Exception):
            if exc is not None:
                self._span.end(outcome="error", error=f"{type(exc).__name__}: {exc}")
            else:
                self._span.end()
        return False


class LangfuseTracer:
    """Sends spans and scores to Langfuse.

    The client is injectable, which is what lets this class be tested at all
    while ``langfuse`` is unavailable, and what makes adapting to a pinned SDK
    version a change in one place. Without one, it is imported lazily in the
    constructor so that a process with no Langfuse keys never imports the SDK --
    the same reason ``llm.router.build_providers`` imports its adapters locally.
    """

    def __init__(
        self,
        *,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
        capture_text: bool = False,
        client: LangfuseClient | None = None,
    ) -> None:
        if client is None:
            client = self._build_client(public_key=public_key, secret_key=secret_key, host=host)

        self._client = client
        self._capture_text = capture_text
        #: How many backend calls were swallowed because the client failed. Read
        #: it to report degradation rather than guessing why a trace is missing.
        self.degraded_calls = 0

    @staticmethod
    def _build_client(
        *,
        public_key: str | None,
        secret_key: str | None,
        host: str | None,
    ) -> LangfuseClient:
        if importlib.util.find_spec(LANGFUSE_PACKAGE) is None:
            raise TracingUnavailableError()

        # Imported here, not at module scope: an unconfigured process must not
        # pay an SDK import for a backend it will never call.
        from langfuse import Langfuse  # type: ignore[import-not-found]

        built: LangfuseClient = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        return built

    @property
    def capture_text(self) -> bool:
        """Whether prompt and completion text is being sent to Langfuse."""
        return self._capture_text

    def span(self, name: str, **fields: object) -> AbstractContextManager[Span]:
        """Open a Langfuse span, redacting text first."""
        safe = redact_text_fields(fields, capture_text=self._capture_text)
        try:
            started = self._client.start_span(name=name, metadata=safe)
        except Exception:  # an outage must cost traces, not content
            self.degraded_calls += 1
            return NoopTracer().span(name)
        return _SpanScope(started)

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        """Attach a score to a trace: a human rating, or a rubric result."""
        try:
            self._client.create_score(
                trace_id=trace_id,
                name=name,
                value=value,
                comment=comment,
            )
        except Exception:  # as above: degrade, never raise
            self.degraded_calls += 1


# --------------------------------------------------------------------------- #
# Factory and status
# --------------------------------------------------------------------------- #


class TracingStatus(BaseModel):
    """Whether tracing is really on, in a shape an API can serialise.

    Mirrors ``llm.router.ConfigStatus``: the point of both is that a UI can say
    "running without tracing" out loud instead of leaving someone to wonder why
    the Langfuse project is empty.

    ``configured`` and ``package_installed`` are separate fields because they are
    separate failures with separate fixes, and one boolean would conflate them.
    """

    configured: bool
    package_installed: bool
    backend: Literal["langfuse", "noop", "unavailable"]
    capture_text: bool
    message: str


def _read_key(env: Mapping[str, str], name: str) -> str | None:
    """Return a usable value, treating blank and whitespace-only as absent.

    ``LANGFUSE_PUBLIC_KEY=`` in a ``.env`` file is a very common way to "unset" a
    variable; treating it as present would build a client that 401s on flush.
    """
    value = env.get(name, "").strip()
    return value or None


def _capture_text_enabled(env: Mapping[str, str]) -> bool:
    return env.get(LANGFUSE_CAPTURE_TEXT_ENV, "").strip().lower() in _TRUTHY


def get_tracer(env: Mapping[str, str] | None = None) -> Tracer:
    """The tracer this process should use.

    Both keys present -> :class:`LangfuseTracer` (which raises
    :class:`TracingUnavailableError` until the package is installed). Anything
    else -- neither key, one key, blank keys -- is the no-op. Half-configured is a
    misconfiguration, not a configuration, so it degrades rather than constructing
    a client that cannot authenticate.
    """
    environ = env if env is not None else os.environ
    public_key = _read_key(environ, LANGFUSE_PUBLIC_KEY_ENV)
    secret_key = _read_key(environ, LANGFUSE_SECRET_KEY_ENV)

    if public_key is None or secret_key is None:
        return NoopTracer()

    return LangfuseTracer(
        public_key=public_key,
        secret_key=secret_key,
        host=_read_key(environ, LANGFUSE_HOST_ENV),
        capture_text=_capture_text_enabled(environ),
    )


def tracing_status(env: Mapping[str, str] | None = None) -> TracingStatus:
    """Report what tracing is doing, without constructing a client."""
    environ = env if env is not None else os.environ
    configured = (
        _read_key(environ, LANGFUSE_PUBLIC_KEY_ENV) is not None
        and _read_key(environ, LANGFUSE_SECRET_KEY_ENV) is not None
    )
    installed = importlib.util.find_spec(LANGFUSE_PACKAGE) is not None
    capture_text = _capture_text_enabled(environ)

    if not configured:
        return TracingStatus(
            configured=False,
            package_installed=installed,
            backend="noop",
            capture_text=False,
            message=(
                "Tracing is not configured, so every span is served by the no-op "
                f"tracer and nothing leaves the process. Set {LANGFUSE_PUBLIC_KEY_ENV} "
                f"and {LANGFUSE_SECRET_KEY_ENV} to send traces to Langfuse."
            ),
        )

    if not installed:
        return TracingStatus(
            configured=True,
            package_installed=False,
            backend="unavailable",
            capture_text=capture_text,
            message=(
                f"Tracing is configured but the {LANGFUSE_PACKAGE!r} package is not "
                "installed, so no tracer can be built. It is phase-gated in "
                "pyproject.toml and lands with the Langfuse credentials."
            ),
        )

    return TracingStatus(
        configured=True,
        package_installed=True,
        backend="langfuse",
        capture_text=capture_text,
        message=(
            "Tracing is sending spans to Langfuse. Prompt and completion text is "
            + ("INCLUDED" if capture_text else "not included")
            + f" (set {LANGFUSE_CAPTURE_TEXT_ENV} to change that)."
        ),
    )
