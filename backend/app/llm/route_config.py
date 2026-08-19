"""Runtime model routing, configurable from the admin screen.

The design constraint that shapes everything here: **an empty configuration must
behave exactly as the hardcoded defaults do.** A config feature that changes behaviour
before anyone has configured anything is a regression wearing a feature's clothes — and
it means this can be deployed ahead of the admin UI without a flag.

So a stored row OVERRIDES a code default; it never replaces the mechanism. Every path
that finds nothing usable falls back, in this order:

    configured chain -> code default for the tier -> the fake chain

Never to an exception. An admin can save a route for a provider that later loses its
credential, and a run must degrade to something that works rather than dying on a
configuration change made weeks earlier by someone else.

**No API key is ever stored here.** ``base_url`` exists for Ollama, whose availability
is a reachability question rather than a credential one. A key in a database row is a
key in every backup, replica and screenshot of that table.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from backend.app.llm.contract import ModelTier, TaskClass
from backend.app.llm.router import TASK_TIERS, TIER_CHAINS, RouteEntry

logger = logging.getLogger(__name__)

#: Providers that need no credential, so availability is configuration rather than a
#: key in the environment.
KEYLESS_PROVIDERS: frozenset[str] = frozenset({"ollama", "fake"})

RouteSource = Literal["configured", "default"]


class RouteRecord(BaseModel):
    """One stored routing decision."""

    model_config = ConfigDict(frozen=True)

    task_class: TaskClass
    tier: ModelTier
    chain: list[dict[str, str]] = Field(default_factory=list)
    note: str | None = None


class ProviderSettingRecord(BaseModel):
    """Whether a provider may be used, and where to reach it."""

    model_config = ConfigDict(frozen=True)

    provider: str
    enabled: bool = True
    base_url: str | None = None
    note: str | None = None


class RouteSummary(BaseModel):
    """What the admin screen renders: the effective route and where it came from."""

    task_class: TaskClass
    tier: ModelTier
    chain: list[dict[str, str]]
    source: RouteSource
    note: str | None = None


class RouteStore(Protocol):
    """Where configuration is read from. Two methods, no writes.

    Reads and writes are separated on purpose: the resolver sits on the hot path of
    every model call and has no business being able to mutate configuration.
    """

    async def load_routes(self) -> Sequence[RouteRecord]: ...

    async def load_providers(self) -> Sequence[ProviderSettingRecord]: ...


class InMemoryRouteStore:
    """A store for tests and for a process with no database."""

    def __init__(
        self,
        routes: Iterable[RouteRecord] | None = None,
        providers: Iterable[ProviderSettingRecord] | None = None,
    ) -> None:
        self._routes = list(routes or [])
        self._providers = list(providers or [])

    async def load_routes(self) -> Sequence[RouteRecord]:
        return list(self._routes)

    async def load_providers(self) -> Sequence[ProviderSettingRecord]:
        return list(self._providers)


def _valid_entries(chain: Sequence[Mapping[str, str]]) -> tuple[RouteEntry, ...]:
    """Keep the entries that name both a provider and a model.

    A malformed entry is SKIPPED rather than fatal. The row was written by a human
    through a form; one bad line must not take a task class offline, and the surviving
    entries are still a usable fallback chain.
    """
    entries: list[RouteEntry] = []
    for item in chain:
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if provider and model:
            entries.append(RouteEntry(provider=provider, model=model))
        else:
            logger.warning("ignoring malformed model route entry: %r", dict(item))
    return tuple(entries)


class RouteResolver:
    """Reads configuration once, then answers from memory.

    Cached rather than queried per call: a model call already costs a network round
    trip, and adding a database round trip to decide which model to use would be a
    second one for a value that changes a few times a year. :meth:`refresh` is the
    explicit reload, called at startup and after an admin edit.
    """

    def __init__(self, store: RouteStore) -> None:
        self._store = store
        self._routes: dict[TaskClass, RouteRecord] = {}
        self._providers: dict[str, ProviderSettingRecord] = {}

    async def refresh(self) -> None:
        """Reload configuration. Safe to call at any time."""
        self._routes = {r.task_class: r for r in await self._store.load_routes()}
        self._providers = {p.provider: p for p in await self._store.load_providers()}

    # -- routing ---------------------------------------------------------- #

    def tier_for_task(self, task: TaskClass) -> ModelTier:
        record = self._routes.get(task)
        return record.tier if record else TASK_TIERS[task]

    def chain_for_task(self, task: TaskClass) -> tuple[RouteEntry, ...]:
        """The effective chain: configured if usable, else the tier default.

        An empty or wholly-malformed configured chain falls through to the default
        rather than disabling the task — saving an empty list in a form must not
        silently stop generation.
        """
        record = self._routes.get(task)
        if record is not None:
            entries = _valid_entries(record.chain)
            if entries:
                return entries
        return self.chain_for_tier(self.tier_for_task(task))

    def chain_for_tier(self, tier: ModelTier) -> tuple[RouteEntry, ...]:
        return TIER_CHAINS[tier]

    # -- providers -------------------------------------------------------- #

    def is_disabled(self, provider: str) -> bool:
        setting = self._providers.get(provider)
        return setting is not None and not setting.enabled

    def base_url(self, provider: str) -> str | None:
        setting = self._providers.get(provider)
        return setting.base_url if setting else None

    def keyless_available(self) -> frozenset[str]:
        """Keyless providers an admin has switched on and given an address.

        Ollama needs no credential, so the environment cannot tell us whether it is
        usable — only the configuration can.
        """
        return frozenset(
            name
            for name, setting in self._providers.items()
            if name in KEYLESS_PROVIDERS and setting.enabled and setting.base_url
        )

    # -- admin ------------------------------------------------------------ #

    def describe(self) -> list[RouteSummary]:
        """Every task class with its effective route, for the admin screen.

        Includes unconfigured tasks, marked ``default``, so the screen shows the whole
        picture rather than only the rows someone happened to edit.
        """
        summaries: list[RouteSummary] = []
        for task in TaskClass:
            record = self._routes.get(task)
            configured = record is not None and bool(_valid_entries(record.chain))
            chain = self.chain_for_task(task)
            summaries.append(
                RouteSummary(
                    task_class=task,
                    tier=self.tier_for_task(task),
                    chain=[{"provider": e.provider, "model": e.model} for e in chain],
                    source="configured" if configured else "default",
                    note=record.note if record else None,
                )
            )
        return summaries

    def provider_settings(self) -> list[ProviderSettingRecord]:
        return list(self._providers.values())


__all__ = [
    "KEYLESS_PROVIDERS",
    "InMemoryRouteStore",
    "ProviderSettingRecord",
    "RouteRecord",
    "RouteResolver",
    "RouteSource",
    "RouteStore",
    "RouteSummary",
]
