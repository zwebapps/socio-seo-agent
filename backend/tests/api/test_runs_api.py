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
# The export pack: Tier 3, which is the only publishing path this build has
# --------------------------------------------------------------------------- #


#: An Instagram caption with no hashtags written into it, so the three DECLARED tags have
#: to be appended -- which is the case where the paste is longer than the body and the
#: two counts must not be conflated.
INSTAGRAM_BODY = "Wer beurkundet einen Grundstückskauf? Kurz erklärt."
INSTAGRAM_TAGS = ["#Notar", "#Koblenz", "#Immobilien"]
LINKEDIN_BODY = "Kurz erklärt: was ein Notar beurkundet. #Notar"


async def _exportable_run(service: RunService) -> UUID:
    """A run with two channels and a landing page, checkpointed through the real state.

    Two channels on purpose, and specifically LinkedIn and Instagram: one carries a
    clickable link and the other does not, and that difference is the whole reason the
    pack exists rather than a copy button. The landing page is a real
    ``LandingPageSpec``, dumped exactly as CONVERT dumps it -- a hand-written dict here
    would hide a drift between the spec and this projection, which is the only bug this
    route can really have.
    """
    from backend.app.engines.landing import ChannelCta, FormField, LandingPageSpec, ProofPoint

    run = await service.start(business_id=BUSINESS, goal="more local leads")
    state = new_state(business_id=BUSINESS, goal="more local leads")
    state["outline"] = {
        "target_keyword": "notar koblenz",
        "headings": ["Kosten"],
        "answer_blocks": ["Ein Notar beurkundet Grundstückskaufverträge."],
        "cta": "Termin anfragen",
    }
    state["renderings"] = {
        "linkedin": {
            "body": LINKEDIN_BODY,
            "hashtags": ["#Notar"],
            "hashtags_removed": 2,
            "hashtags_shortfall": 0,
            "over_target": False,
        },
        "instagram": {
            "body": INSTAGRAM_BODY,
            "hashtags": INSTAGRAM_TAGS,
            "hashtags_removed": 0,
            "hashtags_shortfall": 0,
            "over_target": False,
        },
    }
    state["landing_page"] = LandingPageSpec(
        headline="Grundstückskauf in Koblenz beurkunden lassen",
        subhead="Termin innerhalb einer Woche",
        offer="Kostenlose Ersteinschätzung Ihres Kaufvertrags",
        proof_points=[ProofPoint(text="Über 400 Beurkundungen", source="Kanzleiprofil 2025")],
        form_fields=[FormField(name="email", label="E-Mail", required=True)],
        primary_cta="Termin anfragen",
        consent_text="Ich bin mit der Kontaktaufnahme einverstanden.",
        ctas=[ChannelCta(channel="instagram", text="Link in der Bio")],
    ).model_dump(mode="json")
    state["fact_gaps"] = ["uploaded documents"]
    await service.checkpoint(run.id, state=state, current_node="REVIEW")
    await service.await_approval(run.id)
    return run.id


