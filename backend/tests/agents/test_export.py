"""EXPORT and MEASURE: the two nodes on the far side of the human gate.

`test_graph.py` owns the question of whether a run can REACH these nodes. This file
owns what they do once it has, and every test here is about a way the product could
lie:

* publishing something nobody approved;
* reporting a publication that never left the process;
* losing three channels because one platform was down;
* saying nothing at all when there is no integration configured, which reads exactly
  like a successful publish to anybody looking at a timeline;
* printing a share of voice measured from answers nobody got, or a lead count of zero
  for a link that has been live for four seconds.

Hermetic by construction: the actuator and its ledger are injected, so nothing here
reaches a network, a provider or a database.
"""

from typing import Any
from uuid import uuid4

import pytest

from backend.app.actuators import (
    Actuation,
    ActuationRefusedError,
    Actuator,
    ActuatorError,
    FakeActuator,
    Outcome,
    OutcomeStatus,
)
from backend.app.agents.nodes import (
    ANALYTICS_GAP,
    NO_ACTUATOR_NOTE,
    NOT_APPROVED_NOTE,
    NOTIFY_ACTION,
    PAGE_PUBLISH_ACTION,
    PUBLISH_REVOKED_NOTE,
    SOCIAL_POST_ACTION,
    NodeDeps,
    build_nodes,
)
from backend.app.agents.state import AgentState, NodeError, new_state
from backend.app.agents.tools import ANALYTICS_FETCH, GEO_PROBE, NOTIFY, PUBLISH

BUSINESS = uuid4()
APPROVER = "user:owner-1"


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class Ledger:
    """The `actions` ledger, in memory, with the contract's claim semantics.

    `claim` returns the outcome a key ALREADY has, or None to reserve it -- not a
    boolean. The gap between "does this key exist" and "what did it produce" is where a
    double post lives, which is why the protocol is shaped this way and why the double
    is too.
    """

    def __init__(self) -> None:
        self.claimed: list[Actuation] = []
        self.settled: list[tuple[Actuation, Outcome]] = []
        self._succeeded: dict[str, Outcome] = {}

    async def claim(self, actuation: Actuation) -> Outcome | None:
        self.claimed.append(actuation)
        return self._succeeded.get(actuation.idempotency_key())

    async def settle(self, actuation: Actuation, outcome: Outcome) -> None:
        self.settled.append((actuation, outcome))
        if outcome.status is OutcomeStatus.SUCCEEDED:
            self._succeeded[actuation.idempotency_key()] = outcome

    def targets(self, action_type: str) -> list[str]:
        """Every target CLAIMED for one action type, in order."""
        return [a.target for a in self.claimed if a.action_type == action_type]


class Publisher:
    """A real-looking publisher. `dead` refuses, `banned` declines on policy."""

    def __init__(
        self,
        action_type: str = SOCIAL_POST_ACTION,
        *,
        dead: str | None = None,
        banned: str | None = None,
    ) -> None:
        self._action_type = action_type
        self.dead = dead
        self.banned = banned
        self.sent: list[str] = []

    @property
    def action_type(self) -> str:
        return self._action_type

    @property
    def fake(self) -> bool:
        return False

    async def perform(self, actuation: Actuation) -> Outcome:
        if actuation.target == self.dead:
            raise ActuatorError("the platform returned 503", retryable=True)
        if actuation.target == self.banned:
            raise ActuationRefusedError("this account may not post links")
        self.sent.append(actuation.target)
        return Outcome(
            status=OutcomeStatus.SUCCEEDED,
            action_type=actuation.action_type,
            target=actuation.target,
            external_ref=f"https://{actuation.target}.example/p/1",
        )


def _satisfies_protocol(publisher: Publisher) -> Actuator:
    """mypy checks that the double really is an actuator, like `actuators/fake.py`."""
    return publisher


def _deps(**over: Any) -> NodeDeps:
    base: dict[str, Any] = {"router": object(), "channels": ("linkedin", "facebook")}
    base.update(over)
    return NodeDeps(**base)


