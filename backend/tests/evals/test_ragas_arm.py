"""The Ragas arm: batching, mapping, and every way it can fail to measure.

This arm runs Ragas in a SEPARATE interpreter (`.venv-ragas`), because `ragas` caps
`openai<3.0.0` and this project pins `openai>=3.2.0`. That makes the subprocess
boundary the thing worth testing: what is sent across it, what comes back, and what
happens for each of the several ways it can produce nothing.

Hermetic by construction. No test here spawns a real subprocess, imports Ragas, or
touches the network — the spawn is replaced, so what is exercised is our contract with
the child rather than Ragas itself.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backend.app.llm.router import ModelRouter
from evals.judged import ANSWER_RELEVANCY, FAITHFULNESS
from evals.ragas_arm import MAX_SAMPLES, RagasArm, ragas_arm_off_status

pytestmark = pytest.mark.anyio

#: A key that exists, so the arm gets past its credential check. Never used to reach
#: anything: the spawn is always replaced.
ENV: dict[str, str] = {"OPENROUTER_API_KEY": "test-key-not-real"}


def _arm(tmp_path: Path, *, requested: bool = True, env: dict[str, str] | None = None) -> RagasArm:
    """An arm whose prerequisites all exist, so failures under test are the real ones."""
    python = tmp_path / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n")
    runner = tmp_path / "ragas_runner.py"
    runner.write_text("# stand-in\n")
    return RagasArm(
        ModelRouter(env={}),
        requested=requested,
        env=ENV if env is None else env,
        python=python,
        runner=runner,
    )


def _record_two(arm: RagasArm) -> None:
    arm.record(
        case_id="plumber-01",
        arm="rag_off",
        request="What does a Notdienst cost?",
        output="It costs about 149 EUR at night.",
        retrieval_context=(),
    )
    arm.record(
        case_id="plumber-01",
        arm="rag_on",
        request="What does a Notdienst cost?",
        output="It costs 149 EUR at night [chunk:plumber-01#0].",
        retrieval_context=("Notdienst nachts ab 149 EUR.",),
    )


def _child(payload: dict[str, Any]) -> Any:
    """Replace the spawn with a function returning what a child would have written."""

    async def spawn(_payload: dict[str, Any]) -> dict[str, Any] | str:
        payload["sent"] = _payload
        reply: dict[str, Any] | str = payload["reply"]
        return reply

    return spawn


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #


async def test_the_arm_is_inert_unless_it_is_asked_for(tmp_path: Path) -> None:
    """CI runs without `--ragas`, and the guarantee is structural rather than
    asserted: nothing is recorded, nothing is spawned, and Ragas is never imported."""
    arm = _arm(tmp_path, requested=False)

    assert (
        arm.record(case_id="c1", arm="rag_on", request="q", output="a", retrieval_context=("ctx",))
        is None
    )
    assert await arm.resolve() == {}
    assert arm.status() == ragas_arm_off_status()
    assert arm.status().requested is False


async def test_asking_for_it_with_nothing_recorded_says_so(tmp_path: Path) -> None:
    """Distinct from "the arm was off": the flag WAS passed, and a header that
    conflated the two would hide a harness bug that judged nothing."""
    arm = _arm(tmp_path)

    assert await arm.resolve() == {}
    status = arm.status()
    assert status.requested is True
    assert "no samples" in status.note


# --------------------------------------------------------------------------- #
# The payload sent across the boundary
# --------------------------------------------------------------------------- #


async def test_the_judge_model_is_resolved_from_our_routing_table(tmp_path: Path) -> None:
    """The one thing the out-of-process design keeps from `ModelRouter`. Without it the
    child would need a model id of its own, which is exactly the hardcoded call site
    `CLAUDE.md` forbids — and a tier change would silently stop moving the judge."""
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {"reply": {"results": {}, "calls": 0}}
    arm._spawn = _child(box)  # type: ignore[method-assign]

    await arm.resolve()

    expected = ModelRouter(env={}).resolve(
        __import__("backend.app.llm.contract", fromlist=["TaskClass"]).TaskClass.REVIEW
    )
    assert box["sent"]["model"] == expected.chain[0].model


async def test_an_arm_with_no_retrieval_context_is_sent_truthfully(tmp_path: Path) -> None:
    """`rag_off` has no context by definition. It is sent as an empty list rather than
    padded with the oracle or the question: faithfulness must come back NOT MEASURED
    there, and inventing a context would make it come back as a score instead."""
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {"reply": {"results": {}, "calls": 0}}
    arm._spawn = _child(box)  # type: ignore[method-assign]

    await arm.resolve()

    sent = {sample["key"]: sample for sample in box["sent"]["samples"]}
    assert sent["plumber-01::rag_off"]["contexts"] == []
    assert sent["plumber-01::rag_on"]["contexts"] == ["Notdienst nachts ab 149 EUR."]


async def test_the_payload_is_json_serialisable(tmp_path: Path) -> None:
    """It crosses a process boundary as JSON. A tuple or a Decimal in there is a
    `TypeError` at spawn time, which would take the whole arm down."""
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {"reply": {"results": {}, "calls": 0}}
    arm._spawn = _child(box)  # type: ignore[method-assign]

    await arm.resolve()

    json.dumps(box["sent"])  # raises if anything in it is not serialisable


async def test_more_samples_than_the_ceiling_are_refused_and_named(tmp_path: Path) -> None:
    """The budget guard is what running out-of-process gives up, so the sample ceiling
    stands in for it. Silently dropping the overflow would make a truncated eval look
    like a complete one."""
    arm = _arm(tmp_path)
    for index in range(MAX_SAMPLES + 3):
        arm.record(
            case_id=f"case-{index}", arm="rag_on", request="q", output="a", retrieval_context=()
        )

    assert len(arm.errors) == 3
    assert all("was not judged" in error for error in arm.errors)


# --------------------------------------------------------------------------- #
# Mapping what comes back
# --------------------------------------------------------------------------- #


async def test_scores_are_mapped_onto_both_arms(tmp_path: Path) -> None:
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {
        "reply": {
            "results": {
                "plumber-01::rag_off": {FAITHFULNESS: None, ANSWER_RELEVANCY: 0.5, "errors": {}},
                "plumber-01::rag_on": {FAITHFULNESS: 0.875, ANSWER_RELEVANCY: 0.9, "errors": {}},
            },
            "calls": 12,
            "tokens_in": 4000,
            "tokens_out": 500,
        }
    }
    arm._spawn = _child(box)  # type: ignore[method-assign]

    judged = await arm.resolve()

    assert judged["plumber-01::rag_on"].faithfulness.score == 0.875
    assert judged["plumber-01::rag_off"].faithfulness.measured is False
    assert judged["plumber-01::rag_off"].answer_relevancy.score == 0.5
    status = arm.status()
    assert status.calls == 12
    assert status.measured == 3, "three of four metric slots produced a number"
    assert status.attempted == 4


async def test_a_score_for_an_empty_context_is_discarded_not_reported(tmp_path: Path) -> None:
    """MEASURED, not assumed. Driven against a stub judge, Ragas returned
    `faithfulness = 1.00` for a sample whose contexts were EMPTY — it has nothing there
    to contradict, so every claim passes by default. That would put the report's best
    number on its least grounded output, so the score is thrown away and the reason kept.

    This is the same refusal the DeepEval arm makes before calling. Here it has to
    happen on the RETURN, because it must survive whatever the library decides to
    compute.
    """
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {
        "reply": {
            "results": {
                # Exactly what a real run produced: 1.0 on the arm with no context.
                "plumber-01::rag_off": {FAITHFULNESS: 1.0, ANSWER_RELEVANCY: None, "errors": {}},
                "plumber-01::rag_on": {FAITHFULNESS: 1.0, ANSWER_RELEVANCY: None, "errors": {}},
            },
            "calls": 4,
        }
    }
    arm._spawn = _child(box)  # type: ignore[method-assign]

    judged = await arm.resolve()

    ungrounded = judged["plumber-01::rag_off"].faithfulness
    assert ungrounded.score is None, "1.00 on an ungrounded arm must not reach the report"
    assert "unverifiable, not faithful" in (ungrounded.error or "")
    # The grounded arm's score is untouched: the rule is about missing context, not
    # about distrusting the judge.
    assert judged["plumber-01::rag_on"].faithfulness.score == 1.0


async def test_the_spend_is_priced_with_our_own_table(tmp_path: Path) -> None:
    """The judge call does not go through our router, so its cost would otherwise
    vanish from a report where every other number is accounted for. The child reports
    tokens; the price comes from `llm/pricing.py` like everything else."""
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {
        "reply": {"results": {}, "calls": 4, "tokens_in": 10_000, "tokens_out": 2_000}
    }
    arm._spawn = _child(box)  # type: ignore[method-assign]

    await arm.resolve()

    assert arm.status().cost_usd >= Decimal(0)
    assert arm.status().tokens_in == 10_000


async def test_a_nan_or_missing_score_is_not_measured_and_never_zero(tmp_path: Path) -> None:
    """The whole discipline of this harness in one assertion. A judge that returned
    nothing has told us nothing about the text; 0.00 would say the text is entirely
    unfaithful, and a reader cannot tell those apart afterwards."""
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {
        "reply": {
            "results": {
                "plumber-01::rag_on": {
                    FAITHFULNESS: None,
                    ANSWER_RELEVANCY: None,
                    "errors": {FAITHFULNESS: "ragas returned no usable score"},
                }
            },
            "calls": 2,
        }
    }
    arm._spawn = _child(box)  # type: ignore[method-assign]

    judged = await arm.resolve()

    outcome = judged["plumber-01::rag_on"].faithfulness
    assert outcome.score is None
    assert outcome.cell() == "n/m"
    assert "no usable score" in (outcome.error or "")


async def test_a_sample_the_child_did_not_answer_still_gets_a_reason(tmp_path: Path) -> None:
    """A missing key must not become a missing row: every recorded arm gets a cell, and
    every cell that is not a number carries why."""
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {"reply": {"results": {}, "calls": 1}}
    arm._spawn = _child(box)  # type: ignore[method-assign]

    judged = await arm.resolve()

    assert set(judged) == {"plumber-01::rag_off", "plumber-01::rag_on"}
    assert judged["plumber-01::rag_on"].faithfulness.error


# --------------------------------------------------------------------------- #
# Every way it can fail to run
# --------------------------------------------------------------------------- #


async def test_a_missing_environment_is_reported_with_the_command_that_fixes_it(
    tmp_path: Path,
) -> None:
    """The most likely failure by far, and the one where a vague message costs the most:
    somebody passed `--ragas` without building the venv."""
    arm = RagasArm(
        ModelRouter(env={}),
        requested=True,
        env=ENV,
        python=tmp_path / "nonexistent" / "python",
        runner=tmp_path / "runner.py",
    )
    _record_two(arm)

    judged = await arm.resolve()
    status = arm.status()

    assert status.available is False
    assert "make ragas-env" in status.note
    assert judged["plumber-01::rag_on"].faithfulness.measured is False
    assert "make ragas-env" in (judged["plumber-01::rag_on"].faithfulness.error or "")


async def test_no_credential_means_nothing_judged_rather_than_judged_by_nothing(
    tmp_path: Path,
) -> None:
    """The in-process arm can fall back to FakeProvider and say so. This one cannot —
    there is no fake on the other side of a subprocess — so it refuses instead of
    producing numbers from an unconfigured endpoint."""
    arm = _arm(tmp_path, env={})
    _record_two(arm)

    await arm.resolve()

    assert arm.status().available is False
    assert "OPENROUTER_API_KEY" in arm.status().note


async def test_a_child_that_reports_an_error_degrades_rather_than_raising(
    tmp_path: Path,
) -> None:
    """An optional arm must never take the deterministic scorers down with it — those
    are the ones that gate drafts."""
    arm = _arm(tmp_path)
    _record_two(arm)

    async def failing(_payload: dict[str, Any]) -> dict[str, Any] | str:
        return "the Ragas subprocess did not finish within 90s and was killed"

    arm._spawn = failing  # type: ignore[method-assign,assignment]

    judged = await arm.resolve()

    assert "was killed" in arm.status().note
    assert all(not entry.any_measured for entry in judged.values())
    assert arm.errors, "the failure has to reach the run notes"


async def test_a_results_block_that_is_not_a_mapping_is_survived(tmp_path: Path) -> None:
    """The child is a separate program; its output is parsed defensively for the same
    reason the run checkpoint is."""
    arm = _arm(tmp_path)
    _record_two(arm)
    box: dict[str, Any] = {"reply": {"results": ["not", "a", "mapping"], "calls": 0}}
    arm._spawn = _child(box)  # type: ignore[method-assign]

    judged = await arm.resolve()

    assert all(not entry.any_measured for entry in judged.values())
    assert "no results block" in arm.status().note
