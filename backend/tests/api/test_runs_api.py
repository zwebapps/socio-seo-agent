"""The runs API: start a run, watch it, resume it.

Written before the routes. The SSE test is the interesting one — a stream that cannot be
resumed from a sequence number forces a client that lost its connection to replay the
whole run, and a stream that never terminates leaks a connection per reload.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from backend.app.agents.state import new_state
from backend.app.api import runs as runs_api
from backend.app.core.config import get_settings
from backend.app.db.models import Role, User
from backend.app.llm.pricing import format_usd
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


async def _exportable_run(service: RunService, *, published: dict[str, Any] | None = None) -> UUID:
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
    if published is not None:
        # Set on the state rather than merged into a read-back checkpoint: the stored
        # form is already JSON (`caps` is a dict), so re-checkpointing it would fail on
        # the dataclass the real state carries.
        state["published"] = published
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
    """A run that published NOTHING offers no short link, and says why.

    The premise has changed and the prohibition has not. `publish.page` is a real
    actuator now, so a pack CAN carry a tracked link — but only one that was actually
    minted. This run stops at REVIEW without publishing, so there is no `short_links` row
    behind any code, and a plausible-looking `/l/xxxxxxxx` here would be a URL that 404s
    in somebody's Instagram bio: worse than an honest gap, because the failure is
    invisible. The hub URL is real and is offered instead.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(service)

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run_id}/export")).json()

    assert body["trackedLinkNote"], "the absence has to be stated, not left as an empty field"
    assert body["hubUrl"].endswith(f"/go/{BUSINESS}")
    assert body["trackedLinks"] == []
    assert body["publishedPageUrl"] is None
    assert "/l/" not in json.dumps(body), "no short link may appear without a row behind it"
    for channel in body["channels"]:
        assert "trackedLink" not in channel


async def test_a_simulated_publish_puts_no_link_in_the_pack() -> None:
    """The fabrication guard, now that it can actually fire.

    A simulated publish produces a real outcome row marked `fake`, with a `fake://`
    reference and no minted codes. Reading addresses out of that row is the one way this
    projection could put a dead link in front of a customer — the outcome LOOKS
    successful, because in every respect except reaching the world it was. So `fake` is
    refused before anything is read, and the honest note comes back instead.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(
        service,
        published={
            "approved_by": "user:owner-1",
            "attempted": 1,
            "refs": [
                {
                    "action_type": "publish.page",
                    "target": "landing_page",
                    "status": "succeeded",
                    "external_ref": "fake://publish.page/landing_page#deadbeef",
                    "replayed": False,
                    "fake": True,
                    "error": None,
                    "summary": "publish.page -> landing_page: done (SIMULATED)",
                    "at": "2026-08-20T10:00:00+00:00",
                    "detail": {"simulated": True},
                }
            ],
            "not_published": [],
            "simulated": True,
            "note": "Published 1 of 1 -- SIMULATED",
        },
    )

    async with _client(service) as client:
        body = (await client.get(f"/api/v1/runs/{run_id}/export")).json()

    assert body["publishedPageUrl"] is None
    assert body["trackedLinks"] == []
    assert body["trackedLinkNote"], "a simulated publish is still an absence, and says so"
    assert "fake://" not in json.dumps(body)


async def test_a_real_publish_puts_its_page_and_its_codes_in_the_pack() -> None:
    """The other half: what A1a mints has to reach the thing a human pastes.

    The addresses come from the `publish.page` outcome's own `detail`, which is where the
    landing actuator recorded what it wrote — so a code in this pack is a code that has a
    `short_links` row behind it. The note disappears, because a sentence explaining an
    absence that is not there is a contradiction on the screen.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _exportable_run(
        service,
        published={
            "approved_by": "user:owner-1",
            "attempted": 1,
            "refs": [
                {
                    "action_type": "publish.page",
                    "target": "landing_page",
                    "status": "succeeded",
                    "external_ref": "https://sma.example/p/6f1c9b02-0000-4000-8000-000000000001",
                    "replayed": False,
                    "fake": False,
                    "error": None,
                    "summary": "publish.page -> landing_page: done",
                    "at": "2026-08-20T10:00:00+00:00",
                    "detail": {
                        "content_piece_id": "6f1c9b02-0000-4000-8000-000000000001",
                        "path": "/p/6f1c9b02-0000-4000-8000-000000000001",
                        "status": "published",
                        "score": 88,
                        "ctas": [
                            {
                                "channel": "instagram",
                                "text": "Link in der Bio",
                                "code": "aB3xK9mQ",
                                "path": "/l/aB3xK9mQ",
                                "url": "https://sma.example/l/aB3xK9mQ",
                            }
                        ],
                    },
                }
            ],
            "not_published": [],
            "simulated": False,
            "note": "Published 1 of 1",
        },
    )

    async with _client(service) as client:
        response = await client.get(f"/api/v1/runs/{run_id}/export")
        body = response.json()
        markdown = (
            await client.get(f"/api/v1/runs/{run_id}/export", params={"format": "markdown"})
        ).text

    assert body["publishedPageUrl"].endswith("/p/6f1c9b02-0000-4000-8000-000000000001")
    assert [link["code"] for link in body["trackedLinks"]] == ["aB3xK9mQ"]
    assert body["trackedLinks"][0]["channel"] == "instagram"
    assert body["trackedLinkNote"] is None

    # The markdown is the point of the tier: nobody pastes an escaped JSON string.
    assert "https://sma.example/l/aB3xK9mQ" in markdown
    assert "Published page:" in markdown

    # The pack still refuses to claim a publication. These are addresses to paste, and
    # what EXPORT did with them belongs on the review screen.
    assert "sends nothing to any platform" in body["notice"]


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
    # The spend reader is faked here rather than left real, and it is not a convenience.
    # Unoverridden it is `monthly_spend_usd`, a live `model_usage` read -- so every test
    # that posted a run through this helper opened a Postgres connection, and the pool
    # binds to whichever test's event loop reached it first. Every later test in the
    # module then failed with "attached to a different loop", which is the shared-table
    # db-suite pollution BACKLOG.md section D records, arriving in a route test that has
    # no reason to touch a database at all. Zero spend is also the honest fixture: these
    # tests are about the run lifecycle, and the ceiling has its own tests below.
    # `_spend` is defined further down, beside the ceiling tests it was written for;
    # the lambda body runs per request, so the forward reference resolves.
    app.dependency_overrides[runs_api.get_monthly_spend_reader] = lambda: _spend("0")
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