def _wired(*actuators: Any, store: Ledger | None = None, **over: Any) -> tuple[NodeDeps, Ledger]:
    """Deps with a publishing integration, resolved per action type."""
    ledger = store or Ledger()
    by_action = {actuator.action_type: actuator for actuator in actuators}
    return _deps(actuator_for=by_action.get, actuator_store=ledger, **over), ledger


def _state(**over: Any) -> AgentState:
    state = new_state(
        business_id=BUSINESS,
        goal="more local leads",
        dna={"name": "Müller Sanitär GmbH", "city": "Koblenz", "email": "chef@mueller.de"},
    )
    state.update(
        {
            "approved_by": APPROVER,
            "landing_page": {"headline": "Notdienst in 30 Minuten", "offer": "Festpreis"},
            "renderings": {
                "linkedin": {"body": "Wir sind da.", "hashtags": ["#Notdienst"]},
                "facebook": {"body": "Wir sind da.", "hashtags": []},
            },
        }
    )
    state.update(over)  # type: ignore[typeddict-item]
    return state


def _codes(updates: dict[str, Any]) -> list[str]:
    return [error.code for error in updates.get("errors", []) if isinstance(error, NodeError)]


# --------------------------------------------------------------------------- #
# EXPORT: nothing publishes without an approval
# --------------------------------------------------------------------------- #


async def test_a_run_nobody_approved_publishes_nothing_and_nothing_reaches_the_actuator() -> None:
    """The strongest form of the assertion: not "it refused" but "it never asked".

    An `Actuation` carrying a blank approver would be a request nobody made, and
    building one to have `actuate()` refuse it would write a ledger row per channel for
    a run no human ever saw.
    """
    publisher = Publisher()
    deps, ledger = _wired(publisher)

    updates = await build_nodes(deps)["EXPORT"](_state(approved_by=None))

    report = updates["published"]
    assert report["refs"] == []
    assert report["attempted"] == 0
    assert report["note"] == NOT_APPROVED_NOTE
    assert _codes(updates) == ["not_approved"]
    assert ledger.claimed == [], "no key may be claimed for an unapproved run"
    assert publisher.sent == []


async def test_an_empty_approver_string_is_not_an_approval() -> None:
    """`approve()` refuses a blank one, so this is the checkpoint-corruption path: the
    node must not read whitespace as authority."""
    deps, ledger = _wired(Publisher())

    updates = await build_nodes(deps)["EXPORT"](_state(approved_by="   "))

    assert updates["published"]["note"] == NOT_APPROVED_NOTE
    assert ledger.claimed == []


async def test_the_approver_is_recorded_on_every_actuation() -> None:
    """ "On whose authority" is the question the `actions` ledger exists to answer."""
    deps, ledger = _wired(Publisher(), FakeActuator(PAGE_PUBLISH_ACTION))

    await build_nodes(deps)["EXPORT"](_state())

    assert {actuation.approved_by for actuation in ledger.claimed} == {APPROVER}


# --------------------------------------------------------------------------- #
# EXPORT: what it publishes, and in what order
# --------------------------------------------------------------------------- #


async def test_the_landing_page_is_published_before_the_posts_that_point_at_it() -> None:
    """Ordering with a consequence: every post carries the ask that points at the page,
    so posting first spends the clicks a tracked link earns on a page that is not there
    yet -- and those clicks are the leads the whole chain exists to capture."""
    posts = Publisher()
    page = Publisher(PAGE_PUBLISH_ACTION)
    deps, ledger = _wired(posts, page)

    updates = await build_nodes(deps)["EXPORT"](_state())

    assert ledger.claimed[0].action_type == PAGE_PUBLISH_ACTION
    assert ledger.targets(SOCIAL_POST_ACTION) == ["linkedin", "facebook"]
    assert updates["published"]["refs"][0]["target"] == "landing_page"


async def test_a_published_post_carries_its_body_and_hashtags() -> None:
    """The payload is persisted verbatim on the audit row: a row that cannot say what
    was sent is not an audit."""
    posts = Publisher()
    deps, ledger = _wired(posts)

    await build_nodes(deps)["EXPORT"](_state())

    linkedin = next(a for a in ledger.claimed if a.target == "linkedin")
    assert linkedin.payload["body"] == "Wir sind da."
    assert linkedin.payload["hashtags"] == ["#Notdienst"]


