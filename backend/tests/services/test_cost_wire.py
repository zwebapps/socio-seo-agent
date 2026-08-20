"""The cost report's WIRE SHAPE: camelCase keys, and money as strings.

This file exists because of a bug that got all the way to a rendered screen. `CostReport`
was a plain `BaseModel`, and it is returned NESTED inside `api/cost.CostOut`. FastAPI's
`response_model_by_alias` applies the OUTER model's alias generator and does not reach
into a nested model that has none — so the response came back with a camelCase
`businessId` wrapping a wholly snake_case report, and the frontend read `undefined` for
every field on the page.

Nothing typed caught it: both sides were internally consistent, and the mismatch existed
only in the JSON between them. It was found by calling the endpoint with curl. These tests
are what make that unnecessary next time.

They are deliberately NOT database tests, so they run on a machine with no Postgres — the
serialisation shape is not a database concern and should not be gated behind one.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest

from backend.app.llm import Completion, Usage
from backend.app.services.cost_service import CostReport, DailySpend, RunSpend, SpendRow
from backend.app.services.kb_service import RETRIEVAL_PROMPT_VERSION, retrieve
from backend.app.services.run_executor import KB_NODE

pytestmark = pytest.mark.anyio


def _report() -> CostReport:
    """A fully populated report, so every nested model is exercised."""
    return CostReport(
        window_days=30,
        since=datetime.now(UTC),
        calls=2,
        tokens_in=100,
        tokens_out=200,
        total_usd="0.02100000",
        by_model=[
            SpendRow(
                key="openai/gpt-4.1",
                calls=1,
                tokens_in=50,
                tokens_out=100,
                usd="0.02000000",
                priced=True,
            )
        ],
        by_node=[],
        by_prompt_version=[],
        by_day=[DailySpend(day=datetime.now(UTC).date(), calls=2, usd="0.02100000")],
        default_run_cap_usd="0.50000000",
        runs_in_window=1,
        runs_at_cap=0,
        top_runs=[RunSpend(run_id=uuid4(), usd="0.02100000", cap_usd="0.50000000", at_cap=False)],
        ledger_wired=True,
        message="fixture",
    )


def test_every_multi_word_field_serialises_as_camel_case() -> None:
    """The bug: these went out as `total_usd`, `by_model`, `ledger_wired`, and the screen
    read undefined for all of them."""
    body = _report().model_dump(by_alias=True)

    expected = {
        "windowDays",
        "totalUsd",
        "tokensIn",
        "tokensOut",
        "byModel",
        "byNode",
        "byPromptVersion",
        "byDay",
        "defaultRunCapUsd",
        "runsInWindow",
        "runsAtCap",
        "topRuns",
        "ledgerWired",
    }
    assert expected <= set(body), f"missing camelCase keys: {sorted(expected - set(body))}"

    snake = {key for key in body if "_" in key}
    assert not snake, f"snake_case keys on the wire: {sorted(snake)}"


def test_the_nested_models_are_camel_case_too() -> None:
    """The failure was specifically about NESTING: an outer alias generator does not reach
    into a nested model that has none."""
    body = _report().model_dump(by_alias=True)

    row = body["byModel"][0]
    assert {"tokensIn", "tokensOut"} <= set(row)
    assert "tokens_in" not in row

    run = body["topRuns"][0]
    assert {"runId", "capUsd", "atCap"} <= set(run)
    assert "run_id" not in run


def test_money_is_a_string_and_never_a_float() -> None:
    """`Decimal` end to end, serialised as text. A JSON number here would let a client
    parse it into a float and reintroduce the drift the backend exists to avoid."""
    body = _report().model_dump(by_alias=True)

    for key in ("totalUsd", "defaultRunCapUsd"):
        assert isinstance(body[key], str), f"{key} is {type(body[key]).__name__}, not a string"
    assert isinstance(body["byModel"][0]["usd"], str)
    assert isinstance(body["topRuns"][0]["capUsd"], str)


def test_a_field_can_still_be_populated_by_its_python_name() -> None:
    """`populate_by_name` is what lets the service construct these with snake_case
    keywords while the wire stays camel. Without it every construction site would have to
    write camelCase, which would be the tail wagging the dog."""
    row = SpendRow(key="m", calls=1, tokens_in=1, tokens_out=1, usd="0", priced=None)

    assert row.tokens_in == 1
    assert row.model_dump(by_alias=True)["tokensIn"] == 1


def test_money_formatting_is_consistent_across_an_empty_and_a_populated_figure() -> None:
    """A report showing `$0` beside `$0.02100000` looks like two different units. The
    quantum comes from the price table, so it matches the ledger column's own scale."""
    from backend.app.services.cost_service import _usd

    assert _usd(None) == "0.00000000"
    assert _usd(Decimal("0")) == "0.00000000"
    assert _usd(Decimal("0.021")) == "0.02100000"
    # A run cap is stored at a different scale (Numeric(10, 6)) and must still render the
    # same way, or the dashboard mixes formats within one row.
    assert _usd(Decimal("0.500000")) == "0.50000000"


async def test_a_retrieval_call_is_attributed_to_a_step_and_not_to_nothing() -> None:
    """Found by driving a real run the first time retrieval was wired into the graph.

    The agentic loop makes three cheap calls per attempt — decide, rewrite, grade — and
    every one of them wrote a `model_usage` row with an EMPTY `node`, so eighteen of the
    twenty-six rows in the ledger belonged to no step. This repo has already fixed
    exactly this once, for llm spans; an unattributed cost row is a number nobody can
    act on, which is the whole reason the ledger exists.

    `KB` rather than a graph node name, because the calls belong to the retrieval loop
    and borrowing HARVEST's name would put another node's spend in its column.
    """
    seen: list[dict[str, str] | None] = []

    class _Router:
        async def complete(self, task: object, messages: object, **kw: object) -> Completion:
            trace = kw.get("trace")
            seen.append(trace if trace is None or isinstance(trace, dict) else None)
            return Completion(
                text="prose, so the loop stops asking",
                tool_calls=[],
                usage=Usage(
                    provider="stub",
                    model="stub/m",
                    tokens_in=10,
                    tokens_out=5,
                    usd=Decimal("0.00001"),
                    latency_ms=3,
                ),
                is_final=True,
            )

    class _Embedder:
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.0] * 8 for _ in texts]

    class _Store:
        async def upsert(self, *args: object, **kwargs: object) -> int:
            return 0

        async def search(self, *args: object, **kwargs: object) -> list[object]:
            return []

        async def existing_hashes(self, *args: object, **kwargs: object) -> set[str]:
            return set()

    await retrieve(
        "was kostet eine rohrreinigung",
        business_id=uuid4(),
        router=cast("Any", _Router()),
        embedder=cast("Any", _Embedder()),
        store=cast("Any", _Store()),
        trace={"node": KB_NODE, "prompt_version": RETRIEVAL_PROMPT_VERSION},
    )

    assert seen, "the loop must have made at least one call"
    assert all(entry is not None and entry.get("node") == KB_NODE for entry in seen), (
        "every retrieval call must name the step it belongs to"
    )
