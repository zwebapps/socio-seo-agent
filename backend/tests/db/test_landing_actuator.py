"""``publish.page``: the actuator that finally makes the conversion chain reachable.

Everything else in the product's promise already worked in isolation. What did not
exist was a caller: nothing in the application invoked ``create_landing_page``, so no
landing ``content_pieces`` row was ever written by a run, ``GET /p/{id}`` could never
serve anything, and **no tracked short link was minted outside a hand-written test**.
Run → page → tracked link → click → lead → attribution was a chain with its first link
missing, and the run reported a cheerful simulated publish over the gap.

So these tests are deliberately end-to-end and deliberately on real SQL. A double
cannot prove the thing that was broken: the bug was that no row existed, and only the
database can say whether one does. Row-level security is on and the restricted role is
what writes, because a publish path that works as a superuser and fails in production
is the failure mode this repo already fixed once.

What is asserted, in the order the value arrives:

1. EXPORT publishes the page — one piece, ``status='published'``, spec in ``meta``.
2. The page is then SERVABLE, which is the point of publishing rather than storing.
3. One tracked link per channel CTA, each retargeted to carry its own ``?ref=``.
4. Running EXPORT twice publishes ONCE. This is the test that matters most: EXPORT is
   reachable only by resume, resume is retried by humans, and an idempotency key that
   did not hold would mint a second page and a second set of links every time.
5. A real publish and a simulated one in the SAME run stay distinguishable.
6. An unpublishable page is REFUSED, not failed, and writes nothing.
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from backend.app.actuators import FakeActuator, OutcomeStatus
from backend.app.actuators.landing import LandingPageActuator
from backend.app.agents.nodes import (
    PAGE_PUBLISH_ACTION,
    SOCIAL_POST_ACTION,
    NodeDeps,
    build_nodes,
)
from backend.app.agents.state import AgentState, new_state
from backend.app.db.adapters.content_store import (
    LANDING_SPEC_KEY,
    LANDING_SURFACE,
    PostgresContentStore,
)
from backend.app.db.adapters.lead_store import PostgresLeadStore
from backend.app.db.session import business_session
from backend.app.engines.landing.contract import (
    ChannelCta,
    FormField,
    LandingPageSpec,
    ProofPoint,
)
from backend.app.main import create_app

pytestmark = [pytest.mark.db]

BASE = "https://sma.example"
APPROVER = "user:owner-1"


class Ledger:
    """The `actions` ledger in memory, with the contract's claim semantics.

    In memory rather than Postgres on purpose: `PostgresActionStore` has its own tests,
    and what these tests are about is what the ACTUATOR writes to the content and link
    tables. Keeping the ledger here means a replay is driven by the documented claim
    contract rather than by whatever the ledger happens to do.
    """

    def __init__(self) -> None:
        self.settled: dict[str, Any] = {}
        self.claimed: list[Any] = []

    async def claim(self, actuation: Any) -> Any:
        self.claimed.append(actuation)
        return self.settled.get(actuation.idempotency_key())

    async def settle(self, actuation: Any, outcome: Any) -> None:
        if outcome.status is OutcomeStatus.SUCCEEDED:
            self.settled[actuation.idempotency_key()] = outcome


def a_spec(**over: Any) -> LandingPageSpec:
    base: dict[str, Any] = {
        "headline": "Notdienst-Checkliste für Hauseigentümer in Koblenz",
        "subhead": "Fünf Prüfungen, bevor Sie den Notdienst rufen.",
        "offer": "Eine zweiseitige Checkliste mit den fünf Prüfungen bei einem Wasserschaden.",
        "proof_points": [
            ProofPoint(text="Seit 1998 in Koblenz.", source="Leistungsübersicht 2026"),
            ProofPoint(text="24-Stunden-Notdienst.", source="mueller-sanitaer.de/notdienst"),
        ],
        "form_fields": [
            FormField(name="name", label="Ihr Name", required=False),
            FormField(name="email", label="E-Mail-Adresse", required=True),
        ],
        "primary_cta": "Checkliste anfordern",
        "consent_text": "Ich bin mit der Kontaktaufnahme einverstanden.",
        "ctas": [
            ChannelCta(channel="linkedin", text="Unsere Notdienst-Checkliste:"),
            ChannelCta(channel="facebook", text="Checkliste zum Mitnehmen:"),
        ],
    }
    base.update(over)
    return LandingPageSpec.model_validate(base)


def _deps(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    ledger: Ledger,
    *,
    also: Mapping[str, Any] | None = None,
) -> NodeDeps:
    page = LandingPageActuator(content_store=content_store, link_store=link_store, base_url=BASE)
    by_action: dict[str, Any] = {PAGE_PUBLISH_ACTION: page, **(dict(also) if also else {})}
    # `router=object()`: EXPORT makes no model call, and handing it a real router
    # would hide a regression that started making one.
    return NodeDeps(
        router=object(),
        actuator_for=by_action.get,
        actuator_store=ledger,
    )


def _state(business_id: UUID, spec: LandingPageSpec, **over: Any) -> AgentState:
    # `channels` is per-run state rather than a `NodeDeps` field, so a run that
    # renders two channels says so here.
    state = new_state(
        business_id=business_id,
        goal="more local leads",
        dna={"name": "Müller Sanitär GmbH", "city": "Koblenz"},
        channels=("linkedin", "facebook"),
    )
    state.update({"approved_by": APPROVER, "landing_page": spec.model_dump(mode="json")})
    state.update(over)  # type: ignore[typeddict-item]
    return state


def _ref(updates: dict[str, Any], action_type: str) -> dict[str, Any]:
    """The one outcome row for `action_type`, out of EXPORT's published report.

    `updates["published"]` is the REPORT (approved_by, attempted, refs, note, ...), not a
    list of outcomes -- and `refs` rows carry `action_type`/`fake`, not `action`/
    `simulated`. Reading it through one helper means these tests assert the shape the
    review screen actually renders.
    """
    refs = updates["published"]["refs"]
    return next(row for row in refs if row["action_type"] == action_type)


async def read_pieces(business_id: UUID) -> list[dict[str, Any]]:
    async with business_session(business_id) as db:
        rows = (await db.execute(text("SELECT * FROM content_pieces"))).mappings().all()
    return [dict(row) for row in rows]


async def read_links(business_id: UUID) -> list[dict[str, Any]]:
    async with business_session(business_id) as db:
        rows = (
            (await db.execute(text("SELECT * FROM short_links ORDER BY created_at")))
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# 1-3. The chain that did not exist
# --------------------------------------------------------------------------- #


async def test_export_publishes_a_real_page_with_a_tracked_link_per_channel(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """The whole point of A1a: a run leaves a servable page and real tracked links."""
    ledger = Ledger()
    spec = a_spec()
    deps = _deps(content_store, link_store, ledger)

    updates = await build_nodes(deps)["EXPORT"](_state(business_a, spec))

    page = _ref(updates, PAGE_PUBLISH_ACTION)
    assert page["status"] == "succeeded"
    # Not simulated, and the ref is a real URL rather than a `fake://` string. These two
    # assertions are the ones `FakeActuator` could never satisfy.
    assert page["fake"] is False
    assert page["external_ref"].startswith(f"{BASE}/p/")
    assert updates["published"]["simulated"] is False

    pieces = await read_pieces(business_a)
    assert len(pieces) == 1
    assert pieces[0]["status"] == "published"
    assert pieces[0]["surface"] == LANDING_SURFACE
    # `run_id` is deliberately NOT asserted non-null: `AgentState` carries no run id at
    # all, so `_actuate` cannot put one on the `Actuation` and the column stays NULL for
    # every page a run publishes. That is a real attribution gap, filed rather than
    # widened into this task -- closing it means changing the state contract and the
    # checkpoint shape in both drivers. The actuator already forwards
    # `actuation.run_id`, so it needs no change when the state key lands.
    assert pieces[0]["run_id"] is None
    # The spec is kept under its own key, so the page can be re-rendered and diffed
    # without the model that wrote it.
    assert pieces[0]["meta"][LANDING_SPEC_KEY]["headline"] == spec.headline

    links = await read_links(business_a)
    assert len(links) == len(spec.ctas)
    assert {link["channel"] for link in links} == {"linkedin", "facebook"}
    for link in links:
        assert link["content_piece_id"] == pieces[0]["id"]
        # Retargeted after minting, so each link attributes its OWN clicks.
        assert f"ref={link['code']}" in link["target_url"]


async def test_the_published_page_is_actually_servable(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """`status='published'` has to mean the public route serves it.

    Storing a row and calling it published is the simulation this task replaced. The
    public route refuses anything that is not `approved` or `published`, so this is
    what proves the status the actuator writes is the one the route accepts.
    """
    ledger = Ledger()
    deps = _deps(content_store, link_store, ledger)
    await build_nodes(deps)["EXPORT"](_state(business_a, a_spec()))

    piece_id = (await read_pieces(business_a))[0]["id"]

    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        page = await client.get(f"/p/{piece_id}")

    assert page.status_code == 200
    assert "Checkliste anfordern" in page.text
    # The consent sentence is not decoration: the form stores contact details.
    assert "einverstanden" in page.text


# --------------------------------------------------------------------------- #
# 4. The one that matters most
# --------------------------------------------------------------------------- #


async def test_running_export_twice_publishes_exactly_one_page(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """EXPORT is reached by RESUME, and humans retry a resume.

    Without a holding idempotency key this mints a second page and a second set of
    short links on every retry -- and the first page's links keep pointing at the first
    page, so half a campaign's clicks land on an orphan. This is the reason
    `publish.page` belongs in the actuator layer and not in the node.
    """
    ledger = Ledger()
    spec = a_spec()
    deps = _deps(content_store, link_store, ledger)
    export = build_nodes(deps)["EXPORT"]

    first = await export(_state(business_a, spec))
    second = await export(_state(business_a, spec))

    assert len(await read_pieces(business_a)) == 1
    assert len(await read_links(business_a)) == len(spec.ctas)

    assert _ref(first, PAGE_PUBLISH_ACTION)["status"] == "succeeded"
    assert _ref(first, PAGE_PUBLISH_ACTION)["replayed"] is False
    # "already done" is a different fact about this run and the same fact about the
    # world, so it is reported as a success that says it replayed.
    assert _ref(second, PAGE_PUBLISH_ACTION)["status"] == "succeeded"
    assert _ref(second, PAGE_PUBLISH_ACTION)["replayed"] is True
    # Same page, not a second one that happens to look alike.
    assert (
        _ref(second, PAGE_PUBLISH_ACTION)["external_ref"]
        == _ref(first, PAGE_PUBLISH_ACTION)["external_ref"]
    )


# --------------------------------------------------------------------------- #
# 5-6. Honesty under mixed configuration, and refusal
# --------------------------------------------------------------------------- #


async def test_a_real_publish_and_a_simulated_post_stay_distinguishable(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """One run, two integrations, two different truths -- and the report says which.

    This is the mixed state the resolver's docstring promises and nothing could
    previously demonstrate: `publish.page` is real now, while `social.post` is still
    gated on App Review nobody has. A Delivery tab that rendered these identically
    would be the exact lie the actuator layer exists to prevent.
    """
    ledger = Ledger()
    deps = _deps(
        content_store,
        link_store,
        ledger,
        also={SOCIAL_POST_ACTION: FakeActuator(SOCIAL_POST_ACTION)},
    )

    updates = await build_nodes(deps)["EXPORT"](
        _state(
            business_a,
            a_spec(),
            renderings={"linkedin": {"body": "Wir sind da.", "hashtags": []}},
        )
    )

    assert _ref(updates, PAGE_PUBLISH_ACTION)["fake"] is False
    assert _ref(updates, SOCIAL_POST_ACTION)["fake"] is True
    # The run-level flag stays True while ANY destination is simulated, so the note
    # cannot claim a clean sweep -- and it names the simulation explicitly.
    assert updates["published"]["simulated"] is True
    assert "SIMULATED" in updates["published"]["note"]


async def test_an_unpublishable_page_is_refused_and_writes_nothing(
    content_store: PostgresContentStore,
    link_store: PostgresLeadStore,
    business_a: UUID,
) -> None:
    """A page that cannot capture a lead is a POLICY refusal, not a provider failure.

    And nothing is half-written: the deterministic audit runs before the first INSERT,
    so a refused publish leaves no orphan piece and no dangling links behind it.
    """
    ledger = Ledger()
    deps = _deps(content_store, link_store, ledger)

    # No consent sentence and no form field: the audit's own errors, not invented ones.
    updates = await build_nodes(deps)["EXPORT"](
        _state(business_a, a_spec(consent_text="", form_fields=[]))
    )

    page = _ref(updates, PAGE_PUBLISH_ACTION)
    assert page["status"] == "refused"
    # The reason reaches the surface, because the fix hints are what the model needs.
    assert "consent" in (page["error"] or "").lower()
    assert await read_pieces(business_a) == []
    assert await read_links(business_a) == []