async def test_an_external_ref_reaches_the_run_so_a_customer_can_check_it() -> None:
    deps, _ = _wired(Publisher())

    updates = await build_nodes(deps)["EXPORT"](_state(landing_page=None))

    refs = {ref["target"]: ref for ref in updates["published"]["refs"]}
    assert refs["linkedin"]["external_ref"] == "https://linkedin.example/p/1"
    assert refs["linkedin"]["status"] == "succeeded"
    assert updates["published"]["not_published"] == []
    assert "errors" not in updates, "a clean publish is not a degradation"


async def test_a_rendering_stored_as_a_plain_string_still_publishes() -> None:
    """Checkpoints written before `renderings` became a mapping hold a bare body
    string, and nothing migrates a JSONB display field. A reader of that column does
    not get to assume its own version wrote it."""
    deps, ledger = _wired(Publisher())

    await build_nodes(deps)["EXPORT"](
        _state(landing_page=None, renderings={"linkedin": "an older checkpoint"})
    )

    assert ledger.claimed[0].payload == {"body": "an older checkpoint", "hashtags": []}


# --------------------------------------------------------------------------- #
# EXPORT: per-destination degradation
# --------------------------------------------------------------------------- #


async def test_one_dead_platform_costs_that_channel_and_says_which() -> None:
    """Exactly HARVEST's per-source rule, applied to the far end of the run: a run that
    published three of four has to say which one it did not."""
    posts = Publisher(dead="facebook")
    deps, _ = _wired(posts, FakeActuator(PAGE_PUBLISH_ACTION))

    updates = await build_nodes(deps)["EXPORT"](_state())

    report = updates["published"]
    assert posts.sent == ["linkedin"], "the live platform still received its post"
    assert report["not_published"] == ["facebook"]
    assert "facebook" in report["note"]
    assert _codes(updates) == ["publish_failed"]
    message = updates["errors"][-1].message
    assert "facebook" in message and "503" in message
    assert "unaffected" in message


async def test_a_policy_refusal_is_recorded_as_a_refusal_not_a_failure() -> None:
    """The two must not be logged alike: a refusal is the system working, and alerting
    on it is how everyone learns to ignore alerts."""
    deps, _ = _wired(Publisher(banned="facebook"))

    updates = await build_nodes(deps)["EXPORT"](_state(landing_page=None))

    assert _codes(updates) == ["publish_refused"]
    refs = {ref["target"]: ref for ref in updates["published"]["refs"]}
    assert refs["facebook"]["status"] == "refused"
    assert refs["linkedin"]["status"] == "succeeded"


async def test_a_publisher_that_explodes_outside_the_contract_loses_one_channel() -> None:
    """`actuate()` returns an Outcome for everything it knows about; this is the layer
    below -- a ledger that cannot reach its database, say. It must still cost one
    destination rather than the run."""

    class Broken(Ledger):
        async def claim(self, actuation: Actuation) -> Outcome | None:
            if actuation.target == "facebook":
                raise RuntimeError("the connection pool is exhausted")
            return await super().claim(actuation)

    deps, _ = _wired(Publisher(), store=Broken())

    updates = await build_nodes(deps)["EXPORT"](_state(landing_page=None))

    report = updates["published"]
    assert report["not_published"] == ["facebook"]
    assert [ref["target"] for ref in report["refs"] if ref["status"] == "succeeded"] == ["linkedin"]
    assert "RuntimeError" in (updates["errors"][-1].message)


# --------------------------------------------------------------------------- #
# EXPORT: the unconfigured deployment, which is every deployment today
# --------------------------------------------------------------------------- #