async def test_a_run_records_the_channels_the_caller_chose() -> None:
    """The seam that existed and could not be reached.

    `NodeDeps.channels` was injectable from the first day the nodes were written, and
    the one production construction site never passed it -- so the system looked
    configurable and rendered the same three channels for every business.
    """
    service = RunService(InMemoryRunStore())

    async with _client_with_executor(service, _RecordingExecutor()) as client:
        response = await client.post(
            "/api/v1/runs", json={"goal": "more local leads", "channels": ["linkedin"]}
        )

    assert response.status_code == 202
    run = await service.get(UUID(response.json()["runId"]))
    assert run is not None
    assert run.channels == ["linkedin"]


async def test_omitting_channels_records_none_rather_than_guessing_the_default() -> None:
    """Empty on the row means "nobody chose", which is not the same fact as a caller
    choosing all three. The executor resolves it at `new_state`, so the distinction
    survives in the row and the default can change without rewriting history."""
    service = RunService(InMemoryRunStore())

    async with _client_with_executor(service, _RecordingExecutor()) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    run = await service.get(UUID(response.json()["runId"]))
    assert run is not None
    assert run.channels == []


async def test_an_unknown_channel_is_refused_by_name_rather_than_dropped() -> None:
    """A silent drop would look like a successful run that simply produced nothing for
    the channel asked for, and the caller would have no way to tell."""
    service = RunService(InMemoryRunStore())

    async with _client_with_executor(service, _RecordingExecutor()) as client:
        response = await client.post(
            "/api/v1/runs",
            json={"goal": "more local leads", "channels": ["linkedin", "threads"]},
        )

    assert response.status_code == 422
    assert "threads" in response.text


