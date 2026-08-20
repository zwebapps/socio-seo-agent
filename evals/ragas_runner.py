"""The Ragas half of the judged eval — and the ONLY file that runs outside our venv.

Executed by `evals/ragas_arm.py` as `.venv-ragas/bin/python evals/ragas_runner.py`,
reading one JSON object on stdin and writing one JSON object on stdout. It must not
import anything from `backend/` or `evals/` — it runs in an environment pinned to
`openai==2.54.0` for Ragas's sake, while this project runs on `openai>=3.2.0`, and
those two cannot coexist in one interpreter (`ARCHITECTURE.md` §14).

Three rules, all of them about not lying:

* **stdout carries JSON and nothing else.** Ragas and LangChain both write progress
  and deprecation notices, so everything chatty is forced to stderr; a stray print
  here becomes a parse error in the parent, which is reported as "not measured" rather
  than crashing the harness, but it would still lose a real measurement.
* **A metric that could not be computed comes back `null`, never `0.0`.** Zero says
  the text is entirely unfaithful; null says we do not know. The parent renders them
  differently and neither is silent.
* **`answer_relevancy` needs an EMBEDDINGS model**, not just a judge, because Ragas
  measures it by embedding generated questions against the original. If no embeddings
  endpoint is configured it is reported as not measured, with that reason — the
  alternative is a plausible number derived from nothing.

The judge model id arrives in the payload. It is resolved by the PARENT from our own
routing table, so this file hardcodes no model and cannot silently pick one.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from typing import Any

#: Both metric names, matching `evals/judged.py`. Duplicated here rather than
#: imported, and this is the one duplication in the design: importing from `evals`
#: would drag this project's dependencies into an interpreter that deliberately has a
#: different `openai`. The parent asserts the two lists agree, so the copy cannot
#: drift silently.
FAITHFULNESS = "faithfulness"
ANSWER_RELEVANCY = "answer_relevancy"


class _UsageTally:
    """Sums token usage across every judge call LangChain reports.

    Exists so the parent can put this arm's spend into the same accounting as the
    rest of the harness. Out-of-process judging is the reason the Ragas arm cannot
    use our `ModelRouter`, and losing the cost with it would have been a second
    concession on top of the first.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def on_llm_end(self, response: Any, **_: Any) -> None:
        self.calls += 1
        output = getattr(response, "llm_output", None) or {}
        usage = output.get("token_usage") or output.get("usage") or {}
        self.tokens_in += int(usage.get("prompt_tokens", 0) or 0)
        self.tokens_out += int(usage.get("completion_tokens", 0) or 0)


def _build_handler() -> Any:
    """A LangChain callback handler that tallies usage, or None if unavailable."""
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except Exception:  # pragma: no cover - exercised only in the isolated venv
        return None

    tally = _UsageTally()

    class Handler(BaseCallbackHandler):
        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            tally.on_llm_end(response, **kwargs)

    handler = Handler()
    handler.tally = tally  # type: ignore[attr-defined]
    return handler


def _judge(payload: dict[str, Any], handler: Any) -> Any:
    """The Ragas LLM wrapper over an OpenAI-compatible endpoint."""
    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper

    callbacks = [handler] if handler is not None else []
    chat = ChatOpenAI(
        model=payload["model"],
        base_url=payload.get("base_url") or None,
        api_key=payload["api_key"],
        # 0 for a judge: this is a measurement, and a measurement that moves when
        # nothing changed is not one.
        temperature=0,
        timeout=payload.get("timeout_s", 60),
        max_retries=2,
        callbacks=callbacks,
    )
    return LangchainLLMWrapper(chat)


def _embeddings(payload: dict[str, Any]) -> Any | None:
    """The embeddings wrapper, or None when no embeddings endpoint is configured."""
    model = payload.get("embeddings_model")
    if not model:
        return None
    from langchain_openai import OpenAIEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper

    return LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(
            model=model,
            base_url=payload.get("embeddings_base_url") or payload.get("base_url") or None,
            api_key=payload["api_key"],
        )
    )


def measure(payload: dict[str, Any]) -> dict[str, Any]:
    """Score every sample, returning one entry per sample key.

    A per-sample failure is contained: the entry says which metric could not be
    measured and why, and the other samples still return numbers. A failure that
    takes the whole dataset with it would turn one unparseable judge reply into an
    empty column.
    """
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.metrics import Faithfulness, ResponseRelevancy

    handler = _build_handler()
    judge = _judge(payload, handler)
    embeddings = _embeddings(payload)

    metrics: list[Any] = [Faithfulness(llm=judge)]
    relevancy_error: str | None = None
    if embeddings is None:
        relevancy_error = (
            "answer_relevancy needs an embeddings endpoint (Ragas embeds generated "
            "questions against the original); none is configured, so it was not measured"
        )
    else:
        metrics.append(ResponseRelevancy(llm=judge, embeddings=embeddings))

    samples = payload["samples"]
    keys = [sample["key"] for sample in samples]
    results: dict[str, dict[str, Any]] = {
        key: {FAITHFULNESS: None, ANSWER_RELEVANCY: None, "errors": {}} for key in keys
    }
    if relevancy_error:
        for key in keys:
            results[key]["errors"][ANSWER_RELEVANCY] = relevancy_error

    dataset = EvaluationDataset(
        samples=[
            SingleTurnSample(
                user_input=sample["question"],
                response=sample["answer"],
                retrieved_contexts=list(sample["contexts"]),
            )
            for sample in samples
        ]
    )

    try:
        outcome = evaluate(dataset=dataset, metrics=metrics, show_progress=False)
    except Exception as exc:
        for key in keys:
            results[key]["errors"]["evaluate"] = f"{type(exc).__name__}: {exc}"
        return _envelope(results, handler)

    scores = getattr(outcome, "scores", None) or []
    for index, key in enumerate(keys):
        row = scores[index] if index < len(scores) else {}
        if not isinstance(row, dict):
            results[key]["errors"]["evaluate"] = "ragas returned a row that is not a mapping"
            continue
        # The Ragas column names happen to equal our metric keys today; mapped
        # explicitly so a rename on either side is a visible edit rather than a column
        # that silently stops being filled.
        columns = ((FAITHFULNESS, "faithfulness"), (ANSWER_RELEVANCY, "answer_relevancy"))
        for metric, name in columns:
            value = row.get(name)
            # NaN is how Ragas reports "this one did not work", and `float('nan')`
            # would serialise as invalid JSON and then read as a score. Both are
            # turned into an explicit absence.
            if isinstance(value, (int, float)) and value == value:
                results[key][metric] = float(value)
            elif metric not in results[key]["errors"]:
                results[key]["errors"][metric] = "ragas returned no usable score"

    return _envelope(results, handler)


def _envelope(results: dict[str, dict[str, Any]], handler: Any) -> dict[str, Any]:
    tally = getattr(handler, "tally", None)
    return {
        "results": results,
        "calls": getattr(tally, "calls", 0),
        "tokens_in": getattr(tally, "tokens_in", 0),
        "tokens_out": getattr(tally, "tokens_out", 0),
    }


def main() -> int:
    # Ragas and LangChain both emit deprecation warnings on import, and a warning on
    # stdout would be a parse error in the parent.
    warnings.simplefilter("ignore")
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        json.dump({"error": f"could not read the payload: {exc}"}, sys.stdout)
        return 1

    try:
        answer = measure(payload)
    except Exception as exc:
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        return 1

    json.dump(answer, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
