"""The runs API: start a run, watch it, resume it.

Written before the routes. The SSE test is the interesting one — a stream that cannot be
resumed from a sequence number forces a client that lost its connection to replay the
whole run, and a stream that never terminates leaks a connection per reload.
"""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from backend.app.agents.state import new_state
from backend.app.api import runs as runs_api
from backend.app.db.models import Role, User
from backend.app.main import create_app
from backend.app.services.run_service import MAX_RUN_LIST_LIMIT, InMemoryRunStore, RunService

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
    state["renderings"] = {
        "linkedin": {
            "body": "Kurz erklärt: was ein Notar beurkundet.",
            "hashtags": [],
            "hashtags_removed": 0,
            "hashtags_shortfall": 0,
            "over_target": False,
        }
    }
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


# --------------------------------------------------------------------------- #
# The executor is actually reached, and resume refuses what it should
# --------------------------------------------------------------------------- #


class _RecordingExecutor:
    """Records submissions instead of running anything."""

    def __init__(self) -> None:
        self.submitted: list[tuple[UUID, UUID, str, bool]] = []

    def submit(self, run_id: UUID, business_id: UUID, goal: str, *, resume: bool = False) -> None:
        self.submitted.append((run_id, business_id, goal, resume))

    def is_running(self, run_id: UUID) -> bool:
        return False


def _client_with_executor(service: RunService, executor: _RecordingExecutor) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[runs_api.get_run_service] = lambda: service
    app.dependency_overrides[runs_api.current_business] = lambda: BUSINESS
    app.dependency_overrides[runs_api.get_executor] = lambda: executor
    from backend.app.api.auth import current_user

    app.dependency_overrides[current_user] = _user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_starting_a_run_submits_it_for_execution() -> None:
    """The gap this closes: the row used to be created and nothing ever advanced it.

    202 was already correct and already returned; what was missing was anything behind
    it. A test asserting only the status code passed throughout the entire period the
    product could not run a single run.
    """
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()

    async with _client_with_executor(service, executor) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    assert response.status_code == 202
    run_id = UUID(response.json()["runId"])
    assert executor.submitted == [(run_id, BUSINESS, "more local leads", False)]


async def test_resuming_a_stalled_run_submits_it_with_resume_set() -> None:
    """The recovery path for a run whose process died mid-flight."""
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()
    run = await service.start(business_id=BUSINESS, goal="more leads")

    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/resume")

    assert response.status_code == 202
    assert executor.submitted == [(run.id, BUSINESS, "more leads", True)]


@pytest.mark.parametrize("state", ["done", "failed", "partial"])
async def test_resuming_a_finished_run_is_refused(state: str) -> None:
    """Re-running finished work would spend money to overwrite something approved.

    "Resume" does not mean "start again", and a 202 here would quietly destroy output a
    human may already have signed off.
    """
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()
    run = await service.start(business_id=BUSINESS, goal="more leads")
    await service.finish(run.id, outcome=state)  # type: ignore[arg-type]

    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/resume")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_finished"
    assert executor.submitted == [], "nothing may be submitted after a refusal"


async def test_resuming_a_run_awaiting_approval_is_refused() -> None:
    """It is not stalled, it is waiting for a person.

    Resuming would step straight past the review gate, which is the one control that
    stands between a generated page and publication.
    """
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()
    run = await service.start(business_id=BUSINESS, goal="more leads")
    await service.await_approval(run.id)

    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/resume")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_awaiting_approval"
    assert executor.submitted == []


async def test_resuming_another_businesss_run_is_a_404() -> None:
    """Same rule as every other run route: existence is itself information."""
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()
    other = await service.start(business_id=uuid4(), goal="not yours")

    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{other.id}/resume")

    assert response.status_code == 404
    assert executor.submitted == []


async def test_the_application_holds_exactly_one_executor() -> None:
    """A per-request executor would enforce no concurrency limit and drop task refs.

    Each new instance gets its own allowance of four -- which is no allowance -- and
    loses the previous one's strong references, letting live runs be collected.
    """
    from backend.app.services.run_executor import RunExecutor

    app = create_app()
    request = SimpleNamespace(app=app)

    first = runs_api.get_executor(request)  # type: ignore[arg-type]
    second = runs_api.get_executor(request)  # type: ignore[arg-type]

    assert isinstance(first, RunExecutor)
    assert first is second


class _LiveExecutor(_RecordingExecutor):
    """Reports the given runs as already executing in this process."""

    def __init__(self, live: set[UUID]) -> None:
        super().__init__()
        self._live = live

    def is_running(self, run_id: UUID) -> bool:
        return run_id in self._live


async def test_resuming_a_run_that_is_already_executing_is_refused() -> None:
    """Found by driving the real API, not by reading the code.

    A run in state `running` was accepted for resume, which would put a SECOND
    executor on the same run: two writers racing on `next_seq` and on the checkpoint --
    the exact corruption the ordered event drain prevents one level down, reintroduced
    one level up.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    executor = _LiveExecutor(live={run.id})

    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/resume")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_already_executing"
    assert executor.submitted == []


async def test_a_run_left_running_by_a_dead_process_can_still_be_resumed() -> None:
    """The other half, and the reason refusing the DB state `running` would be wrong.

    After a restart the row still says `running` and nothing is driving it. That is the
    case resume exists for, so it must be allowed -- which is why the check asks the
    executor rather than the database.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    executor = _LiveExecutor(live=set())  # a fresh process knows of no live runs

    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/resume")

    assert response.status_code == 202
    assert executor.submitted == [(run.id, BUSINESS, "more leads", True)]


