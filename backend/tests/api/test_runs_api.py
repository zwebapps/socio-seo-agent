"""The runs API: start a run, watch it, resume it.

Written before the routes. The SSE test is the interesting one — a stream that cannot be
resumed from a sequence number forces a client that lost its connection to replay the
whole run, and a stream that never terminates leaks a connection per reload.
"""

import json
from uuid import UUID, uuid4

import httpx

from backend.app.agents.state import new_state
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


# --------------------------------------------------------------------------- #
# The review surface: draft, SEO findings, social, AI blocks
# --------------------------------------------------------------------------- #


async def _reviewable_run(service: RunService) -> UUID:
    """A run checkpointed through the REAL state serialiser.

    Deliberately not a hand-written checkpoint dict: the whole risk on this route is
    that the projection and ``AgentState`` drift apart, and a fabricated fixture would
    hide exactly that.
    """
    run = await service.start(business_id=BUSINESS, goal="more local leads")
    state = new_state(business_id=BUSINESS, goal="more local leads")
    state["outline"] = {
        "target_keyword": "notar koblenz",
        "headings": ["Kosten"],
        "answer_blocks": ["Ein Notar beurkundet Grundstückskaufverträge."],
        "cta": "Termin anfragen",
    }
    state["draft"] = {
        "title": "Notar in Koblenz",
        "meta_description": "Was ein Notar beurkundet.",
        "html": "<h1>Notar in Koblenz</h1>",
    }
    state["seo_report"] = {
        "score": 62,
        "passed": False,
        "findings": [
            {
                "code": "title_length",
                "severity": "error",
                "message": "Title too short.",
                "fix_hint": "The title is 16 characters; write 50-60.",
                "measured": 16.0,
                "expected": "50-60 characters",
            }
        ],
    }
    state["renderings"] = {"linkedin": "Kurz erklärt: was ein Notar beurkundet."}
    state["fact_gaps"] = ["uploaded documents"]
    await service.checkpoint(run.id, state=state, current_node="REVIEW")
    await service.await_approval(run.id)
    return run.id


async def test_the_review_endpoint_returns_all_four_tabs_in_camel_case() -> None:
    """One request feeds the whole review screen, and the wire is camelCase like the
    rest of this API — a client reading `fix_hint` would silently render nothing."""
    service = RunService(InMemoryRunStore())
    run_id = await _reviewable_run(service)

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run_id}/review")

    assert response.status_code == 200
    body = response.json()

    assert body["hasOutput"] is True
    assert body["draft"]["title"] == "Notar in Koblenz"
    assert body["draft"]["metaDescription"] == "Was ein Notar beurkundet."
    assert body["seo"]["score"] == 62
    assert body["seo"]["passed"] is False
    assert body["seo"]["findings"][0]["fixHint"] == "The title is 16 characters; write 50-60."
    assert body["social"][0]["channel"] == "linkedin"
    assert body["social"][0]["characters"] == len("Kurz erklärt: was ein Notar beurkundet.")
    assert body["aiBlocks"]["blocks"] == ["Ein Notar beurkundet Grundstückskaufverträge."]
    assert body["aiBlocks"]["targetKeyword"] == "notar koblenz"
    assert body["factGaps"] == ["uploaded documents"]


async def test_the_review_of_a_run_with_no_output_is_200_with_notes_not_404() -> None:
    """The run exists. "GENERATE has not run yet" is the answer, not an error — and a
    404 would send the UI down its "no such run" path, which is a different, wrong story.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run.id}/review")

    assert response.status_code == 200
    body = response.json()
    assert body["hasOutput"] is False
    assert body["draft"] is None
    assert body["draftNote"], "an empty tab must say why it is empty"
    assert body["social"] == []


async def test_the_review_never_invents_content_for_a_tab_with_no_data() -> None:
    """The product claim is that output is grounded. A review screen that filled an
    empty tab with a placeholder would be a lie about the run, so it is pinned here."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run.id}/review")).json()

    assert body["draft"] is None
    assert body["seo"] is None
    assert body["aiBlocks"] is None
    assert body["social"] == []
    assert body["opportunity"] is None


async def test_a_review_for_another_businesss_run_is_404() -> None:
    """The draft is the most sensitive thing a run produces: it is the customer's
    unpublished content. This route must be no more reachable than the timeline."""
    service = RunService(InMemoryRunStore())
    other = await service.start(business_id=uuid4(), goal="someone else's run")

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{other.id}/review")

    assert response.status_code == 404


async def test_an_unknown_run_review_is_404_not_an_empty_review() -> None:
    async with _client(RunService(InMemoryRunStore())) as client:
        response = await client.get(f"/api/v1/runs/{uuid4()}/review")

    assert response.status_code == 404


async def test_the_draft_html_is_not_carried_in_the_polled_timeline_payload() -> None:
    """The timeline is polled every couple of seconds. If the draft rode along with it,
    every tick would re-send the largest thing the run produced — which is why the
    review is a separate request. `ALLOWED_PAYLOAD_KEYS` enforces the same rule on
    events; this asserts the run payload as a whole."""
    service = RunService(InMemoryRunStore())
    run_id = await _reviewable_run(service)

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run_id}")).json()

    assert "checkpoint" not in body
    # The whole payload as one string, so "the draft is nowhere in here" is checkable
    # rather than only "there is no top-level draft key".
    assert "<h1>" not in json.dumps(body, ensure_ascii=False)