async def test_no_actuator_wired_is_reported_rather_than_skipped() -> None:
    """The default state of this product. A node that returned `{}` here would leave a
    timeline showing EXPORT/done and nothing else, which reads as a successful publish
    -- and that is the single worst thing this layer could produce."""
    updates = await build_nodes(_deps())["EXPORT"](_state())

    report = updates["published"]
    assert report["refs"] == []
    assert report["attempted"] == 0
    assert report["note"] == NO_ACTUATOR_NOTE
    assert "NOT a claim that a post went out" in report["note"]
    assert _codes(updates) == ["actuator_unwired"]


async def test_half_a_wiring_is_no_wiring() -> None:
    """`actuate()` needs an actuator AND a ledger: without the ledger there is no
    idempotency key to claim, so there is no publisher -- only something that posts."""
    deps = _deps(actuator_for={SOCIAL_POST_ACTION: Publisher()}.get)

    updates = await build_nodes(deps)["EXPORT"](_state())

    assert updates["published"]["note"] == NO_ACTUATOR_NOTE


async def test_a_simulated_publish_says_so_all_the_way_up() -> None:
    """A missing credential means the FAKE actuator, and every surface has to be able
    to tell its output from a real post. `fake` is what carries that."""
    deps, _ = _wired(FakeActuator(SOCIAL_POST_ACTION), FakeActuator(PAGE_PUBLISH_ACTION))

    updates = await build_nodes(deps)["EXPORT"](_state())

    report = updates["published"]
    assert report["simulated"] is True
    assert all(ref["fake"] for ref in report["refs"])
    assert all("SIMULATED" in ref["summary"] for ref in report["refs"])
    assert "SIMULATED" in report["note"], (
        "the one-line note is what a screen renders; on its own it would otherwise say "
        "'Published 3 of 3' about three posts that never left this process"
    )


async def test_an_integration_missing_for_one_action_type_only_loses_that_one() -> None:
    """Configured integrations, but none that can publish a page. Distinct from "no
    integration at all", and the run has to be able to say which it was."""
    deps, _ = _wired(Publisher())

    updates = await build_nodes(deps)["EXPORT"](_state())

    report = updates["published"]
    assert report["not_published"] == ["landing_page"]
    assert [ref["target"] for ref in report["refs"] if ref["status"] == "succeeded"] == [
        "linkedin",
        "facebook",
    ]
    assert "no actuator is configured" in updates["errors"][-1].message


async def test_nothing_to_publish_is_its_own_stated_reason() -> None:
    """Three ways to publish nothing, three different people who need to act on it."""
    deps, _ = _wired(Publisher())

    updates = await build_nodes(deps)["EXPORT"](_state(landing_page=None, renderings={}))

    assert _codes(updates) == ["nothing_to_publish"]
    assert "no channel rendering and no landing page" in updates["published"]["note"]


# --------------------------------------------------------------------------- #
# EXPORT: idempotency, through the node
# --------------------------------------------------------------------------- #


async def test_publishing_the_same_run_twice_replays_and_does_not_post_again() -> None:
    """The contract's second rule, exercised where it matters: a replay returns the
    FIRST result and never calls the provider again. "Posted" and "already posted" are
    different facts about the run and the same fact about the world."""
    posts = Publisher()
    deps, ledger = _wired(posts, store=Ledger())
    export = build_nodes(deps)["EXPORT"]
    state = _state(landing_page=None)

    first = await export(state)
    second = await export(state)

    assert posts.sent == ["linkedin", "facebook"], "the provider was called once per channel"
    assert all(ref["replayed"] is False for ref in first["published"]["refs"])
    assert all(ref["replayed"] is True for ref in second["published"]["refs"])
    assert all(ref["status"] == "succeeded" for ref in second["published"]["refs"])
    assert ledger.targets(SOCIAL_POST_ACTION) == ["linkedin", "facebook"] * 2


async def test_an_edited_post_is_a_different_effect_and_does_go_out() -> None:
    """The other half of content-derived keys, and the reason they are not uuids: a
    corrected post has to publish, or the idempotency guard becomes a censor."""
    posts = Publisher()
    deps, _ = _wired(posts, store=Ledger())
    export = build_nodes(deps)["EXPORT"]

    await export(_state(landing_page=None, renderings={"linkedin": "first wording"}))
    await export(_state(landing_page=None, renderings={"linkedin": "corrected wording"}))

    assert posts.sent == ["linkedin", "linkedin"]


