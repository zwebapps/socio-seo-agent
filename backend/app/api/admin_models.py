"""Admin API for model routing and provider availability.

Lets an operator change which model serves which task, and turn a provider on or off,
**without a redeploy** — the config-in-the-database rule from CLAUDE.md.

Three rules this module enforces, each because the alternative is expensive:

1. **Authentication is required on every route.** Model choice moves real money: an
   anonymous caller who could point GENERATE at the most expensive model available
   would be spending the operator's budget.
2. **An API key is REFUSED, not ignored.** Silently dropping a key from a request body
   would teach a user that pasting one here is acceptable. Keys stay in the injected
   environment; a key in this table is a key in every backup, replica and screenshot.
3. **An unknown provider name is rejected.** A typo would store a route that resolves
   to nothing, and the run would quietly fall back to the fake provider — visible only
   as mysteriously worse output.
"""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from backend.app.agents.state import DEFAULT_MAX_USD
from backend.app.agents.tools import ACTUATOR_TOOLS, NODE_TOOLS
from backend.app.api.auth import CurrentUser
from backend.app.db.adapters.route_store import (
    PostgresRouteStore,
    PostgresToolPolicyStore,
    RouteConfigWriter,
    ToolPolicyWriter,
)
from backend.app.db.models import Role, User
from backend.app.llm import ModelTier, TaskClass, config_status
from backend.app.llm.catalogue import CatalogueModel, list_models
from backend.app.llm.pricing import compute_usd, format_usd, is_priced
from backend.app.llm.route_config import KEYLESS_PROVIDERS, RouteResolver
from backend.app.llm.router import DEFAULT_MAX_OUTPUT_TOKENS
from backend.app.llm.sampling import (
    MAX_TOKENS_MAX,
    MAX_TOKENS_MIN,
    MAX_TOKENS_STEP,
    REFERENCE_ARTICLE_CHARS,
    TEMPERATURE_MAX,
    TEMPERATURE_MIN,
    TEMPERATURE_STEP,
    SamplingBoundsError,
    rejects_sampling,
    tokens_for_article,
    validate_max_output_tokens,
    validate_temperature,
)
from backend.app.services.prompt_inventory import (
    EVAL_HARNESS_NOTE,
    PromptSurface,
    graph_node_count,
    prompt_surfaces,
    task_class_count,
)
from backend.app.services.tool_policy import (
    RUNTIME_ENFORCED,
    NodeToolPolicyRecord,
    NodeToolView,
    describe_node_tools,
    effective_tools,
    revocable_tools,
    unknown_tool_names,
)

router = APIRouter(prefix="/api/v1/admin/models", tags=["admin"])

#: The providers this build knows how to talk to. A name outside this set is a typo,
#: not a feature request, and storing it would route a task to nothing.
KNOWN_PROVIDERS: frozenset[str] = frozenset({"openrouter", "anthropic", "ollama", "fake"})


class Writer(Protocol):
    async def set_route(
        self,
        *,
        task_class: str,
        tier: str,
        chain: Sequence[dict[str, str]],
        updated_by: UUID | None = ...,
        note: str | None = ...,
    ) -> None: ...

    async def clear_route(self, task_class: str) -> None: ...

    async def set_provider(
        self,
        *,
        provider: str,
        enabled: bool,
        base_url: str | None = ...,
        updated_by: UUID | None = ...,
        note: str | None = ...,
    ) -> None: ...


class SamplingWriter(Protocol):
    async def set_sampling(
        self,
        *,
        task_class: str,
        temperature: Decimal | None,
        max_output_tokens: int | None,
        updated_by: UUID | None = ...,
        note: str | None = ...,
    ) -> None: ...

    async def clear_sampling(self, task_class: str) -> None: ...


class ToolWriter(Protocol):
    async def set_revoked(
        self,
        *,
        node: str,
        revoked: Sequence[str],
        updated_by: UUID | None = ...,
        note: str | None = ...,
    ) -> None: ...


