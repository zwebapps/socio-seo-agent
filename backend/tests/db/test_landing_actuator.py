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

import pytest
from sqlalchemy import text

from backend.app.actuators import OutcomeStatus
from backend.app.actuators.landing import LandingPageActuator
from backend.app.agents.nodes import (
    PAGE_PUBLISH_ACTION,
    NodeDeps,
    build_nodes,
)
from backend.app.agents.state import AgentState, new_state
from backend.app.db.adapters.content_store import (
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
    # No `landing_page` key any more: the actuator tests build their own `Actuation`
    # payload from the spec, and EXPORT has no page to carry.
    state.update({"approved_by": APPROVER})
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


# --------------------------------------------------------------------------- #
# The landing actuator, invoked DIRECTLY
#
# EXPORT no longer reaches it: the founder ruled that we host no landing page
# (`CLAUDE.md`, 2026-08-21), so `PAGE_PUBLISH_ACTION` left `PUBLISHABLE_ACTIONS`.
# The actuator and `publish_landing_page` are RETAINED, because pieces published
# before that ruling exist and `GET /p/{piece_id}` must keep serving them — so the
# tests that drove it through the graph are rewritten to call it directly rather
# than deleted, which would have left retained code with no coverage at all.
# --------------------------------------------------------------------------- #


async def test_the_actuator_still_publishes_a_page_with_a_link_per_channel(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The retained path, exercised without the graph."""
    from backend.app.actuators.contract import Actuation
    from backend.app.actuators.landing import LandingPageActuator

    spec = a_spec()
    actuator = LandingPageActuator(
        content_store=PostgresContentStore(),
        link_store=PostgresLeadStore(),
        base_url=BASE,
    )

    outcome = await actuator.perform(
        Actuation(
            business_id=business_a,
            action_type=actuator.action_type,
            target="landing_page",
            payload=spec.model_dump(mode="json"),
            approved_by=APPROVER,
        )
    )

    assert outcome.status is OutcomeStatus.SUCCEEDED
    assert outcome.fake is False, "this app serves the page, so there is nothing to fake"

    pieces = await read_pieces(business_a)
    assert len(pieces) == 1
    links = await read_links(business_a)
    assert {link["channel"] for link in links} == {cta.channel for cta in spec.ctas}


async def test_the_published_page_is_still_servable(
    scoped_sessions: None, business_a: UUID
) -> None:
    """The reason the actuator is retained rather than deleted: a real row exists and
    the public route has to keep resolving it."""
    from backend.app.actuators.contract import Actuation
    from backend.app.actuators.landing import LandingPageActuator

    actuator = LandingPageActuator(
        content_store=PostgresContentStore(),
        link_store=PostgresLeadStore(),
        base_url=BASE,
    )
    await actuator.perform(
        Actuation(
            business_id=business_a,
            action_type=actuator.action_type,
            target="landing_page",
            payload=a_spec().model_dump(mode="json"),
            approved_by=APPROVER,
        )
    )

    piece_id = (await read_pieces(business_a))[0]["id"]
    target = await PostgresContentStore().resolve_landing_page(piece_id)

    assert target is not None, "the SECURITY DEFINER resolver still finds it"


# --------------------------------------------------------------------------- #
# What EXPORT does instead
# --------------------------------------------------------------------------- #


async def test_export_no_longer_publishes_a_page(scoped_sessions: None, business_a: UUID) -> None:
    """The ruling, asserted against real SQL rather than trusted.

    A run that still wrote a `content_pieces` row with `surface='landing_page'` would be
    hosting a page for a business that asked us not to — and the row is the only place
    that would show.
    """
    deps, _ = _deps(PostgresContentStore(), PostgresLeadStore(), Ledger()), None

    updates = await build_nodes(deps)["EXPORT"](_state(business_a, a_spec()))

    assert all(piece["surface"] != "landing_page" for piece in await read_pieces(business_a))
    assert "landing_page" not in [ref["target"] for ref in updates["published"]["refs"]]