# --------------------------------------------------------------------------- #
# EXPORT: telling the owner
# --------------------------------------------------------------------------- #


async def test_the_owner_is_told_what_went_live_and_what_did_not() -> None:
    notifier = Publisher(NOTIFY_ACTION)
    deps, ledger = _wired(Publisher(dead="facebook"), notifier)

    updates = await build_nodes(deps)["EXPORT"](_state(landing_page=None))

    assert updates["published"]["notified"] is True
    message = next(a for a in ledger.claimed if a.action_type == NOTIFY_ACTION)
    assert message.target == "chef@mueller.de"
    assert message.payload["published"] == ["linkedin"]
    assert message.payload["not_published"] == ["facebook"]


async def test_the_owner_is_told_even_when_everything_failed() -> None:
    """Especially then. A notifier that only fires on success is how a silent failure
    stays silent."""
    notifier = Publisher(NOTIFY_ACTION)
    deps, ledger = _wired(Publisher(dead="linkedin"), notifier)

    await build_nodes(deps)["EXPORT"](
        _state(landing_page=None, renderings={"linkedin": "only channel"})
    )

    message = next(a for a in ledger.claimed if a.action_type == NOTIFY_ACTION)
    assert message.payload["published"] == []


async def test_no_email_on_record_is_named_rather_than_silently_skipped() -> None:
    deps, ledger = _wired(Publisher(), Publisher(NOTIFY_ACTION))

    updates = await build_nodes(deps)["EXPORT"](
        _state(landing_page=None, dna={"name": "Müller Sanitär GmbH"})
    )

    assert updates["published"]["notified"] is False
    assert "no email address on record" in updates["published"]["notify_note"]
    assert ledger.targets(NOTIFY_ACTION) == []


async def test_one_message_per_run_and_not_one_per_channel() -> None:
    notifier = Publisher(NOTIFY_ACTION)
    deps, ledger = _wired(Publisher(), notifier)

    await build_nodes(deps)["EXPORT"](_state(landing_page=None))

    assert ledger.targets(NOTIFY_ACTION) == ["chef@mueller.de"]


# --------------------------------------------------------------------------- #
# EXPORT: the allowlist barrier
# --------------------------------------------------------------------------- #


def test_export_is_the_only_node_that_can_publish_and_it_holds_nothing_else() -> None:
    """docs/AGENT_RUNTIME.md section 3's "second of three independent
    prompt-injection barriers", asserted on the node set rather than on the table."""
    from backend.app.agents.tools import NODE_TOOLS, allowed_tools

    assert allowed_tools("EXPORT") == {PUBLISH, NOTIFY}
    assert not any(
        PUBLISH in tools or NOTIFY in tools
        for node, tools in NODE_TOOLS.items()
        if node != "EXPORT"
    )


async def test_a_revoked_publish_grant_stops_the_publishing_and_says_which_it_was() -> None:
    """The operator kill switch, at the one node where it matters most -- and it must
    not be reported as "no integration configured". One of those is a decision somebody
    made and the other is a deployment gap, and reading the first as the second sends
    somebody to wire a publisher that is already there."""
    posts = Publisher()
    ledger = Ledger()
    deps = _deps(
        actuator_for={SOCIAL_POST_ACTION: posts}.get,
        actuator_store=ledger,
        revoked_tools={"EXPORT": frozenset({PUBLISH})},
    )

    updates = await build_nodes(deps)["EXPORT"](_state(landing_page=None))

    assert posts.sent == []
    assert ledger.claimed == []
    assert updates["published"]["note"] == PUBLISH_REVOKED_NOTE
    assert _codes(updates) == ["publish_revoked"]


# --------------------------------------------------------------------------- #
# MEASURE
# --------------------------------------------------------------------------- #