class ToolPolicyReader(Protocol):
    async def load_policies(self) -> Sequence[NodeToolPolicyRecord]: ...


def get_writer() -> Writer:
    """The write side. Overridden in tests."""
    return RouteConfigWriter()


def get_sampling_writer() -> SamplingWriter:
    """The sampling write side.

    A separate dependency from :func:`get_writer` rather than two more methods on the
    `Writer` protocol: the two feature areas are overridden independently in tests, and
    widening a protocol every existing double already satisfies is how a test double
    quietly stops representing the thing it doubles.
    """
    return RouteConfigWriter()


def get_tool_writer() -> ToolWriter:
    """The tool-revocation write side. Overridden in tests.

    Note the shape of what it can do: `set_revoked` and nothing else. There is no
    method here through which a tool could be GRANTED -- see `services/tool_policy.py`.
    """
    return ToolPolicyWriter()


def get_tool_policies() -> ToolPolicyReader:
    """The tool-revocation read side. Overridden in tests."""
    return PostgresToolPolicyStore()


async def get_resolver() -> RouteResolver:
    """A resolver reflecting what is currently stored. Overridden in tests."""
    resolver = RouteResolver(PostgresRouteStore())
    await resolver.refresh()
    return resolver


async def require_admin(user: CurrentUser) -> User:
    """Platform-admin gate that also yields the author.

    Returns the caller rather than None so a route needs ONE dependency instead of two:
    asking for `CurrentUser` again alongside this would put the auth check in every
    signature twice, and an `Optional[CurrentUser]` parameter is not a shape FastAPI can
    build a response model from.

    **403, not 401, for a signed-in user with the wrong role.** The two are not
    interchangeable: 401 means "go and sign in", which for someone already signed in is
    a loop they cannot escape. 403 means "I know who you are, and no".

    Deactivation beats role: an inactive account is refused whatever it is. `current_user`
    already rejects an inactive session, and this repeats the check because a future
    caller might resolve the user some other way, and the cost of the extra comparison is
    nothing next to the cost of missing it.

    The refusal deliberately does NOT say "you need platform admin". Naming the
    capability tells a customer exactly what to go phishing for, and it is not
    information they can act on legitimately.
    """
    if not user.is_active or user.role != Role.PLATFORM_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": "Your account cannot change these settings.",
            },
        )
    return user


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChainEntryIn(BaseModel):
    provider: str
    model: str

    @field_validator("provider")
    @classmethod
    def _known(cls, value: str) -> str:
        name = value.strip().lower()
        if name not in KNOWN_PROVIDERS:
            raise ValueError(
                f"unknown provider {name!r}; expected one of {sorted(KNOWN_PROVIDERS)}"
            )
        return name

    @field_validator("model")
    @classmethod
    def _present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()


class RouteIn(BaseModel):
    tier: ModelTier
    chain: list[ChainEntryIn] = Field(min_length=1)
    note: str | None = None


class RouteOut(CamelModel):
    task_class: str
    tier: str
    chain: list[dict[str, str]]
    source: str
    note: str | None = None
    #: False when any model in the chain is absent from the price table, so the UI can
    #: warn instead of showing $0.00 for real spend.
    cost_reportable: bool


class RoutesOut(CamelModel):
    routes: list[RouteOut]


class ProviderOut(CamelModel):
    provider: str
    enabled: bool
    available: bool
    requires_key: bool
    base_url: str | None = None
    note: str | None = None


class ProvidersOut(CamelModel):
    providers: list[ProviderOut]


class ProviderIn(BaseModel):
    enabled: bool
    base_url: str | None = None
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_credentials(cls, data: Any) -> Any:
        """Refuse a credential outright rather than dropping it quietly."""
        if isinstance(data, dict):
            offending = [
                key
                for key in data
                if any(word in key.lower() for word in ("key", "secret", "token", "password"))
            ]
            if offending:
                raise ValueError(
                    "credentials are not stored in the database. Remove "
                    f"{sorted(offending)} and set the value in the environment instead."
                )
        return data