async def test_a_channel_alias_is_canonicalised_not_treated_as_a_second_channel() -> None:
    """`engines/channel/specs.py` exists because two tables of channel names
    disagreed; accepting both spellings as separate channels would rebuild that."""
    service = RunService(InMemoryRunStore())

    async with _client_with_executor(service, _RecordingExecutor()) as client:
        response = await client.post(
            "/api/v1/runs",
            json={"goal": "more local leads", "channels": ["facebook_post", "facebook"]},
        )

    assert response.status_code == 202
    run = await service.get(UUID(response.json()["runId"]))
    assert run is not None
    assert run.channels == ["facebook"]


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


# --------------------------------------------------------------------------- #
# Rejection: the other half of the gate, and the hole it closes
#
# The interesting test in this section is not the happy path. It is
# `test_resuming_a_rejected_run_is_refused_and_never_reaches_the_executor`:
# until `rejected` joined resume's finished set, a rejected run was neither
# finished nor `awaiting_approval` as far as that route could tell, so it fell
# past BOTH refusals into `executor.submit(..., resume=True)` and the graph
# carried on through EXPORT -- publishing the draft a human had just refused.
# A review gate that the button beside it walks around is not a gate.
# --------------------------------------------------------------------------- #

REASON = "Tone is far too formal for this client"


async def _parked_run(service: RunService, *, goal: str = "more leads") -> UUID:
    """A run at the gate WITH a checkpoint, the way a real run arrives there."""
    run = await service.start(business_id=BUSINESS, goal=goal)
    state = new_state(business_id=BUSINESS, goal=goal)
    await service.checkpoint(run.id, state=state, current_node="REVIEW")
    await service.await_approval(run.id)
    return run.id


async def test_rejecting_a_parked_run_is_200_and_writes_a_terminal_state() -> None:
    """200 and not 202: rejecting starts no work, so it IS complete when it returns.

    `rejected` rather than `partial`, because the two answer different questions. A
    reviewer's refusal recorded as `partial` is indistinguishable in SQL from a node that
    fell short, so "how often do reviewers refuse what we produce" -- the one number that
    says whether this product is any good -- stops being askable.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"runId": str(run_id), "state": "rejected", "finishedReason": REASON}

    stored = await service.get(run_id)
    assert stored is not None
    assert stored.state == "rejected"
    assert stored.finished_reason == REASON


async def test_the_response_reports_the_stored_reason_rather_than_the_one_sent() -> None:
    """`finishedReason` is the third field for a reason: the screen must render what was
    PERSISTED. Whitespace is collapsed on the way in, so a request whose reason differs
    from the stored one is exactly the case that proves the response is a read-back."""
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)
    sent = "  Tone   is\n\n far too\tformal   "

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": sent})

    assert response.status_code == 200, response.text
    assert response.json()["finishedReason"] == "Tone is far too formal"

    stored = await service.get(run_id)
    assert stored is not None
    assert stored.finished_reason == "Tone is far too formal"
    assert stored.finished_reason != sent, "the echo must not be the request reflected back"


def test_the_reason_bounds_are_the_numbers_the_ruling_names() -> None:
    """Pinned as LITERALS, on purpose, and this test is not redundant with the 422 cases.

    Those cases derive their boundary from `REJECT_REASON_MAX`, so moving the constant moves
    them with it and they cannot notice. What matters about 240 is a RELATIONSHIP: it must
    stay strictly under `MAX_FINISHED_REASON` (255, the width of `runs.finished_reason`),
    because `clamp_reason` truncates SILENTLY. Raise the ceiling to 255 and a person's stated
    reason starts getting quietly shortened -- which is the exact outcome the ruling picked
    240 to prevent, and it would otherwise happen without a single test going red.

    The client mirrors these two numbers the way it mirrors `GOAL_MIN`/`GOAL_MAX`, so a
    change here is also a change to a published contract.
    """
    from backend.app.services.run_service import MAX_FINISHED_REASON

    assert runs_api.REJECT_REASON_MIN == 10
    assert runs_api.REJECT_REASON_MAX == 240
    assert runs_api.REJECT_REASON_MAX < MAX_FINISHED_REASON, (
        "the API ceiling must sit UNDER the column width, or clamp_reason silently truncates "
        "a human's reason and the 422 stops being the only length refusal they can meet"
    )


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("   \n\t  ", id="whitespace-only"),
        pytest.param("too short", id="nine-characters"),
        pytest.param("x" * (runs_api.REJECT_REASON_MAX + 1), id="one-over-the-ceiling"),
    ],
)
async def test_a_rejection_without_a_usable_reason_is_422_before_any_write(
    reason: str | None,
) -> None:
    """A reasonless rejection is the one input this product can do nothing with, and the
    reviewer is the only person who will ever know why.

    Whitespace-only is in this list deliberately: the bounds are measured AFTER collapse,
    so eleven spaces is not a reason that happens to be long enough.

    The ceiling is 240 and not 255, the width of `runs.finished_reason`, because
    `clamp_reason` truncates SILENTLY -- so a 422 is the only length refusal a human can
    meet, and nothing quietly shortens what a person said.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)
    payload: dict[str, object] = {} if reason is None else {"reason": reason}

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run_id}/reject", json=payload)

    assert response.status_code == 422, response.text

    unchanged = await service.get(run_id)
    assert unchanged is not None
    assert unchanged.state == "awaiting_approval", "a refused request must write nothing"
    assert unchanged.finished_reason is None