# --------------------------------------------------------------------------- #
# GET /api/v1/runs -- the list the owner reaches a run from
# --------------------------------------------------------------------------- #


async def test_the_runs_list_returns_this_businesss_runs_newest_first() -> None:
    """Without this route a started run is unreachable.

    `POST /api/v1/runs` hands back an id and nothing persisted it anywhere a person could
    see, so an owner who navigated away had no way back to their own run -- the timeline
    screen was reachable only by pasting an id from curl.
    """
    service = RunService(InMemoryRunStore())
    first = await service.start(business_id=BUSINESS, goal="first goal")
    second = await service.start(business_id=BUSINESS, goal="second goal")

    async with _client(service) as client:
        response = await client.get("/api/v1/runs")

    assert response.status_code == 200
    body = response.json()
    assert [r["goal"] for r in body["runs"]] == ["second goal", "first goal"]
    assert [r["runId"] for r in body["runs"]] == [str(second.id), str(first.id)]


async def test_the_runs_list_is_camel_case_like_the_rest_of_the_api() -> None:
    """A client reading `finished_reason` or `current_node` renders nothing, silently.

    The same trap the review endpoint has a test for: snake_case on the wire would not
    fail anything server-side, it would just leave the reason a run stopped invisible.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")
    await service.checkpoint(
        run.id, state=new_state(business_id=BUSINESS, goal="g"), current_node="HARVEST"
    )

    async with _client(service) as client:
        row = (await client.get("/api/v1/runs")).json()["runs"][0]

    assert set(row) == {
        "runId",
        "goal",
        "state",
        "currentNode",
        "resumedCount",
        "finishedReason",
        "createdAt",
    }
    assert row["currentNode"] == "HARVEST"


async def test_a_partial_run_carries_the_reason_it_stopped() -> None:
    """The honesty requirement this product cares most about.

    A run here legitimately ends `partial` because the configured credential cannot reach
    the mid tier. An owner reading a terminal state with no explanation concludes the
    product is broken; the reason is what makes the state truthful, so the list has to
    carry it and not just the word.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more local leads")
    await service.finish(
        run.id,
        outcome="partial",
        reason="Opportunity selection could not run: the configured credential "
        "cannot reach the mid tier.",
    )

    async with _client(service) as client:
        row = (await client.get("/api/v1/runs")).json()["runs"][0]

    assert row["state"] == "partial"
    assert "cannot reach the mid tier" in row["finishedReason"]


async def test_the_runs_list_does_not_show_another_businesss_run() -> None:
    """Same rule as every other route here, on the surface that returns MANY rows.

    `_require_own_run` protects the single-run reads by comparing the owner. A list has no
    id to check, so if the comparison were left out the one endpoint that returns every
    row would be the one endpoint with no owner check on it. The in-memory store is
    deliberately unscoped, exactly as the cross-business timeline test uses it, so this
    asserts the ROUTE's filter rather than the fake's.
    """
    service = RunService(InMemoryRunStore())
    await service.start(business_id=BUSINESS, goal="mine")
    await service.start(business_id=uuid4(), goal="someone else's goal")

    async with _client(service) as client:
        body = (await client.get("/api/v1/runs")).json()

    assert [r["goal"] for r in body["runs"]] == ["mine"]


async def test_the_runs_list_never_carries_the_draft() -> None:
    """A list of twenty runs is where a checkpoint would cost the most.

    The timeline has the same assertion for the same reason; this is the surface that
    multiplies it by the number of rows.
    """
    service = RunService(InMemoryRunStore())
    await _reviewable_run(service)

    async with _client(service) as client:
        body = (await client.get("/api/v1/runs")).json()

    assert "checkpoint" not in json.dumps(body)
    assert "<h1>" not in json.dumps(body, ensure_ascii=False)


async def test_the_runs_list_honours_a_limit_and_refuses_a_silly_one() -> None:
    """An unbounded `limit` is a request to serialise every run a business ever made."""
    service = RunService(InMemoryRunStore())
    for i in range(4):
        await service.start(business_id=BUSINESS, goal=f"g{i}")

    async with _client(service) as client:
        assert [r["goal"] for r in (await client.get("/api/v1/runs?limit=2")).json()["runs"]] == [
            "g3",
            "g2",
        ]
        assert (await client.get("/api/v1/runs?limit=0")).status_code == 422
        assert (await client.get(f"/api/v1/runs?limit={MAX_RUN_LIST_LIMIT + 1}")).status_code == 422


async def test_the_runs_list_is_not_cacheable() -> None:
    """The goals are the customer's own words about their business, and the list sits
    behind a session cookie -- it must not land in a shared cache. Same rule as the
    leads list, which carries named people and phone numbers."""
    service = RunService(InMemoryRunStore())
    await service.start(business_id=BUSINESS, goal="g")

    async with _client(service) as client:
        response = await client.get("/api/v1/runs")

    assert response.headers["cache-control"] == "no-store"


async def test_an_owner_with_no_runs_gets_an_empty_list_not_an_error() -> None:
    """The first thing a new owner's dashboard does is ask this question, and the honest
    answer is "none yet" -- a 404 would send the screen down an error path and make an
    empty account look like a broken one."""
    async with _client(RunService(InMemoryRunStore())) as client:
        response = await client.get("/api/v1/runs")

    assert response.status_code == 200
    assert response.json() == {"runs": []}