def _task_class(raw: str) -> TaskClass:
    try:
        return TaskClass(raw.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unknown_task_class",
                "message": f"unknown task class {raw!r}; expected one of "
                f"{[t.value for t in TaskClass]}",
            },
        ) from exc


@router.get("/routes", response_model=RoutesOut, response_model_by_alias=True)
async def list_routes(
    _: Annotated[User, Depends(require_admin)],
    resolver: Annotated[RouteResolver, Depends(get_resolver)],
) -> RoutesOut:
    """Every task class with its EFFECTIVE route and where that route came from."""
    return RoutesOut(
        routes=[
            RouteOut(
                task_class=s.task_class.value,
                tier=s.tier.value,
                chain=s.chain,
                source=s.source,
                note=s.note,
                cost_reportable=all(is_priced(e["model"]) for e in s.chain),
            )
            for s in resolver.describe()
        ]
    )


@router.put("/routes/{task_class}")
async def put_route(
    task_class: str,
    payload: RouteIn,
    admin: Annotated[User, Depends(require_admin)],
    writer: Annotated[Writer, Depends(get_writer)],
) -> dict[str, str]:
    """Point one task class at a specific chain of models."""
    task = _task_class(task_class)
    await writer.set_route(
        task_class=task.value,
        tier=payload.tier.value,
        chain=[{"provider": e.provider, "model": e.model} for e in payload.chain],
        updated_by=admin.id,
        note=payload.note,
    )
    return {"status": "saved", "taskClass": task.value}


@router.delete("/routes/{task_class}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_route(
    task_class: str,
    _: Annotated[User, Depends(require_admin)],
    writer: Annotated[Writer, Depends(get_writer)],
) -> Response:
    """Revert one task class to the code default."""
    task = _task_class(task_class)
    await writer.clear_route(task.value)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/providers", response_model=ProvidersOut, response_model_by_alias=True)
async def list_providers(
    _: Annotated[User, Depends(require_admin)],
    resolver: Annotated[RouteResolver, Depends(get_resolver)],
) -> ProvidersOut:
    """Provider availability, and what each one needs in order to be usable.

    `available` answers a different question per provider: a hosted one needs a
    credential in the environment, while Ollama needs an address, because a local server
    has no key to check.
    """
    status_report = config_status()
    stored = {p.provider: p for p in resolver.provider_settings()}
    keyless_ready = resolver.keyless_available()

    out: list[ProviderOut] = []
    for name in sorted(KNOWN_PROVIDERS):
        setting = stored.get(name)
        enabled = setting.enabled if setting else True
        requires_key = name not in KEYLESS_PROVIDERS
        if name == "fake":
            available = True
        elif requires_key:
            available = name in status_report.available_providers
        else:
            available = name in keyless_ready
        out.append(
            ProviderOut(
                provider=name,
                enabled=enabled,
                available=available and enabled,
                requires_key=requires_key,
                base_url=setting.base_url if setting else None,
                note=setting.note if setting else None,
            )
        )
    return ProvidersOut(providers=out)


@router.put("/providers/{provider}")
async def put_provider(
    provider: str,
    payload: ProviderIn,
    admin: Annotated[User, Depends(require_admin)],
    writer: Annotated[Writer, Depends(get_writer)],
) -> dict[str, str]:
    """Turn a provider on or off, and set its address if it is a local one."""
    name = provider.strip().lower()
    if name not in KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unknown_provider",
                "message": f"unknown provider {name!r}; expected one of {sorted(KNOWN_PROVIDERS)}",
            },
        )
    await writer.set_provider(
        provider=name,
        enabled=payload.enabled,
        base_url=payload.base_url,
        updated_by=admin.id,
        note=payload.note,
    )
    return {"status": "saved", "provider": name}


class CatalogueOut(CamelModel):
    provider: str
    models: list[CatalogueModel]
    #: True when the list came from the provider itself rather than from our price
    #: table. The screen should say so: a fallback list is not "everything available".
    live: bool
    message: str | None = None