async def test_a_reason_at_the_ceiling_is_accepted_and_not_clamped() -> None:
    """The boundary in the other direction. 240 characters fit the column with room to
    spare, so nothing may be truncated -- if this ever fails, the ceiling and the column
    have drifted apart and a reviewer's words are being cut without being told."""
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)
    reason = "n" * runs_api.REJECT_REASON_MAX

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": reason})

    assert response.status_code == 200, response.text
    assert response.json()["finishedReason"] == reason
    assert "..." not in response.json()["finishedReason"]


@pytest.mark.parametrize("state", ["queued", "running", "done", "failed", "partial"])
async def test_a_run_that_is_not_parked_cannot_be_rejected(state: str) -> None:
    """The SAME code as approve, because it is the same condition -- one client handler
    serves both buttons. Only the sentence differs."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    if state != "queued":
        await service.finish(run.id, outcome=state)  # type: ignore[arg-type]

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/reject", json={"reason": REASON})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "run_not_awaiting_approval"
    assert state in detail["message"]
    assert "rejected" in detail["message"]


async def test_a_second_rejection_is_409_and_does_not_overwrite_the_first_reason() -> None:
    """Deliberately not idempotent-by-silence. The caller believes they are deciding
    something; the honest answer is that it is already decided -- and the first
    reviewer's words must survive the second click."""
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)

    async with _client(service) as client:
        first = await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})
        second = await client.post(
            f"/api/v1/runs/{run_id}/reject", json={"reason": "second opinion, also no"}
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "run_not_awaiting_approval"

    stored = await service.get(run_id)
    assert stored is not None
    assert stored.finished_reason == REASON


async def test_a_run_with_no_checkpoint_is_still_rejectable() -> None:
    """The one deliberate divergence from approve, which refuses a checkpoint-less run.

    Approve needs a checkpoint because the approval is written INTO it. A rejection writes
    nothing there, and a run parked having produced nothing is precisely what a reviewer
    should be able to dismiss. A reviewer must always be able to say no.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    await service.await_approval(run.id)
    assert (await service.get(run.id)) is not None

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/reject", json={"reason": REASON})

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "rejected"


async def test_rejecting_leaves_the_checkpoint_readable() -> None:
    """A refused draft is evidence of work the owner already paid for, so the review tabs
    have to keep rendering it. Proven by projecting the review AFTER the rejection rather
    than by inspecting the column: the projection is what the screen actually reads."""
    service = RunService(InMemoryRunStore())
    run_id = await _reviewable_run(service)

    async with _client(service) as client:
        assert (
            await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})
        ).status_code == 200
        review = await client.get(f"/api/v1/runs/{run_id}/review")

    assert review.status_code == 200
    body = review.json()
    assert body["hasOutput"] is True
    assert body["draft"]["title"] == "Notar in Koblenz"


async def test_the_rejecter_is_not_recorded_anywhere() -> None:
    """A difference from approve, not an oversight. `approved_by` exists because it
    authorises an outward publish and lands on every `actions` row; a rejection authorises
    nothing and sends nothing, and with one user per business a `rejected_by` would store
    what `business_id` already implies."""
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})

    assert set(response.json()) == {"runId", "state", "finishedReason"}

    restored = await service.restore(run_id)
    assert restored is not None
    assert restored.get("approved_by") in (None, ""), "a rejection approves nothing"
    assert not any("reject" in key for key in restored), (
        f"nothing about the rejecter belongs in the checkpoint, found {sorted(restored)}"
    )


async def test_rejecting_never_reaches_the_executor() -> None:
    """There is nothing to start and nothing to stop. Approve submits; reject must not."""
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)
    executor = _RecordingExecutor()

    async with _client_with_executor(service, executor) as client:
        response = await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})

    assert response.status_code == 200, response.text
    assert executor.submitted == []


async def test_resuming_a_rejected_run_is_refused_and_never_reaches_the_executor() -> None:
    """THE hole this task closes, and the reason the state had to join resume's set.

    A rejected run is not `awaiting_approval` any more, and before `rejected` was added to
    the finished set it was not "finished" either -- so it fell past BOTH of resume's
    refusals and reached `executor.submit(..., resume=True)`, which continues the graph from
    the checkpoint through EXPORT and PUBLISHES the draft a human had just refused.

    The assertion that matters is the second one. A 409 with a submission behind it would
    still have published.
    """
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)
    executor = _RecordingExecutor()

    async with _client_with_executor(service, executor) as client:
        rejected = await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})
        resumed = await client.post(f"/api/v1/runs/{run_id}/resume")

    assert rejected.status_code == 200, rejected.text
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"]["code"] == "run_finished"
    assert executor.submitted == [], (
        "a rejected run reaching the executor republishes what a human refused"
    )


async def test_approving_a_rejected_run_is_refused_and_never_reaches_the_executor() -> None:
    """Approve-after-reject is the existing 409 and needs no new vocabulary: rejection is
    terminal and not reversible, and the recovery is a NEW run that re-derives from
    current documents rather than republishing what was refused."""
    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)
    executor = _RecordingExecutor()

    async with _client_with_executor(service, executor) as client:
        assert (
            await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})
        ).status_code == 200
        approved = await client.post(f"/api/v1/runs/{run_id}/approve")

    assert approved.status_code == 409
    assert approved.json()["detail"]["code"] == "run_not_awaiting_approval"
    assert executor.submitted == []


async def test_the_event_stream_ends_for_a_rejected_run() -> None:
    """`rejected` is terminal, so nothing will ever move this run. Without it in
    `TERMINAL` the stream holds the connection open for the full MAX_STREAM_SECONDS
    waiting for events that cannot come -- fifteen minutes per reload."""
    assert "rejected" in runs_api.TERMINAL

    service = RunService(InMemoryRunStore())
    run_id = await _parked_run(service)

    async with _client(service) as client:
        assert (
            await client.post(f"/api/v1/runs/{run_id}/reject", json={"reason": REASON})
        ).status_code == 200
        async with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    assert "event: end" in body
    assert '"state": "rejected"' in body
    assert REASON in body


async def test_another_businesss_run_cannot_be_rejected() -> None:
    """404, not 403: whether a run exists is itself information."""
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=uuid4(), goal="not yours")
    await service.await_approval(run.id)

    async with _client(service) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/reject", json={"reason": REASON})

    assert response.status_code == 404

    untouched = await service.get(run.id)
    assert untouched is not None
    assert untouched.state == "awaiting_approval"


async def test_rejection_needs_a_session() -> None:
    """The rejecter is not RECORDED, which is not the same as not being AUTHENTICATED:
    `current_business` resolves through the authenticated user, so an anonymous caller is
    401 here as everywhere else."""
    app = create_app()
    app.dependency_overrides[runs_api.get_run_service] = lambda: RunService(InMemoryRunStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/v1/runs/{uuid4()}/reject", json={"reason": REASON})

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# The per-business monthly ceiling (`ARCHITECTURE.md` 7.4)
#
# Two properties are under test, and the second is the one that is easy to fake.
#
# 1. a business past its ceiling is REFUSED, with both numbers stated;
# 2. the ceiling is read BEFORE anything that could reach a provider.
#
# A test that only asserts (1) passes whether the check runs before the call or after
# it -- an over-budget business would be refused either way, having already spent the
# money. So the ordering is asserted directly, on the HAPPY path, where a
# check-after-the-call would still return 202: `_TracingExecutor` and the spend reader
# append to one shared list, and the assertion is on the ORDER of that list. Moving
# `_require_monthly_headroom` below `executor.submit` turns
# `["spend-read", "submit"]` into `["submit", "spend-read"]` and fails it.
#
# `submit` standing in for "a provider call" is exact here rather than approximate: it
# is the route's only path to the executor, the executor is the only thing that drives
# the graph, and the graph is the only caller of the router. Nothing else in these
# handlers can spend a cent.
# --------------------------------------------------------------------------- #


class _TracingExecutor(_RecordingExecutor):
    """A recording executor that also records WHEN it was reached."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        self._trace = trace

    def submit(self, run_id: UUID, business_id: UUID, goal: str, *, resume: bool = False) -> None:
        self._trace.append("submit")
        super().submit(run_id, business_id, goal, resume=resume)


def _spend(usd: str, *, trace: list[str] | None = None, seen: list[UUID] | None = None) -> Any:
    """A ledger read double, returning `usd` as a `Decimal` -- never a float."""

    async def read(business_id: UUID) -> Decimal:
        if trace is not None:
            trace.append("spend-read")
        if seen is not None:
            seen.append(business_id)
        return Decimal(usd)

    return read


def _client_with_spend(
    service: RunService, executor: _RecordingExecutor, spend: Any
) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[runs_api.get_run_service] = lambda: service
    app.dependency_overrides[runs_api.current_business] = lambda: BUSINESS
    app.dependency_overrides[runs_api.get_executor] = lambda: executor
    app.dependency_overrides[runs_api.get_monthly_spend_reader] = lambda: spend
    from backend.app.api.auth import current_user

    app.dependency_overrides[current_user] = _user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _cap() -> Decimal:
    return get_settings().business_monthly_cap_usd


async def test_a_business_over_its_monthly_ceiling_cannot_start_a_run() -> None:
    """The refusal states BOTH numbers.

    "Over budget" with no figures is a support ticket: the owner cannot tell whether they
    are a cent over or a hundred dollars over, and nobody can check it against the cost
    screen. So the spend and the ceiling are both in the message, formatted by the same
    `format_usd` that screen uses.
    """
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()
    spent = _cap() + Decimal("5")

    async with _client_with_spend(service, executor, _spend(str(spent))) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "monthly_cap_exceeded"
    assert format_usd(spent) in detail["message"], "the refusal must state what was spent"
    assert format_usd(_cap()) in detail["message"], "the refusal must state the ceiling"

    assert executor.submitted == [], "nothing may be submitted after a cap refusal"
    assert await service.recent() == [], (
        "a refused run must not leave a row behind: it would count as a run in the list "
        "and on the cost screen while having done nothing"
    )


async def test_a_business_under_its_monthly_ceiling_runs() -> None:
    """The other half of the same control. A guard that refuses everyone is not a cap."""
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()

    async with _client_with_spend(service, executor, _spend("0.01")) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    assert response.status_code == 202, response.text
    assert executor.submitted == [
        (UUID(response.json()["runId"]), BUSINESS, "more local leads", False)
    ]


async def test_the_ceiling_is_read_before_the_run_reaches_the_executor() -> None:
    """The ordering, asserted on the happy path so it cannot pass by accident.

    This is the test that fails if the check is moved after `executor.submit`: both calls
    happen either way and the response is 202 either way, so only their ORDER separates a
    control from an audit.
    """
    trace: list[str] = []
    service = RunService(InMemoryRunStore())
    executor = _TracingExecutor(trace)

    async with _client_with_spend(service, executor, _spend("0.01", trace=trace)) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    assert response.status_code == 202, response.text
    assert trace == ["spend-read", "submit"], (
        "the ceiling must be read before the run is handed to the executor; "
        f"got {trace} -- a check after the call is accounting, not control"
    )


async def test_a_business_exactly_at_its_ceiling_is_refused() -> None:
    """AT the ceiling counts as over.

    Same boundary as `BudgetState.can_afford`, which affords nothing once `remaining_usd`
    is zero, and same as `RunSpend.at_cap`, which reports `>=`. A run whose first call
    costs an unknown amount cannot be started with nothing left, and two cap levels that
    disagreed at their boundary would be reported as a bug within a week.
    """
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()

    async with _client_with_spend(service, executor, _spend(str(_cap()))) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "monthly_cap_exceeded"
    assert executor.submitted == []


async def test_the_ceiling_comes_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The number is a setting, so an operator can lower it without a deploy.

    Also pins the direction: the same spend that passes under a $25 ceiling is refused
    under a 10-cent one, which is what makes this the ceiling doing the work rather than
    the spend figure.
    """
    tight = get_settings().model_copy(update={"business_monthly_cap_usd": Decimal("0.10")})
    monkeypatch.setattr(runs_api, "get_settings", lambda: tight)

    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()

    async with _client_with_spend(service, executor, _spend("0.20")) as client:
        response = await client.post("/api/v1/runs", json={"goal": "more local leads"})

    assert response.status_code == 409, response.text
    assert format_usd(Decimal("0.10")) in response.json()["detail"]["message"]
    assert executor.submitted == []


async def test_the_ceiling_is_read_for_the_calling_business() -> None:
    """One business's spend must not consume another's allowance.

    The business id is not accepted from the client anywhere in this module, so what is
    asserted here is that the guard reads the SAME id the run is started for, rather than
    a platform-wide total that would refuse everyone once any one business overspent.
    """
    seen: list[UUID] = []
    service = RunService(InMemoryRunStore())
    executor = _RecordingExecutor()

    async with _client_with_spend(service, executor, _spend("0.01", seen=seen)) as client:
        assert (
            await client.post("/api/v1/runs", json={"goal": "more local leads"})
        ).status_code == 202

    assert seen == [BUSINESS]


async def test_resuming_is_refused_when_the_ceiling_is_used_up() -> None:
    """Resume restarts the graph with a FRESH per-run budget.

    Leaving it unguarded would let a business past its ceiling walk through it one resume
    at a time, which the per-run cap cannot see and this ceiling exists to stop.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    executor = _RecordingExecutor()

    async with _client_with_spend(service, executor, _spend(str(_cap()))) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/resume")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "monthly_cap_exceeded"
    assert executor.submitted == []


async def test_resume_reads_the_ceiling_before_the_executor() -> None:
    """Same ordering assertion as for start, on the other guarded entry point."""
    trace: list[str] = []
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    executor = _TracingExecutor(trace)

    async with _client_with_spend(service, executor, _spend("0.01", trace=trace)) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/resume")

    assert response.status_code == 202, response.text
    assert trace == ["spend-read", "submit"], f"got {trace}"


async def test_an_unknown_run_is_still_404_for_a_business_over_its_ceiling() -> None:
    """Which refusal comes first is a disclosure decision, not a style one.

    A cross-business or absent run id stays indistinguishable from absent even when the
    caller's ledger would refuse them anyway -- otherwise "409 rather than 404" would tell
    a caller that an id they guessed exists.
    """
    service = RunService(InMemoryRunStore())
    other = await service.start(business_id=uuid4(), goal="someone else's run")
    executor = _RecordingExecutor()

    async with _client_with_spend(service, executor, _spend(str(_cap()))) as client:
        response = await client.post(f"/api/v1/runs/{other.id}/resume")

    assert response.status_code == 404
    assert executor.submitted == []


async def test_approving_a_parked_run_is_not_blocked_by_the_ceiling() -> None:
    """A deliberate exclusion, recorded as a test so it cannot be undone by accident.

    Approval publishes work that was already generated and already paid for. Refusing it
    at the review gate would strand a draft a person is looking at, and buy nothing: the
    only way to reach `awaiting_approval` is through `start_run`, which IS guarded, so
    what approval can release is bounded by runs that began before the breach.
    """
    service = RunService(InMemoryRunStore())
    run = await service.start(business_id=BUSINESS, goal="more leads")
    state = new_state(business_id=BUSINESS, goal="more leads")
    await service.checkpoint(run.id, state=state, current_node="REVIEW")
    await service.await_approval(run.id)

    executor = _RecordingExecutor()
    async with _client_with_spend(service, executor, _spend(str(_cap() + Decimal("5")))) as client:
        response = await client.post(f"/api/v1/runs/{run.id}/approve")

    assert response.status_code == 202, response.text
    assert [(rid, resume) for rid, _, _, resume in executor.submitted] == [(run.id, True)]