async def test_the_export_pack_projects_paste_ready_copy_from_the_checkpoint() -> None:
    """The pack is projected from the run's own checkpoint, not assembled by a client.

    `pasteText` is the field the whole tier turns on: it is the exact string to put in
    the composer, so the count beside it has to be a count of THAT string. A client that
    joined the body and the hashtags itself would measure one thing and paste another.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run_id}/export")

    assert response.status_code == 200
    body = response.json()
    assert body["hasPack"] is True

    by_channel = {channel["channel"]: channel for channel in body["channels"]}
    assert set(by_channel) == {"linkedin", "instagram"}

    linkedin = by_channel["linkedin"]
    # The one tag is already written into the body, so nothing is appended and the two
    # counts agree -- which is also the guard against blindly re-appending every tag.
    assert linkedin["appendedHashtags"] == []
    assert linkedin["pasteText"] == LINKEDIN_BODY
    assert linkedin["bodyCharacters"] == len(LINKEDIN_BODY)
    assert linkedin["pasteCharacters"] == len(LINKEDIN_BODY)
    assert linkedin["characterTarget"] == 1_700
    assert linkedin["characterLimit"] == 3_000
    assert linkedin["hashtagLimit"] == 3
    assert linkedin["linkInBody"] is True
    assert linkedin["linkMechanism"] == "inline"

    instagram = by_channel["instagram"]
    assert instagram["appendedHashtags"] == INSTAGRAM_TAGS
    assert instagram["pasteText"] == f"{INSTAGRAM_BODY}\n\n{' '.join(INSTAGRAM_TAGS)}"
    assert instagram["bodyCharacters"] == len(INSTAGRAM_BODY)
    assert instagram["pasteCharacters"] == len(instagram["pasteText"])
    assert instagram["pasteCharacters"] > instagram["bodyCharacters"]

    assert body["landingPage"]["offer"] == "Kostenlose Ersteinschätzung Ihres Kaufvertrags"
    assert body["landingPage"]["proofPoints"][0]["source"] == "Kanzleiprofil 2025"
    assert body["aiBlocks"]["blocks"] == ["Ein Notar beurkundet Grundstückskaufverträge."]
    assert body["factGaps"] == ["uploaded documents"]


async def test_the_pack_says_a_link_in_the_body_does_not_work_on_instagram() -> None:
    """docs/CHANNELS.md section 1 calls this the correction that matters most: a URL in
    an Instagram caption is not a broken link, it is NO link. Someone pasting the caption
    by hand has to be told, or the CTA is dead and the attribution never happens."""
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run_id}/export")).json()

    instagram = next(c for c in body["channels"] if c["channel"] == "instagram")
    assert instagram["linkInBody"] is False
    assert instagram["linkMechanism"] == "bio_hub"
    assert any("does not work on this channel" in note for note in instagram["notes"])
    assert any("bio hub" in note for note in instagram["notes"])

    # And the channel that CAN carry one is not warned about it, or the warning becomes
    # noise that gets skipped on the channel where it matters.
    linkedin = next(c for c in body["channels"] if c["channel"] == "linkedin")
    assert not any("does not work on this channel" in note for note in linkedin["notes"])


async def test_the_pack_reports_what_code_had_to_correct() -> None:
    """Evidence about the MODEL, not the renderer. A tidy block shown without saying two
    hashtags were cut out in code reports the renderer's competence as the model's."""
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run_id}/export")).json()

    linkedin = next(c for c in body["channels"] if c["channel"] == "linkedin")
    assert linkedin["hashtagsRemoved"] == 2
    assert any("removed in code" in note for note in linkedin["notes"])


async def test_a_run_with_no_renderings_names_the_node_rather_than_an_empty_pack() -> None:
    """An empty `channels` array with nothing beside it is indistinguishable from a
    rendering bug, and the owner cannot tell "REPACK has not run" from "the download is
    broken" -- which need completely different responses from them.

    A run that HAS a checkpoint and no renderings, specifically: that is the state a run
    stopped at OPPORTUNITY leaves behind, and it is the one where the note has to name a
    node rather than say the run has saved nothing.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")
    await service.checkpoint(
        run.id,
        state=new_state(business_id=BUSINESS, goal="g"),
        current_node="OPPORTUNITY",
    )

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run.id}/export")

    assert response.status_code == 200, "the run exists; there is simply nothing in it yet"
    body = response.json()
    assert body["hasPack"] is False
    assert body["channels"] == []
    assert "REPACK" in body["channelsNote"], "the empty half must name the node that fills it"
    assert body["landingPage"] is None
    assert "CONVERT" in body["landingPageNote"], "and the two notes name DIFFERENT nodes"


async def test_a_run_that_has_saved_nothing_at_all_says_that_instead() -> None:
    """A different fact from "REPACK has not run", and it must read as one: this run has
    not checkpointed once, so no node's absence can be singled out."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run.id}/export")).json()

    assert body["hasPack"] is False
    assert "has not saved any state yet" in body["channelsNote"]


async def test_the_pack_never_invents_a_tracked_short_link() -> None:
    """Nothing mints a short link for a run yet (`publish_landing_page` has no caller),
    so a plausible-looking `/l/xxxxxxxx` in the pack would be a URL that 404s in
    somebody's Instagram bio. The hub URL is real and is offered instead."""
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run_id}/export")).json()

    assert body["trackedLinkNote"], "the absence has to be stated, not left as an empty field"
    assert body["hubUrl"].endswith(f"/go/{BUSINESS}")
    assert "/l/" not in json.dumps(body), "no short link may be fabricated anywhere in the pack"
    for channel in body["channels"]:
        assert "trackedLink" not in channel


