"""The runs API: start a run, watch it, resume it.

Written before the routes. The SSE test is the interesting one — a stream that cannot be
resumed from a sequence number forces a client that lost its connection to replay the
whole run, and a stream that never terminates leaks a connection per reload.
"""

from uuid import UUID, uuid4

import httpx

from backend.app.api import runs as runs_api
from backend.app.db.models import Role, User
from backend.app.main import create_app
from backend.app.services.run_service import InMemoryRunStore, RunService

BUSINESS = uuid4()


def _user() -> User:
    user = User(email="o@example.test", password_hash="x", is_active=True, role=Role.OWNER)
    user.id = uuid4()
    return user


def _client(service: RunService) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[runs_api.get_run_service] = lambda: service
    app.dependency_overrides[runs_api.current_business] = lambda: BUSINESS
    from backend.app.api.auth import current_user

    app.dependency_overrides[current_user] = _user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_starting_a_run_returns_202_and_an_id_immediately() -> None:
    """202, not 200: the work has not happened yet. Returning 200 would imply it had."""
    service = RunService(InMemoryRunStore())
    async with _client(service) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    assert response.status_code == 202
    body = response.json()
    assert UUID(body["runId"])
    assert body["state"] == "queued"


async def test_fetching_a_run_returns_its_state_and_timeline() -> None:
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")
    await service.record_event(run.id, node="INTAKE", status="done", payload={"cost_usd": "0.001"})

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run.id}")).json()

    assert body["state"] == "queued"
    assert body["events"][0]["node"] == "INTAKE"
    assert body["events"][0]["seq"] == 1


async def test_an_unknown_run_is_404_not_500() -> None:
    async with _client(RunService(InMemoryRunStore())) as client:
        response = await client.get(f"/api/v1/runs/{uuid4()}")
    assert response.status_code == 404


async def test_the_event_stream_replays_from_a_sequence_number() -> None:
    """Without this a client that dropped its connection must replay the whole run."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")
    for i in range(4):
        await service.record_event(run.id, node=f"N{i}", status="done")
    await service.finish(run.id, outcome="done")

    async with (
        _client(service) as client,
        client.stream("GET", f"/api/v1/runs/{run.id}/events?after=2") as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "N2" in body and "N3" in body
    assert "N0" not in body, "events before `after` should not be replayed"


async def test_the_stream_terminates_when_the_run_is_finished() -> None:
    """A stream that never ends leaks a connection on every reload."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")
    await service.record_event(run.id, node="REVIEW", status="done")
    await service.await_approval(run.id)

    async with (
        _client(service) as client,
        client.stream("GET", f"/api/v1/runs/{run.id}/events") as response,
    ):
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "awaiting_approval" in body
    assert body.rstrip().endswith("data: {}") or "event: end" in body


async def test_a_run_from_another_business_is_not_visible() -> None:
    """The store is scoped by business; the route must not let an id from elsewhere
    through just because the caller knows it."""
    service = RunService(InMemoryRunStore())
    other = await service.start(business_id=uuid4(), goal="someone else's run")

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{other.id}")

    assert response.status_code == 404, "a cross-business run must be indistinguishable from absent"
