"""DB-backed model routing.

Written before the store. The property that makes this safe to ship is the one tested
first: an EMPTY configuration must behave exactly as the hardcoded defaults do. A
config feature that changes behaviour before anyone configures anything is a
regression dressed as a feature.
"""

import pytest

from backend.app.llm import ModelTier, TaskClass
from backend.app.llm.route_config import (
    InMemoryRouteStore,
    ProviderSettingRecord,
    RouteRecord,
    RouteResolver,
)
from backend.app.llm.router import TIER_CHAINS, ModelRouter


def _available_default(tier: ModelTier, *, providers: set[str]) -> list[tuple[str, str]]:
    """The default chain for a tier, minus entries whose provider is not usable.

    resolve() filters the chain by availability, so a test comparing against the raw
    TIER_CHAINS constant asserts something the router never promises.
    """
    return [(e.provider, e.model) for e in TIER_CHAINS[tier] if e.provider in providers]


def _router(store: InMemoryRouteStore | None = None, **kw: object) -> ModelRouter:
    resolver = RouteResolver(store) if store is not None else None
    return ModelRouter(env={"OPENROUTER_API_KEY": "x"}, resolver=resolver, **kw)  # type: ignore[arg-type]


async def test_an_empty_store_leaves_the_code_defaults_untouched() -> None:
    """The safety property. Ship this with an empty table and nothing changes."""
    store = InMemoryRouteStore()
    await (resolver := RouteResolver(store)).refresh()

    for tier, expected in TIER_CHAINS.items():
        assert resolver.chain_for_tier(tier) == expected


async def test_a_configured_route_overrides_the_default_chain() -> None:
    """Uses a credentialled provider with a non-default model, so this isolates the
    override from the availability filter — routing to a provider that is not usable is
    a different behaviour, covered separately below."""
    store = InMemoryRouteStore(
        routes=[
            RouteRecord(
                task_class=TaskClass.GENERATE,
                tier=ModelTier.STRONG,
                chain=[{"provider": "openrouter", "model": "some/other-model"}],
            )
        ]
    )
    router = _router(store)
    await router.refresh_routes()

    route = router.resolve(TaskClass.GENERATE)
    assert [(e.provider, e.model) for e in route.chain] == [("openrouter", "some/other-model")]


async def test_an_unconfigured_task_still_uses_its_default() -> None:
    """Configuring one task must not blank the others."""
    store = InMemoryRouteStore(
        routes=[
            RouteRecord(
                task_class=TaskClass.GENERATE,
                tier=ModelTier.STRONG,
                chain=[{"provider": "ollama", "model": "llama3.1:70b"}],
            )
        ]
    )
    router = _router(store)
    await router.refresh_routes()

    plan = router.resolve(TaskClass.PLAN)
    assert [(e.provider, e.model) for e in plan.chain] == _available_default(
        ModelTier.MID, providers={"openrouter"}
    )


async def test_a_route_naming_an_unavailable_provider_falls_back_rather_than_failing() -> None:
    """An admin can save a route for a provider that later loses its credential. The
    run must degrade to something that works, not die."""
    store = InMemoryRouteStore(
        routes=[
            RouteRecord(
                task_class=TaskClass.CLASSIFY,
                tier=ModelTier.CHEAP,
                chain=[{"provider": "anthropic", "model": "claude-haiku-4-5"}],
            )
        ]
    )
    # Only openrouter is credentialled.
    router = _router(store)
    await router.refresh_routes()

    route = router.resolve(TaskClass.CLASSIFY)
    assert route.using_fake is True, "no usable entry should resolve to the fake chain"


async def test_an_empty_chain_is_ignored_rather_than_disabling_the_task() -> None:
    """Saving an empty list must not silently stop a task class from working."""
    store = InMemoryRouteStore(
        routes=[RouteRecord(task_class=TaskClass.PLAN, tier=ModelTier.MID, chain=[])]
    )
    router = _router(store)
    await router.refresh_routes()

    assert [(e.provider, e.model) for e in router.resolve(TaskClass.PLAN).chain] == (
        _available_default(ModelTier.MID, providers={"openrouter"})
    )


async def test_a_malformed_chain_entry_is_skipped_not_fatal() -> None:
    store = InMemoryRouteStore(
        routes=[
            RouteRecord(
                task_class=TaskClass.PLAN,
                tier=ModelTier.MID,
                chain=[
                    {"provider": "openrouter"},
                    {"model": "x"},
                    {"provider": "openrouter", "model": "openai/gpt-4.1"},
                ],
            )
        ]
    )
    router = _router(store)
    await router.refresh_routes()

    assert [(e.provider, e.model) for e in router.resolve(TaskClass.PLAN).chain] == [
        ("openrouter", "openai/gpt-4.1")
    ]


async def test_a_disabled_provider_is_removed_from_every_chain() -> None:
    """Turning a provider off in admin must take effect everywhere at once."""
    store = InMemoryRouteStore(
        providers=[ProviderSettingRecord(provider="openrouter", enabled=False)]
    )
    router = _router(store)
    await router.refresh_routes()

    assert router.resolve(TaskClass.PLAN).using_fake is True


async def test_ollama_is_available_from_its_base_url_without_a_key() -> None:
    """A local provider has no credential, so availability is configuration, not a key."""
    store = InMemoryRouteStore(
        providers=[
            ProviderSettingRecord(
                provider="ollama", enabled=True, base_url="http://localhost:11434/v1"
            )
        ],
        routes=[
            RouteRecord(
                task_class=TaskClass.CLASSIFY,
                tier=ModelTier.CHEAP,
                chain=[{"provider": "ollama", "model": "llama3.1:8b"}],
            )
        ],
    )
    # No API keys at all.
    resolver = RouteResolver(store)
    router = ModelRouter(env={}, resolver=resolver)
    await router.refresh_routes()

    route = router.resolve(TaskClass.CLASSIFY)
    assert route.using_fake is False, "ollama should be usable with no key"
    assert route.chain[0].provider == "ollama"


async def test_the_resolver_reports_what_is_overridden_for_the_admin_screen() -> None:
    store = InMemoryRouteStore(
        routes=[
            RouteRecord(
                task_class=TaskClass.GENERATE,
                tier=ModelTier.STRONG,
                chain=[{"provider": "ollama", "model": "x"}],
                note="cost trial",
            )
        ]
    )
    resolver = RouteResolver(store)
    await resolver.refresh()

    summary = resolver.describe()
    generate = next(r for r in summary if r.task_class == TaskClass.GENERATE)
    assert generate.source == "configured"
    assert generate.note == "cost trial"
    plan = next(r for r in summary if r.task_class == TaskClass.PLAN)
    assert plan.source == "default"


@pytest.mark.parametrize("task", list(TaskClass))
async def test_every_task_class_resolves_with_or_without_configuration(task: TaskClass) -> None:
    router = _router(InMemoryRouteStore())
    await router.refresh_routes()
    assert router.resolve(task).chain, f"{task} resolved to nothing"