@router.get("/available", response_model=CatalogueOut, response_model_by_alias=True)
async def available_models(
    _: Annotated[User, Depends(require_admin)],
    resolver: Annotated[RouteResolver, Depends(get_resolver)],
    provider: str = "openrouter",
) -> CatalogueOut:
    """Models an admin can actually pick, for one provider.

    Never raises on an unreachable provider: it falls back to what we know from the
    price table and says so. An admin screen that 500s because a local Ollama is not
    running is a bad screen — and the operator most likely opened it *in order to*
    configure that provider.
    """
    name = provider.strip().lower()
    if name not in KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unknown_provider",
                "message": f"unknown provider {name!r}; expected one of {sorted(KNOWN_PROVIDERS)}",
            },
        )

    configured_url = resolver.base_url(name)
    message: str | None = None

    # A keyless provider with no configured address is NOT probed. Defaulting to
    # localhost would mean the app reaches out to a machine the operator never pointed
    # it at -- and it made this endpoint's own test non-hermetic by finding a real
    # Ollama server on the developer's laptop.
    if name in KEYLESS_PROVIDERS and name != "fake" and not configured_url:
        return CatalogueOut(
            provider=name,
            models=[],
            live=False,
            message=(
                "No address is configured for this provider, so nothing was queried. "
                "Set its base URL first (Ollama's default is "
                "http://localhost:11434/v1), then reload."
            ),
        )

    models = await list_models(name, base_url=configured_url)
    live = any(m.note is None or "price table" not in (m.note or "") for m in models)

    if name in KEYLESS_PROVIDERS and not models:
        # Empty is ambiguous here and the difference matters to whoever is looking:
        # a server that is off and a server with nothing pulled look identical.
        message = (
            "No local models found. Either the server is not reachable at the "
            "configured address, or no model has been pulled yet (`ollama pull llama3.1`)."
        )
    elif not any(m.priced for m in models):
        message = (
            "None of these models is in our price table, so cost cannot be reported for "
            "them. Spend will show as $0.00 even when it is not zero."
        )

    return CatalogueOut(provider=name, models=models, live=live, message=message)


# --------------------------------------------------------------------------- #
# Sampling: temperature and the output ceiling, per task class
# --------------------------------------------------------------------------- #


class SamplingBounds(CamelModel):
    """What the sliders may offer, and WHY those are the limits.

    Sent to the client rather than duplicated there. A frontend with its own copy of
    `min`/`max` is a second source of truth for a security-adjacent limit, and the day
    they disagree the server refuses a value the UI happily produced.
    """

    temperature_min: float
    temperature_max: float
    temperature_step: float
    temperature_reason: str

    max_tokens_min: int
    max_tokens_max: int
    max_tokens_step: int
    max_tokens_reason: str


def _bounds() -> SamplingBounds:
    return SamplingBounds(
        temperature_min=float(TEMPERATURE_MIN),
        temperature_max=float(TEMPERATURE_MAX),
        temperature_step=float(TEMPERATURE_STEP),
        temperature_reason=(
            f"Capped at {TEMPERATURE_MAX} because that is the maximum the STRICTEST "
            "adapter accepts: the Anthropic Messages API takes 0-1 while the "
            "OpenAI-compatible surfaces take 0-2. A control that could emit 1.7 would "
            "produce a 400 the moment a chain fell back to Anthropic. Nothing useful "
            "is lost above it -- marketing copy degrades into invented specifics well "
            "below 1.2, and this pipeline turns invented specifics into claim-gate "
            "refusals rather than flair."
        ),
        max_tokens_min=MAX_TOKENS_MIN,
        max_tokens_max=MAX_TOKENS_MAX,
        max_tokens_step=MAX_TOKENS_STEP,
        max_tokens_reason=(
            f"Floor of {MAX_TOKENS_MIN} because a {REFERENCE_ARTICLE_CHARS}-character "
            f"German article needs about {tokens_for_article()} output tokens once it "
            "is HTML inside a JSON tool-call argument -- prose at four characters per "
            "token, markup at one and a half, plus the title and meta description. A "
            "lower ceiling truncates the JSON argument, which does not parse, so the "
            f"node gets NO page rather than a short one. Ceiling of {MAX_TOKENS_MAX} "
            "because the pre-call budget guard reserves the FULL allowance on every "
            "call, so raising this buys refusals, not longer articles."
        ),
    )