def _published(**over: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "approved_by": APPROVER,
        "attempted": 2,
        "refs": [
            {"target": "linkedin", "status": "succeeded", "fake": False},
            {"target": "facebook", "status": "failed", "fake": False},
        ],
        "not_published": ["facebook"],
        "simulated": False,
        "note": "Published 1 of 2",
    }
    report.update(over)
    return report


def _probe(**over: Any) -> dict[str, Any]:
    probe: dict[str, Any] = {
        "headline": "mentioned in 3 of 9 usable answers",
        "mention_share_pct": 33.3,
        "unprompted_mention_share_pct": 11.1,
        "usable_answers": 9,
        "no_answer_count": 3,
        "using_fake_provider": False,
        "caveats": ["a sample, not a census"],
    }
    probe.update(over)
    return probe


async def test_measure_reads_the_published_refs_and_counts_only_what_went_live() -> None:
    updates = await build_nodes(_deps())["MEASURE"](_state(published=_published()))

    report = updates["measurement"]
    assert report["published_refs"] == 1, "a failed channel is not something to measure"
    assert report["channels"] == ["linkedin"]


async def test_measure_names_the_analytics_gap_it_will_never_fill() -> None:
    """GSC/GA4 is cut from this build, so the grant is held and unwired. A measurement
    that silently omits search traffic reads as one that looked and found nothing."""
    updates = await build_nodes(_deps())["MEASURE"](_state(published=_published()))

    assert ANALYTICS_GAP in updates["measurement"]["gaps"]
    assert "cut from this build" in ANALYTICS_GAP


def test_analytics_fetch_is_granted_and_deliberately_left_unwired() -> None:
    """The wiring is the claim, so the wiring is what is asserted. `_implementations`
    IS the map from tool name to capability, which is why this test reads it directly:
    an `analytics.fetch` entry appearing here -- least of all a fake one -- would turn
    a stated omission into a fabricated metric."""
    from backend.app.agents.nodes import _implementations
    from backend.app.agents.tools import allowed_tools

    everything = _implementations(_deps(geo_probe=lambda dna: None))

    assert ANALYTICS_FETCH in allowed_tools("MEASURE"), "the grant is deliberate, not forgotten"
    assert ANALYTICS_FETCH not in everything
    assert GEO_PROBE in everything, "the other grant IS wired, so this test can tell them apart"


async def test_measure_carries_the_harvest_baseline_forward_and_reports_no_movement() -> None:
    """A re-probe minutes after publishing asks the same models the same prompts, and
    calling the difference "movement" would report sampling noise as a result."""
    probed: list[Any] = []

    async def probe(dna: Any, **kwargs: Any) -> dict[str, Any]:
        probed.append(dna)
        return _probe()

    updates = await build_nodes(_deps(geo_probe=probe))["MEASURE"](
        _state(published=_published(), facts={"visibility": _probe()})
    )

    share = updates["measurement"]["share_of_voice"]
    assert probed == [], "HARVEST already measured it; this cycle must not re-probe"
    assert share["source"] == "harvest"
    assert share["baseline"]["measured"] is True
    assert share["baseline"]["headline"] == "mentioned in 3 of 9 usable answers"
    assert "movement" not in share, "a metric nobody measured is absent, never zero"
    assert "next cycle" in share["movement_note"]


async def test_measure_takes_the_first_measurement_when_harvest_had_none() -> None:
    async def probe(dna: Any, **kwargs: Any) -> dict[str, Any]:
        return _probe()

    updates = await build_nodes(_deps(geo_probe=probe))["MEASURE"](_state(published=_published()))

    share = updates["measurement"]["share_of_voice"]
    assert share["source"] == "measure"
    assert share["baseline"]["mention_share_pct"] == 33.3
    assert "nothing to compare it against" in share["movement_note"]


async def test_a_probe_whose_answers_were_all_no_answer_reports_no_share_not_zero() -> None:
    """`no_answer` is excluded from the denominator, which leaves NO denominator here.
    A model outage recorded as brand absence is the difference between a measurement
    and a fabrication."""
    updates = await build_nodes(_deps())["MEASURE"](
        _state(
            published=_published(),
            facts={"visibility": _probe(usable_answers=0, no_answer_count=9)},
        )
    )

    baseline = updates["measurement"]["share_of_voice"]["baseline"]
    assert baseline["measured"] is False
    assert baseline["no_answer_count"] == 9
    assert "mention_share_pct" not in baseline, "0% would read as a measured absence"
    assert "excluded from the denominator" in baseline["note"]


