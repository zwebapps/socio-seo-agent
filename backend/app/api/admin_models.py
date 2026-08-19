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
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from backend.app.api.auth import CurrentUser
from backend.app.db.adapters.route_store import PostgresRouteStore, RouteConfigWriter
from backend.app.llm import ModelTier, TaskClass, config_status
from backend.app.llm.pricing import is_priced
from backend.app.llm.route_config import KEYLESS_PROVIDERS, RouteResolver

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


def get_writer() -> Writer:
    """The write side. Overridden in tests."""
    return RouteConfigWriter()


async def get_resolver() -> RouteResolver:
    """A resolver reflecting what is currently stored. Overridden in tests."""
    resolver = RouteResolver(PostgresRouteStore())
    await resolver.refresh()
    return resolver


async def require_admin(user: CurrentUser) -> object:
    """Authentication gate that also yields the author.

    Returns the caller rather than None so a route needs ONE dependency instead of two:
    asking for `CurrentUser` again alongside this would make the auth check appear
    twice in every signature, and an `Optional[CurrentUser]` parameter is not a shape
    FastAPI can build a response model from.

    Currently any authenticated user, because this build has no role column yet. Kept as
    its own dependency so adding one is a change here rather than in five routes — and
    so a test overriding it makes the intent obvious.
    """
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
    _: Annotated[object, Depends(require_admin)],
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
    admin: Annotated[object, Depends(require_admin)],
    writer: Annotated[Writer, Depends(get_writer)],
) -> dict[str, str]:
    """Point one task class at a specific chain of models."""
    task = _task_class(task_class)
    await writer.set_route(
        task_class=task.value,
        tier=payload.tier.value,
        chain=[{"provider": e.provider, "model": e.model} for e in payload.chain],
        updated_by=getattr(admin, "id", None),
        note=payload.note,
    )
    return {"status": "saved", "taskClass": task.value}


@router.delete("/routes/{task_class}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_route(
    task_class: str,
    _: Annotated[object, Depends(require_admin)],
    writer: Annotated[Writer, Depends(get_writer)],
) -> Response:
    """Revert one task class to the code default."""
    task = _task_class(task_class)
    await writer.clear_route(task.value)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/providers", response_model=ProvidersOut, response_model_by_alias=True)
async def list_providers(
    _: Annotated[object, Depends(require_admin)],
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
    admin: Annotated[object, Depends(require_admin)],
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
        updated_by=getattr(admin, "id", None),
        note=payload.note,
    )
    return {"status": "saved", "provider": name}
