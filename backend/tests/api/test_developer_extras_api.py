"""The `/developer` extras over HTTP: sampling, tool toggles, prompt versions, cost.

Written for the properties that are not CRUD, because the CRUD is the least interesting
part of any settings API:

* **the platform-admin gate covers the new routes too.** A new endpoint under an
  authenticated prefix is not automatically authenticated -- the dependency has to be on
  it -- so every route added here is checked unauthenticated as well;
* **the tool endpoint cannot be talked into granting a capability.** A body carrying
  `granted` is a 422 rather than a quietly-ignored field, and the success response echoes
  the EFFECTIVE set so a caller can assert it is a subset of the code allowlist;
* **an out-of-range sampling value is refused with the bound it broke**, because
  "invalid temperature" is not something an operator can act on;
* **the sampling screen is told which models will ignore a temperature**, since the
  STRONG chain's first choice rejects the parameter outright and a slider with no warning
  would be a control that silently does nothing.
"""

from decimal import Decimal
from typing import Any

import httpx
import pytest

from backend.app.agents.tools import NODE_TOOLS, allowed_tools
from backend.app.api import admin_models as admin_api
from backend.app.db.models import Role, User
from backend.app.llm.route_config import (
    InMemoryRouteStore,
    RouteResolver,
    SamplingRecord,
)
from backend.app.llm.sampling import MAX_TOKENS_MIN
from backend.app.main import create_app
from backend.app.services.tool_policy import NodeToolPolicyRecord


class FakeSamplingWriter:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []
        self.cleared: list[str] = []

    async def set_sampling(
        self,
        *,
        task_class: str,
        temperature: Decimal | None,
        max_output_tokens: int | None,
        **kw: Any,
    ) -> None:
        self.saved.append(
            {
                "task_class": task_class,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            }
        )

    async def clear_sampling(self, task_class: str) -> None:
        self.cleared.append(task_class)


class FakeToolWriter:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    async def set_revoked(self, *, node: str, revoked: Any, **kw: Any) -> None:
        self.saved.append({"node": node, "revoked": list(revoked)})


class FakeToolPolicies:
    def __init__(self, policies: list[NodeToolPolicyRecord] | None = None) -> None:
        self._policies = policies or []

    async def load_policies(self) -> list[NodeToolPolicyRecord]:
        return list(self._policies)


