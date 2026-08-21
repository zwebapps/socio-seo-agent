"""AgentState and the control rules that bound a run.

Written first. The interesting properties of this state machine are not "does it
produce an article" — they are the ones that stop it running forever, spending
without limit, or losing work on a crash. Those are tested here, before the graph
exists, because they are the parts that cannot be bolted on afterwards.
"""

from decimal import Decimal

import pytest

from backend.app.agents.state import (
    DEFAULT_CHANNELS,
    AgentState,
    CapExceededError,
    NodeError,
    RunCaps,
    channels_of,
    charge,
    from_checkpoint,
    new_state,
    normalise_channels,
    record_error,
    step,
    to_checkpoint,
)


def _state(caps: RunCaps | None = None) -> AgentState:
    return new_state(
        business_id="11111111-1111-1111-1111-111111111111", goal="more leads", caps=caps
    )


def test_a_new_run_starts_empty_and_unspent() -> None:
    state = _state()
    assert state["step_count"] == 0
    assert state["cost_usd"] == Decimal("0")
    assert state["errors"] == []
    assert state["validate_loops"] == 0


def test_steps_are_counted_and_capped() -> None:
    caps = RunCaps(max_steps=3, max_usd=Decimal("1"), max_validate_loops=2)
    state = _state(caps)

    for expected in (1, 2, 3):
        state = step(state, "HARVEST")
        assert state["step_count"] == expected

    with pytest.raises(CapExceededError) as exc:
        step(state, "GENERATE")
    assert exc.value.cap == "max_steps"
    assert "3" in str(exc.value), "the message must name the limit that was hit"


def test_cost_is_charged_and_capped_before_it_is_exceeded() -> None:
    """The guard refuses the call that WOULD exceed, not the one that already did."""
    caps = RunCaps(max_steps=99, max_usd=Decimal("0.10"), max_validate_loops=2)
    state = charge(_state(caps), Decimal("0.06"))
    assert state["cost_usd"] == Decimal("0.06")

    with pytest.raises(CapExceededError) as exc:
        charge(state, Decimal("0.05"))
    assert exc.value.cap == "max_usd"
    assert state["cost_usd"] == Decimal("0.06"), "a refused charge must not be applied"


def test_money_is_decimal_never_float() -> None:
    state = charge(_state(), Decimal("0.1"))
    state = charge(state, Decimal("0.2"))
    assert state["cost_usd"] == Decimal("0.3"), "float arithmetic would give 0.30000000000000004"


def test_errors_accumulate_rather_than_raising() -> None:
    """A failed fact source degrades the run; it does not end it."""
    state = record_error(_state(), NodeError(node="HARVEST", code="serp_quota", message="quota"))
    state = record_error(state, NodeError(node="HARVEST", code="robots", message="disallowed"))

    assert len(state["errors"]) == 2
    assert {e.code for e in state["errors"]} == {"serp_quota", "robots"}


def test_state_survives_a_json_round_trip() -> None:
    """The checkpoint is JSON in Postgres. A state that cannot serialise cannot
    resume, and a run that cannot resume loses the work already paid for."""
    import json

    from backend.app.agents.state import from_checkpoint, to_checkpoint

    state = record_error(
        charge(step(_state(), "PLAN"), Decimal("0.02")),
        NodeError(node="PLAN", code="thin", message="not much to go on"),
    )

    restored = from_checkpoint(json.loads(json.dumps(to_checkpoint(state))))

    assert restored["step_count"] == state["step_count"]
    assert restored["cost_usd"] == state["cost_usd"]
    assert isinstance(restored["cost_usd"], Decimal), "Decimal must survive, not become float"
    assert [e.code for e in restored["errors"]] == ["thin"]


def test_the_run_id_survives_the_round_trip_as_a_string() -> None:
    """`run_id` is what attributes a published page to the run that made it.

    A string rather than a `UUID` for the same reason `business_id` is one: this goes
    into a JSONB column, and a `UUID` does not survive `json.dumps`.
    """
    import json
    from uuid import uuid4

    from backend.app.agents.state import from_checkpoint, run_uuid, to_checkpoint

    run = uuid4()
    state = new_state(business_id="11111111-1111-1111-1111-111111111111", goal="x", run_id=run)

    assert state["run_id"] == str(run)
    restored = from_checkpoint(json.loads(json.dumps(to_checkpoint(state))))
    assert restored["run_id"] == str(run)
    assert run_uuid(restored) == run, "the actuation needs it back as a UUID"