class SamplingOut(CamelModel):
    task_class: str
    tier: str
    #: None means "send nothing, take the provider default" -- which is what every call
    #: site does today and what an unconfigured task does.
    temperature: float | None = None
    max_output_tokens: int | None = None
    source: str
    note: str | None = None

    #: Models in this task's EFFECTIVE chain that reject `temperature` outright. When
    #: this is non-empty the screen must say so: a stored temperature is skipped for
    #: those entries rather than failing the call, so setting one here has no effect on
    #: them and the operator would otherwise be tuning a control that does nothing.
    models_rejecting_temperature: list[str]
    #: True when EVERY entry in the chain rejects it, so the temperature control is
    #: inert for this task.
    temperature_inert: bool

    #: USD the budget guard reserves per call at the effective ceiling, for the chain's
    #: first entry. This is the consequence of the number, and it is why the ceiling is
    #: shown with money next to it rather than on its own. None when the model has no
    #: price-table entry, because a zero there would be a lie.
    reserved_usd_per_call: str | None = None
    #: How many such calls fit inside one run at the default run cap. The honest unit:
    #: an operator raising a ceiling is spending run headroom, not tokens.
    calls_within_run_cap: int | None = None


class SamplingListOut(CamelModel):
    bounds: SamplingBounds
    #: The run ceiling the reservation above is measured against.
    run_cap_usd: str
    sampling: list[SamplingOut]


class SamplingIn(BaseModel):
    """A sampling change. Extra fields are refused rather than ignored.

    `extra="forbid"` because a client sending `topP` or `maxTokens` (rather than
    `maxOutputTokens`) would otherwise get a 200 for a request that changed nothing --
    the single most confusing outcome a settings API can produce.
    """

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    temperature: Decimal | None = None
    max_output_tokens: int | None = None
    note: str | None = None


def _reservation(model: str, max_tokens: int | None) -> tuple[str | None, int | None]:
    """What the guard reserves for one call at this ceiling, and how many fit in a run.

    Input tokens are deliberately EXCLUDED: this is the marginal cost of the ceiling
    being moved, and mixing in a guess at prompt size would make the number look precise
    when the prompt is the part we cannot know here.
    """
    if not is_priced(model):
        return None, None
    tokens = max_tokens if max_tokens is not None else DEFAULT_MAX_OUTPUT_TOKENS
    reserved = compute_usd(model, 0, tokens)
    fits = int(DEFAULT_MAX_USD / reserved) if reserved > 0 else None
    return format_usd(reserved), fits


@router.get("/sampling", response_model=SamplingListOut, response_model_by_alias=True)
async def list_sampling(
    _: Annotated[User, Depends(require_admin)],
    resolver: Annotated[RouteResolver, Depends(get_resolver)],
) -> SamplingListOut:
    """Every task class with its sampling policy, its bounds and its consequences."""
    out: list[SamplingOut] = []
    for record in resolver.describe_sampling():
        task = record.task_class
        chain = resolver.chain_for_task(task)
        refusing = sorted({e.model for e in chain if rejects_sampling(e.model)})
        first = chain[0].model if chain else ""
        reserved, fits = _reservation(first, record.max_output_tokens)
        out.append(
            SamplingOut(
                task_class=task.value,
                tier=resolver.tier_for_task(task).value,
                temperature=float(record.temperature) if record.temperature is not None else None,
                max_output_tokens=record.max_output_tokens,
                source="default" if record.is_empty else "configured",
                note=record.note,
                models_rejecting_temperature=refusing,
                temperature_inert=bool(chain) and len(refusing) == len(chain),
                reserved_usd_per_call=reserved,
                calls_within_run_cap=fits,
            )
        )
    return SamplingListOut(bounds=_bounds(), run_cap_usd=str(DEFAULT_MAX_USD), sampling=out)