def _client(
    *,
    authenticated: bool = True,
    sampling_writer: FakeSamplingWriter | None = None,
    tool_writer: FakeToolWriter | None = None,
    policies: FakeToolPolicies | None = None,
    store: InMemoryRouteStore | None = None,
) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[admin_api.get_sampling_writer] = lambda: (
        sampling_writer or FakeSamplingWriter()
    )
    app.dependency_overrides[admin_api.get_tool_writer] = lambda: tool_writer or FakeToolWriter()
    app.dependency_overrides[admin_api.get_tool_policies] = lambda: policies or FakeToolPolicies()

    # The resolver MUST be overridden or these tests reach the real database, which
    # surfaces as "Future attached to a different loop" rather than as anything resembling
    # the actual problem. Copied deliberately from test_admin_models_api.
    resolver = RouteResolver(store or InMemoryRouteStore())

    async def _resolver() -> RouteResolver:
        await resolver.refresh()
        return resolver

    app.dependency_overrides[admin_api.get_resolver] = _resolver
    if authenticated:
        app.dependency_overrides[admin_api.require_admin] = lambda: User(
            email="admin@example.test",
            password_hash="x",
            is_active=True,
            role=Role.PLATFORM_ADMIN,
        )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/admin/models/sampling", None),
        ("PUT", "/api/v1/admin/models/sampling/generate", {"temperature": 0.5}),
        ("DELETE", "/api/v1/admin/models/sampling/generate", None),
        ("GET", "/api/v1/admin/models/tools", None),
        ("PUT", "/api/v1/admin/models/tools/EXPORT", {"revoked": ["publish"]}),
        ("GET", "/api/v1/admin/models/prompt-versions", None),
        ("GET", "/api/v1/admin/cost", None),
    ],
)
async def test_every_new_route_refuses_an_unauthenticated_caller(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Being under an authenticated PREFIX is not authentication. Each route carries the
    dependency itself, and each one is checked, because forgetting it on one route is the
    failure this parametrisation exists to catch."""
    async with _client(authenticated=False) as client:
        response = await client.request(method, path, json=body)

    assert response.status_code in (401, 403), (
        f"{method} {path} answered {response.status_code} with no session"
    )


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #


async def test_sampling_lists_every_task_with_its_bounds_and_their_reasons() -> None:
    async with _client() as client:
        response = await client.get("/api/v1/admin/models/sampling")

    assert response.status_code == 200
    body = response.json()

    tasks = {row["taskClass"] for row in body["sampling"]}
    assert {"generate", "classify", "embed"} <= tasks
    assert body["bounds"]["maxTokensMin"] == MAX_TOKENS_MIN
    assert body["bounds"]["temperatureMax"] == 1.0
    # The reasons travel with the numbers so the screen never has to invent an
    # explanation for a limit it did not choose.
    assert "German" in body["bounds"]["maxTokensReason"]
    assert "Anthropic" in body["bounds"]["temperatureReason"]


async def test_an_unconfigured_task_reports_default_and_sends_nothing() -> None:
    async with _client() as client:
        body = (await client.get("/api/v1/admin/models/sampling")).json()

    generate = next(r for r in body["sampling"] if r["taskClass"] == "generate")
    assert generate["source"] == "default"
    assert generate["temperature"] is None
    assert generate["maxOutputTokens"] is None


async def test_the_screen_is_told_which_models_will_ignore_a_temperature() -> None:
    """Without this the slider is a control that silently does nothing on the one task an
    operator most wants to tune: GENERATE's first-choice model rejects `temperature`
    outright, so a stored value is skipped for it."""
    async with _client() as client:
        body = (await client.get("/api/v1/admin/models/sampling")).json()

    generate = next(r for r in body["sampling"] if r["taskClass"] == "generate")
    assert generate["modelsRejectingTemperature"], (
        "the strong chain's models accept temperature now, or the warning is not being "
        "computed -- check against MODELS_REJECTING_SAMPLING before deleting this"
    )


async def test_the_ceiling_is_reported_with_the_money_it_reserves() -> None:
    """The consequence, not just the number: the pre-call guard reserves the FULL
    allowance, so raising a ceiling spends run headroom."""
    store = InMemoryRouteStore(
        sampling=[SamplingRecord(task_class="generate", max_output_tokens=8192)]  # type: ignore[arg-type]
    )
    async with _client(store=store) as client:
        body = (await client.get("/api/v1/admin/models/sampling")).json()

    generate = next(r for r in body["sampling"] if r["taskClass"] == "generate")
    assert generate["source"] == "configured"
    assert generate["maxOutputTokens"] == 8192
    # A string, not a float: money is Decimal end to end and serialises as text.
    assert isinstance(generate["reservedUsdPerCall"], str)
    assert Decimal(generate["reservedUsdPerCall"]) > 0
    assert generate["callsWithinRunCap"] is not None
    assert Decimal(body["runCapUsd"]) > 0


async def test_a_sampling_policy_can_be_saved() -> None:
    writer = FakeSamplingWriter()
    async with _client(sampling_writer=writer) as client:
        response = await client.put(
            "/api/v1/admin/models/sampling/generate",
            json={"temperature": 0.35, "maxOutputTokens": 4096},
        )

    assert response.status_code == 200
    assert writer.saved == [
        {"task_class": "generate", "temperature": Decimal("0.35"), "max_output_tokens": 4096}
    ]


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ({"temperature": 1.5}, "sampling_out_of_range"),
        ({"temperature": -0.1}, "sampling_out_of_range"),
        ({"maxOutputTokens": 512}, "sampling_out_of_range"),
        ({"maxOutputTokens": 100000}, "sampling_out_of_range"),
    ],
)
async def test_an_out_of_range_value_is_refused_naming_the_bound(
    body: dict[str, Any], expected_code: str
) -> None:
    writer = FakeSamplingWriter()
    async with _client(sampling_writer=writer) as client:
        response = await client.put("/api/v1/admin/models/sampling/generate", json=body)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == expected_code
    assert "supported range" in detail["message"]
    assert writer.saved == [], "a refused request must not write"


async def test_a_misspelled_field_is_refused_rather_than_silently_ignored() -> None:
    """`maxTokens` instead of `maxOutputTokens` would otherwise be a 200 that changed
    nothing -- the most confusing outcome a settings API can produce."""
    writer = FakeSamplingWriter()
    async with _client(sampling_writer=writer) as client:
        response = await client.put(
            "/api/v1/admin/models/sampling/generate", json={"maxTokens": 4096}
        )

    assert response.status_code == 422
    assert writer.saved == []


async def test_an_unknown_task_class_is_refused() -> None:
    async with _client() as client:
        response = await client.put(
            "/api/v1/admin/models/sampling/not-a-task", json={"temperature": 0.5}
        )
    assert response.status_code == 422


async def test_a_sampling_policy_can_be_reverted() -> None:
    writer = FakeSamplingWriter()
    async with _client(sampling_writer=writer) as client:
        response = await client.delete("/api/v1/admin/models/sampling/generate")

    assert response.status_code == 204
    assert writer.cleared == ["generate"]


# --------------------------------------------------------------------------- #
# Tool toggles -- the security-relevant half
# --------------------------------------------------------------------------- #


async def test_the_tools_screen_reports_the_ceiling_the_policy_and_the_effect() -> None:
    policies = FakeToolPolicies([NodeToolPolicyRecord(node="GENERATE", revoked=["web_search"])])
    async with _client(policies=policies) as client:
        response = await client.get("/api/v1/admin/models/tools")

    assert response.status_code == 200
    body = response.json()
    nodes = {n["node"]: n for n in body["nodes"]}

    assert set(nodes) == set(NODE_TOOLS)
    assert "web_search" in nodes["GENERATE"]["granted"]
    assert "web_search" not in nodes["GENERATE"]["effective"]
    assert body["actuatorTools"] == ["notify", "publish"]
    # The screen must not imply a kill switch is armed when the graph does not read the
    # policy yet.
    assert body["enforced"] is False
    assert "cannot be switched on" in body["policy"]


async def test_a_body_that_tries_to_grant_a_tool_is_refused_not_ignored() -> None:
    """THE test for this feature. A 200 here -- even with the field ignored -- would tell a
    caller that granting works, and the next person to read the API would believe it."""
    writer = FakeToolWriter()
    async with _client(tool_writer=writer) as client:
        response = await client.put(
            "/api/v1/admin/models/tools/GENERATE",
            json={"revoked": [], "granted": ["publish"]},
        )

    assert response.status_code == 422, (
        "the API accepted a `granted` field. Even ignored, that is an API that appears to "
        "widen a prompt-injection barrier from a browser."
    )
    assert writer.saved == []


async def test_a_revocation_response_echoes_a_subset_of_what_the_code_grants() -> None:
    """The invariant, asserted across the wire rather than only in the pure function: the
    effective set the server reports back can never exceed `NODE_TOOLS`."""
    writer = FakeToolWriter()
    async with _client(tool_writer=writer) as client:
        response = await client.put(
            "/api/v1/admin/models/tools/EXPORT", json={"revoked": ["publish"]}
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body["effective"]) <= allowed_tools("EXPORT")
    assert body["effective"] == ["notify"]
    assert writer.saved == [{"node": "EXPORT", "revoked": ["publish"]}]


async def test_revoking_both_actuators_from_export_is_accepted_as_the_kill_switch() -> None:
    async with _client() as client:
        response = await client.put(
            "/api/v1/admin/models/tools/EXPORT", json={"revoked": ["publish", "notify"]}
        )

    assert response.status_code == 200
    assert response.json()["effective"] == []


async def test_revoking_a_tool_a_node_never_held_is_refused_as_a_no_op() -> None:
    """Harmless -- set difference ignores it -- but the operator would believe they had
    switched something off, and on an actuator that is a dangerous belief."""
    async with _client() as client:
        response = await client.put(
            "/api/v1/admin/models/tools/GENERATE", json={"revoked": ["publish"]}
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "tool_not_granted"


async def test_an_unknown_tool_name_is_refused() -> None:
    async with _client() as client:
        response = await client.put(
            "/api/v1/admin/models/tools/GENERATE", json={"revoked": ["pubish"]}
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_tool"


async def test_an_unknown_node_is_refused() -> None:
    async with _client() as client:
        response = await client.put("/api/v1/admin/models/tools/NOT_A_NODE", json={"revoked": []})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_node"


# --------------------------------------------------------------------------- #
# Prompt versions
# --------------------------------------------------------------------------- #


async def test_prompt_versions_reports_an_inventory_and_admits_nothing_is_selectable() -> None:
    async with _client() as client:
        response = await client.get("/api/v1/admin/models/prompt-versions")

    assert response.status_code == 200
    body = response.json()

    assert body["selectable"] is False, (
        "the endpoint claims a prompt version can be selected; nothing in the runtime "
        "reads a stored one, so the screen would offer a control that does nothing"
    )
    keys = {s["key"] for s in body["surfaces"]}
    assert {"nodes", "kb_retrieval", "onboarding"} == keys
    assert all(s["version"] for s in body["surfaces"]), "a surface could not be read"
    assert "--prompt-version" in body["evalHarnessNote"]