def test_a_checkpoint_written_before_the_run_id_existed_still_reads() -> None:
    """Nothing migrates a JSONB column, so an older row has no `run_id` at all -- and a
    run that cannot resume loses work a customer already paid for."""
    from backend.app.agents.state import from_checkpoint, run_uuid, to_checkpoint

    old = to_checkpoint(_state())
    del old["run_id"]
    assert "run_id" not in old

    revived = from_checkpoint(old)
    assert revived["run_id"] is None, "a missing key reads as unattributed, not as a crash"
    assert run_uuid(revived) is None


@pytest.mark.parametrize("junk", ["", "   ", "latest", "not-a-uuid", 7, None, ["x"], {}])
def test_a_hand_edited_run_id_reads_as_unattributed_rather_than_reaching_a_foreign_key(
    junk: object,
) -> None:
    """This column can hold whatever an UPDATE, a backup restore or a copied row put in
    it. The value ends up in `Actuation.run_id`, which is typed `UUID | None` and lands
    in a foreign key -- so anything unparseable must read as "not attributed" rather
    than crash the publish path or write a reference to nothing."""
    from backend.app.agents.state import from_checkpoint

    assert from_checkpoint({"run_id": junk, "errors": []})["run_id"] is None


def test_a_run_id_is_normalised_rather_than_copied_through() -> None:
    """Whitespace and case are the same id; the stored form must be the canonical one,
    or the same run reads as two in a `GROUP BY`."""
    from backend.app.agents.state import from_checkpoint

    canonical = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert from_checkpoint({"run_id": f"  {canonical.upper()}  ", "errors": []})["run_id"] == (
        canonical
    )


def test_caps_have_the_documented_defaults() -> None:
    """14 steps and $0.50 per run, from docs/AGENT_RUNTIME.md section 8."""
    caps = RunCaps()
    assert caps.max_steps == 14
    assert caps.max_usd == Decimal("0.50")
    assert caps.max_validate_loops == 2


# --------------------------------------------------------------------------- #
# channels — per-run, checkpointed, and normalised on the way in
# --------------------------------------------------------------------------- #


def test_channels_default_when_the_caller_does_not_choose() -> None:
    """The behaviour every run had before the field existed, preserved exactly."""
    assert channels_of(_state()) == DEFAULT_CHANNELS


def test_a_chosen_channel_set_survives_the_checkpoint_round_trip() -> None:
    """The whole point of the key.

    As a `NodeDeps` field this was rebuilt from the default on every resume, so a run
    started for LinkedIn alone came back from its checkpoint targeting three channels
    and published two posts nobody asked for.
    """
    state = new_state(
        business_id="11111111-1111-1111-1111-111111111111",
        goal="more leads",
        channels=["linkedin"],
    )
    assert channels_of(from_checkpoint(to_checkpoint(state))) == ("linkedin",)


def test_a_checkpoint_written_before_channels_existed_resumes_on_the_default() -> None:
    """A run must never fail to resume over a field a default answers correctly."""
    payload = to_checkpoint(_state())
    del payload["channels"]
    assert channels_of(from_checkpoint(payload)) == DEFAULT_CHANNELS


def test_an_unknown_channel_is_dropped_rather_than_carried() -> None:
    """A channel with no spec cannot be length- or link-checked.

    `actuators/social.py` already refuses one for that reason, so carrying it here
    would only produce a rendering that nothing downstream could validate.
    """
    assert normalise_channels(["linkedin", "threads"]) == ["linkedin"]


def test_channel_aliases_collapse_to_one_channel() -> None:
    """`facebook_post` and `facebook` are the same post, not two of them."""
    assert normalise_channels(["facebook_post", "facebook"]) == ["facebook"]


def test_an_empty_channel_set_reads_as_the_default_not_as_nothing() -> None:
    """A run targeting nothing would pass VALIDATE, reach REPACK and render zero
    posts — a silently empty deliverable. The default is the honest reading of
    "the caller did not choose"."""
    assert normalise_channels([]) == list(DEFAULT_CHANNELS)


@pytest.mark.parametrize("raw", [None, "linkedin", 7, {"linkedin": True}, [None, 3]])
def test_a_hand_edited_channel_column_reads_as_the_default(raw: object) -> None:
    """This column can hold whatever an UPDATE put there. A bare string is the
    interesting case: it is iterable, so a naive reader would render posts for the
    channels `l`, `i`, `n`, ..."""
    assert normalise_channels(raw) == list(DEFAULT_CHANNELS)