async def test_a_dead_probe_skips_the_cycle_rather_than_recording_an_absence() -> None:
    """ "provider down → skip the cycle, never corrupt the series"."""

    async def probe(dna: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("all providers refused")

    updates = await build_nodes(_deps(geo_probe=probe))["MEASURE"](_state(published=_published()))

    report = updates["measurement"]
    assert "share_of_voice" not in report
    assert any("the probe failed" in gap for gap in report["gaps"])
    assert any("all providers refused" in gap for gap in report["gaps"])


async def test_no_probe_configured_is_a_named_gap_and_not_a_zero() -> None:
    updates = await build_nodes(_deps())["MEASURE"](_state(published=_published()))

    report = updates["measurement"]
    assert "share_of_voice" not in report
    assert any("no probe configured" in gap for gap in report["gaps"])


async def test_measure_reports_the_attribution_path_and_refuses_a_lead_count() -> None:
    """A lead is counted when a visitor arrives through /go/{code} and submits the
    form, which is minutes to weeks after EXPORT. Zero here would be indistinguishable
    from a piece nobody has seen yet."""
    updates = await build_nodes(_deps())["MEASURE"](_state(published=_published()))

    attribution = updates["measurement"]["attribution"]
    assert attribution["channels"] == ["linkedin"]
    assert attribution["leads_measured"] is False
    assert "leads" not in attribution, "an absent metric has no key, let alone a 0"
    assert "not a result" in attribution["note"]


async def test_measure_says_nothing_is_live_when_export_published_nothing() -> None:
    updates = await build_nodes(_deps())["MEASURE"](
        _state(published={"refs": [], "note": NO_ACTUATOR_NOTE, "simulated": False})
    )

    report = updates["measurement"]
    assert report["published_refs"] == 0
    assert "nothing to measure" in report["note"]
    assert NO_ACTUATOR_NOTE in report["note"], "the reason travels with the absence"


async def test_measure_carries_the_simulation_flag_so_no_metric_is_read_as_real() -> None:
    """A simulated publish has no audience, so anything measured downstream of it would
    be measuring us."""
    updates = await build_nodes(_deps())["MEASURE"](
        _state(published=_published(simulated=True, refs=[]))
    )

    assert updates["measurement"]["simulated"] is True


async def test_measure_survives_a_run_where_export_never_ran() -> None:
    """MEASURE is reachable on its own, and a missing `published` is not a crash: the
    honest answer is that nothing is live."""
    updates = await build_nodes(_deps())["MEASURE"](_state())

    assert updates["measurement"]["published_refs"] == 0


async def test_measure_calls_no_model() -> None:
    """It is an engine node: everything it reports is read or counted, never judged."""

    class Exploding:
        async def complete(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("MEASURE must not call a model")

    measure = build_nodes(_deps(router=Exploding()))["MEASURE"]

    updates = await measure(_state(published=_published()))

    assert updates["measurement"]["published_refs"] == 1


async def test_export_calls_no_model_either() -> None:
    class Exploding:
        async def complete(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("EXPORT must not call a model")

    deps, _ = _wired(Publisher(), router=Exploding())

    updates = await build_nodes(deps)["EXPORT"](_state(landing_page=None))

    assert updates["published"]["attempted"] == 2


@pytest.mark.parametrize("node", ["EXPORT", "MEASURE"])
async def test_neither_node_charges_the_run_budget(node: str) -> None:
    """No model, so no cost -- and a `_cost` key here would silently spend a run's
    remaining budget on bookkeeping."""
    deps, _ = _wired(Publisher(), FakeActuator(PAGE_PUBLISH_ACTION))

    updates = await build_nodes(deps)[node](_state(published=_published()))

    assert "_cost" not in updates