async def test_the_pack_never_claims_anything_was_published() -> None:
    """The single most misleading thing this payload could do. No actuator is called on
    this route, no `actions` row is written, and the notice says so in the payload rather
    than leaving each client to write that sentence for itself.

    Asserted on what the ROUTE does ("this pack sends nothing") rather than on a claim
    about the deployment: a reassurance that no platform is connected would go stale the
    day one is, and a stale reassurance is worse than none.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run_id}/export")).json()

    assert "sends nothing to any platform" in body["notice"]
    assert "paste" in body["notice"]


async def test_the_markdown_rendering_contains_the_copy_and_the_counts_it_claims() -> None:
    """The reason the pack has a text rendering at all: Tier 3's value is that a person
    pastes it somewhere, and nobody can paste an escaped JSON string."""
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run_id}/export?format=markdown")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    text = response.text

    # The copy itself, for both channels, including the appended hashtags.
    assert LINKEDIN_BODY in text
    assert INSTAGRAM_BODY in text
    assert " ".join(INSTAGRAM_TAGS) in text
    # The cost of pasting it: the measured count, the channel's target, its ceiling.
    assert f"{len(LINKEDIN_BODY):,} characters" in text
    assert "target 1,700" in text
    assert "platform limit 3,000" in text
    # The Instagram truth, in the file as well as on the screen.
    assert "not clickable on this channel" in text
    # The landing page and the answer blocks, because the pack is not only the posts.
    assert "Kostenlose Ersteinschätzung Ihres Kaufvertrags" in text
    assert "Kanzleiprofil 2025" in text
    assert "Ein Notar beurkundet Grundstückskaufverträge." in text
    # And what the run did NOT have, carried into the file rather than left on the screen.
    assert "uploaded documents" in text


async def test_the_markdown_is_an_attachment_named_after_the_run() -> None:
    """`attachment`, so a browser saves the customer's unpublished copy rather than
    rendering it as a page; named after the run, so two downloads do not collide."""
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run_id}/export?format=markdown")

    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="export-pack-{run_id}.md"'


async def test_the_markdown_of_an_empty_run_still_says_which_node_is_missing() -> None:
    """A downloaded file with three empty headings reads as a broken export."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="g")
    await service.checkpoint(
        run.id, state=new_state(business_id=BUSINESS, goal="g"), current_node="OPPORTUNITY"
    )

    async with _client(service) as client:
        text = (await client.get(f"/api/v1/runs/{run.id}/export?format=markdown")).text

    assert "nothing to export yet" in text
    assert "REPACK" in text, "the file carries the same note the screen does"
    assert "CONVERT" in text


async def test_an_unrecognised_export_format_is_refused() -> None:
    """Rather than silently answering with JSON: a client that asked for something else
    has a bug, and answering the question it did not ask is how that bug survives."""
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run_id}/export?format=pdf")

    assert response.status_code == 422


@pytest.mark.parametrize("query", ["", "?format=markdown"])
async def test_the_export_pack_is_not_cacheable(query: str) -> None:
    """The customer's own unpublished copy, behind a session cookie. Same rule as the
    runs list and the leads list -- and it applies to the FILE as much as to the JSON,
    which is the branch a shared cache is most likely to keep."""
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run_id}/export{query}")

    assert response.headers["cache-control"] == "no-store"