@router.put("/sampling/{task_class}")
async def put_sampling(
    task_class: str,
    payload: SamplingIn,
    admin: Annotated[User, Depends(require_admin)],
    writer: Annotated[SamplingWriter, Depends(get_sampling_writer)],
) -> dict[str, str]:
    """Set the temperature and output ceiling for one task class.

    Both values null clears the policy, which is the same thing as never having set one:
    nothing is sent and the provider default applies.
    """
    task = _task_class(task_class)
    try:
        temperature = (
            validate_temperature(payload.temperature) if payload.temperature is not None else None
        )
        max_tokens = (
            validate_max_output_tokens(payload.max_output_tokens)
            if payload.max_output_tokens is not None
            else None
        )
    except (SamplingBoundsError, InvalidOperation) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "sampling_out_of_range", "message": str(exc)},
        ) from exc

    await writer.set_sampling(
        task_class=task.value,
        temperature=temperature,
        max_output_tokens=max_tokens,
        updated_by=admin.id,
        note=payload.note,
    )
    return {"status": "saved", "taskClass": task.value}


@router.delete("/sampling/{task_class}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sampling(
    task_class: str,
    _: Annotated[User, Depends(require_admin)],
    writer: Annotated[SamplingWriter, Depends(get_sampling_writer)],
) -> Response:
    """Revert one task class to the provider defaults."""
    task = _task_class(task_class)
    await writer.clear_sampling(task.value)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Tool toggles: revocation only
# --------------------------------------------------------------------------- #


class ToolsOut(CamelModel):
    """The tool-toggle screen's whole state.

    `granted` on each node is READ-ONLY and is the ceiling. There is no request shape in
    this module that can raise it -- see `ToolsIn` and `services/tool_policy.py`.
    """

    nodes: list[NodeToolView]
    #: Names that reach the outside world. Revoking one of these is a kill switch, which
    #: the screen marks differently from switching off a search tool.
    actuator_tools: list[str]
    #: False while a stored revocation is not yet read by the running graph. Reported so
    #: the screen can say it rather than implying a kill switch is armed when it is not.
    enforced: bool
    #: Why the screen offers no way to GRANT a tool.
    policy: str


class ToolsIn(BaseModel):
    """A revocation set. There is deliberately no `granted` field.

    `extra="forbid"` turns the absence into a refusal: a caller who sends
    `{"revoked": [], "granted": ["publish"]}` gets a 422, not a silent success. Without
    it, a request that looks like it grants a capability would be accepted and quietly
    ignored, and the caller would have every reason to believe it worked.
    """

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    revoked: list[str] = Field(default_factory=list)
    note: str | None = None


@router.get("/tools", response_model=ToolsOut, response_model_by_alias=True)
async def list_tools(
    _: Annotated[User, Depends(require_admin)],
    policies: Annotated[ToolPolicyReader, Depends(get_tool_policies)],
) -> ToolsOut:
    """Every node, what the code grants it, and what an operator has switched off."""
    stored = {p.node: p.revoked for p in await policies.load_policies()}
    return ToolsOut(
        nodes=describe_node_tools(stored),
        actuator_tools=sorted(ACTUATOR_TOOLS),
        enforced=RUNTIME_ENFORCED,
        policy=(
            "Tools can be switched OFF here and cannot be switched on. The per-node "
            "allowlist in backend/app/agents/tools.py is a prompt-injection barrier "
            "(docs/AGENT_RUNTIME.md section 3), and the effective set is that allowlist "
            "MINUS what is revoked here -- a set difference, so nothing stored can add a "
            "capability. Granting is a code change so that it goes through review and "
            "the injection corpus, which a form cannot do."
        ),
    )