async def test_the_export_pack_requires_a_session() -> None:
    """No `current_user` override here, deliberately: this route hands over the whole of
    a business's unpublished content, so it must be no more reachable than the timeline.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    app = create_app()
    app.dependency_overrides[runs_api.get_run_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(f"/api/v1/runs/{run_id}/export")).status_code == 401
        assert (
            await client.get(f"/api/v1/runs/{run_id}/export?format=markdown")
        ).status_code == 401


async def test_an_export_pack_for_another_businesss_run_is_404() -> None:
    service = RunService(InMemoryRunStore())
    other = await service.start(business_id=uuid4(), goal="someone else's run")

    async with _client(service) as client:
        assert (await client.get(f"/api/v1/runs/{other.id}/export")).status_code == 404


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
    assert response.json() == {"runs": [], "nextCursor": None}


# --------------------------------------------------------------------------- #
# Pagination: it used to be a cap, so older runs were unreachable
# --------------------------------------------------------------------------- #


async def test_the_cursor_walks_the_whole_history_without_repeating_or_skipping() -> None:
    """It was a cap, not pagination: a business past the ceiling could not reach its
    older runs at all. Walked in pages of two here, because an off-by-one in a cursor
    shows up as a duplicated or a missing row, and both are invisible on one page."""
    store = InMemoryRunStore()
    service = RunService(store)
    created = [await service.start(business_id=BUSINESS, goal=f"goal {i}") for i in range(5)]
    newest_first = [str(run.id) for run in reversed(created)]

    walked: list[str] = []
    cursor: str | None = None
    async with _client(service) as client:
        for _ in range(5):  # bounded: a cursor bug must fail, not loop forever
            url = "/api/v1/runs?limit=2" + (f"&cursor={cursor}" if cursor else "")
            body = (await client.get(url)).json()
            walked.extend(run["runId"] for run in body["runs"])
            cursor = body["nextCursor"]
            if cursor is None:
                break

    assert walked == newest_first
    assert cursor is None, "the last page must not offer another one"


async def test_the_last_page_offers_no_cursor() -> None:
    """`nextCursor` is how the button knows to disappear. Offering one on a full-but-
    final page would leave a control that fetches nothing."""
    store = InMemoryRunStore()
    service = RunService(store)
    for i in range(2):
        await service.start(business_id=BUSINESS, goal=f"goal {i}")

    async with _client(service) as client:
        body = (await client.get("/api/v1/runs?limit=2")).json()

    assert len(body["runs"]) == 2
    assert body["nextCursor"] is None


async def test_a_malformed_cursor_is_refused_rather_than_silently_restarting() -> None:
    """Quietly returning page one looks exactly like the "older runs" button not
    working, which is the bug report nobody can reproduce."""
    async with _client(RunService(InMemoryRunStore())) as client:
        response = await client.get("/api/v1/runs?cursor=not-a-cursor")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "bad_cursor"


# --------------------------------------------------------------------------- #
# Approval: the human decision the whole machine is built around
# --------------------------------------------------------------------------- #


async def test_approving_a_parked_run_records_who_did_it_and_resumes() -> None:
    """The route that made EXPORT reachable at all.

    `REVIEW` is an interrupt and EXPORT sits after it in `ORDER`, so nothing could pass
    the gate: `await_approval` wrote a state and no actor, and `resume` refuses a parked
    run outright. EXPORT's "no approver" refusal therefore fired on every real run —
    correctly, and uselessly.

    The approver is the AUTHENTICATED user, never a client-supplied value: it lands on
    every `actions` row, so "who authorised this post" has to be answerable from the
    ledger months later.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    state = new_state(business_id=BUSINESS, goal="more leads")
    await service.checkpoint(run.id, state=state, current_node="REVIEW")
    await service.await_approval(run.id)

    executor = _RecordingExecutor()
    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/approve")

    assert response.status_code == 202, response.text
    assert response.json()["state"] == "running"
    assert [(run_id, resume) for run_id, _, _, resume in executor.submitted] == [(run.id, True)], (
        "approval has to RESUME the run, not restart it"
    )

    restored = await service.restore(run.id)
    assert restored is not None
    approver = restored.get("approved_by") or ""
    assert approver.startswith("user:"), f"the approver must be a real identity, got {approver!r}"


async def test_a_run_that_is_not_parked_cannot_be_approved() -> None:
    """409 with the state named, rather than a generic refusal: approving a `running`
    run and approving a `done` one are different mistakes with different fixes."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/approve")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_not_awaiting_approval"
    assert "queued" in response.json()["detail"]["message"]


async def test_approving_a_run_with_no_checkpoint_is_refused() -> None:
    """Not hypothetical: a run can be parked before it produces anything, and approving
    that would be approving nothing. The approver has nowhere to be written."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    await service.await_approval(run.id)

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/approve")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "no_checkpoint"


async def test_another_businesss_run_cannot_be_approved() -> None:
    """404, not 403: whether a run exists is itself information."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=uuid4(), goal="not yours")

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/approve")

    assert response.status_code == 404


async def test_approval_needs_a_session() -> None:
    app = create_app()
    app.dependency_overrides[runs_api.get_run_service] = lambda: RunService(InMemoryRunStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/v1/runs/{uuid4()}/approve")

    assert response.status_code == 401