@router.put("/tools/{node}")
async def put_tools(
    node: str,
    payload: ToolsIn,
    admin: Annotated[User, Depends(require_admin)],
    writer: Annotated[ToolWriter, Depends(get_tool_writer)],
) -> dict[str, Any]:
    """Set which of `node`'s granted tools are switched off.

    Two refusals, and both are about not lying to the operator rather than about
    security -- the set difference already makes widening impossible:

    * an unknown NODE is refused, because a policy row for a node that does not exist is
      a control that can never do anything;
    * an unknown TOOL NAME is refused, because a typo would be silently inert and the
      operator would believe they had switched something off.
    """
    name = node.strip().upper()
    granted = revocable_tools(name)
    if name not in NODE_TOOLS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unknown_node",
                "message": f"unknown node {name!r}; expected one of {sorted(NODE_TOOLS)}",
            },
        )

    unknown = unknown_tool_names(payload.revoked)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unknown_tool",
                "message": f"unknown tool name(s) {unknown}; a typo here would be "
                "silently inert, so it is refused instead",
            },
        )

    not_held = sorted(frozenset(payload.revoked) - granted)
    if not_held:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "tool_not_granted",
                "message": f"node {name} does not hold {not_held}, so revoking it would "
                f"change nothing. It holds {sorted(granted)}.",
            },
        )

    await writer.set_revoked(
        node=name, revoked=payload.revoked, updated_by=admin.id, note=payload.note
    )
    return {
        "status": "saved",
        "node": name,
        # Echo the effect, not just an acknowledgement: the caller can then assert that
        # what it asked for is a SUBSET of what the code grants, which is the invariant.
        "effective": sorted(effective_tools(name, payload.revoked)),
    }


# --------------------------------------------------------------------------- #
# Prompt versions: an inventory, not a selector
# --------------------------------------------------------------------------- #


class PromptVersionsOut(CamelModel):
    surfaces: list[PromptSurface]
    #: True if ANY surface has more than one version to choose between. False today, and
    #: the screen uses it to render an inventory instead of a dropdown.
    selectable: bool
    eval_harness_note: str
    summary: str
    #: How many nodes the graph runs, from `graph.ORDER` itself. `None` when it could not
    #: be read, which the screen states rather than replacing with a guess.
    #:
    #: Sent SEPARATELY from `task_class_count`, and that separation is the point: these are
    #: two different concepts that were being conflated on this screen. A graph node is a
    #: step in the run; a task class is what a model call is FOR. Two nodes doing the same
    #: kind of work share one task class, so the numbers are not meant to match and a
    #: single count would invite exactly the false claim this replaced.
    graph_node_count: int | None
    #: How many model-routing task classes exist. Never the node count.
    task_class_count: int


@router.get("/prompt-versions", response_model=PromptVersionsOut, response_model_by_alias=True)
async def list_prompt_versions(
    _: Annotated[User, Depends(require_admin)],
) -> PromptVersionsOut:
    """Which prompt versions the runtime has, and whether any of them can be switched.

    The answer today is "one each, none switchable", and this endpoint states that
    rather than dressing it up. See `services/prompt_inventory.py`.
    """
    surfaces = prompt_surfaces()
    selectable = any(s.variants > 1 for s in surfaces)
    nodes, _unreadable = graph_node_count()
    return PromptVersionsOut(
        surfaces=surfaces,
        selectable=selectable,
        eval_harness_note=EVAL_HARNESS_NOTE,
        graph_node_count=nodes,
        task_class_count=task_class_count(),
        summary=(
            f"{len(surfaces)} prompt surface(s), each with exactly one version defined in "
            "code, so there is nothing here to choose between. A version is recorded on "
            "every model_usage row, so the cost screen can attribute a change to a prompt "
            "rather than to folklore -- which is what the versions are for. Introducing a "
            "second one is a code change, deliberately: a prompt is the instruction set a "
            "model receives, and swapping it in a form would put untested text in front "
            "of a customer's content."
        ),
    )
