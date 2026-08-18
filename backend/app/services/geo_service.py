"""Probe orchestration: the impure half of AI-answer visibility.

The `geo` engine is pure -- it builds prompts, reads answers, and does the
arithmetic. But *probing is a model call*, and an engine may never make one
(docs/ARCHITECTURE.md section 3). This module is where that call happens, and it
is the only file in the feature that is allowed to be impure.

It reaches exactly two things, both through a seam:

* **models**, through `backend.app.llm.ModelRouter` -- never a vendor SDK, and
  never a model id written in this file (docs/ARCHITECTURE.md section 8: the
  routing tables are the only place a model name lives);
* **persistence**, through the `ProbeStore` Protocol -- never a database session.
  A real SQLAlchemy adapter and an in-memory test fake are interchangeable, which
  is what keeps `tests/services/test_geo_service.py` hermetic.

Four decisions in here are load-bearing, and each exists because the obvious
alternative produces a plausible-looking number that is wrong:

1. **One model per probe, with fallback deliberately disabled.** The router's job
   is normally to retry a failed call on the next model in the chain. That is
   exactly wrong here: if we asked model A and model C answered, the per-model
   breakdown becomes fiction. So each model gets a single-entry chain, and a
   failure on A is recorded as `no_answer` *for A*.
2. **A failed probe is contained, never fatal.** A 503 on one question must not
   shrink the sample to zero, and must never be recorded as the brand being
   absent.
3. **The budget guard runs before every call, and exhaustion truncates the run
   rather than raising.** 40 prompts x 3 models is 120 calls; a run that can
   overspend is a run that will.
4. **The previous score is read before the new outcomes are saved.** Otherwise
   "the latest run" is the run we just wrote, and the trend compares a run
   against itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from backend.app.engines.geo import (
    BrandIdentity,
    GeoPrompt,
    ProbeOutcome,
    ShareOfVoice,
    SovDelta,
    answer_excerpt,
    classify_answer,
    detect_presence,
    diff_share_of_voice,
    share_of_voice,
)
from backend.app.llm import (
    BudgetExceededError,
    BudgetState,
    LlmError,
    Message,
    ModelRouter,
    ModelTier,
    Role,
    RouteEntry,
    TaskClass,
)

__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_MODEL_LIMIT",
    "DEFAULT_PROBE_MAX_TOKENS",
    "PROBE_TASK",
    "ProbeStore",
    "VisibilityReport",
    "probe_models",
    "probe_visibility",
]

#: Reading an answer for a brand name is classification, so it routes to the
#: cheap tier (docs/ARCHITECTURE.md section 8). Probing is the highest-volume
#: model call in the product -- 120 calls per business per run -- and paying
#: strong-tier prices to ask "who is the best plumber in Koblenz" would make the
#: wedge metric the most expensive thing we do.
PROBE_TASK: Final[TaskClass] = TaskClass.CLASSIFY

#: At most three models. Beyond that the marginal information is small and the
#: cost is linear (docs/ROADMAP.md section 2: "2-3 models").
DEFAULT_MODEL_LIMIT: Final = 3

#: Concurrent in-flight probes. Bounded because provider rate limits are shared
#: across every business on the platform: firing 120 requests at once earns a wall
#: of 429s, which this module would faithfully record as `no_answer` and thereby
#: destroy its own sample.
DEFAULT_CONCURRENCY: Final = 4

#: Output ceiling per probe. A recommendation answer is short; the cap is a cost
#: control. It is a genuine trade-off: a brand named only in a long tail after the
#: cut-off is missed, so this understates rather than overstates visibility.
DEFAULT_PROBE_MAX_TOKENS: Final = 600

#: The `error` recorded when a model answered but refused. Distinct from a
#: provider failure, because "the model would not say" and "the model was down"
#: call for different responses from us.
REFUSAL_ERROR: Final = "refusal_or_empty"


class ProbeStore(Protocol):
    """Persistence port for probe results.

    Deliberately two methods. Anything wider would tempt this service into
    querying, and the moment it queries it needs a session, and the moment it
    needs a session these tests need a database.

    `latest_share_of_voice` returns the score of the *previous* run, or `None` for
    a business that has never been probed -- which is a first run, not an error.
    """

    async def save_outcomes(self, business_id: UUID, outcomes: Sequence[ProbeOutcome]) -> int:
        """Persist one run's outcomes. Returns the number of rows written."""
        ...

    async def latest_share_of_voice(self, business_id: UUID) -> ShareOfVoice | None:
        """The most recent completed run's score, or `None` if there is none."""
        ...


class VisibilityReport(BaseModel):
    """One probe run, with everything needed to judge how much to trust it.

    `caveats` is not decoration. This metric is a sample of non-deterministic
    model output, and the failure mode of the whole feature is a confident
    percentage on a dashboard with nothing next to it. The report therefore
    carries its own limitations, and the UI is expected to show them.
    """

    business_id: UUID
    set_version: str
    models: list[str] = Field(default_factory=list)
    prompts_planned: int
    probes_planned: int
    probes_run: int
    outcomes: list[ProbeOutcome] = Field(default_factory=list)
    share_of_voice: ShareOfVoice
    delta: SovDelta
    saved_outcomes: int
    budget_exhausted: bool
    spent_usd: Decimal
    #: True when no provider credential was configured and the router served
    #: `FakeProvider`. The numbers are then arithmetic over canned text.
    using_fake_provider: bool
    caveats: list[str] = Field(default_factory=list)


def probe_models(
    router: ModelRouter,
    *,
    task: TaskClass = PROBE_TASK,
    limit: int = DEFAULT_MODEL_LIMIT,
) -> tuple[RouteEntry, ...]:
    """The models to probe, taken from the router's own chain for `task`.

    The cheap tier's fallback chain is already "two or three different vendors,
    cheapest-adequate first", which is exactly the fan-out this metric wants -- so
    the model list is config in `llm/router.py` rather than a literal here. With
    no credentials configured the chain is the fake one, and the caller reports
    that rather than pretending to have measured something.
    """
    return router.resolve(task).chain[:limit]


def _single_model_router(router: ModelRouter, entry: RouteEntry, task: TaskClass) -> ModelRouter:
    """A router pinned to exactly one model, sharing the parent's providers.

    Pinning is the point. Falling back to another model would silently attribute
    one vendor's answer to another in the per-model breakdown -- and that
    breakdown is the thing that stops a pooled "22%" from hiding "60% on one
    model, 0% on two".
    """
    tier = router.resolve(task).tier
    chains: Mapping[ModelTier, tuple[RouteEntry, ...]] = {tier: (entry,)}
    return ModelRouter(providers=router.providers, chains=chains)


async def probe_visibility(
    *,
    business_id: UUID,
    brand: BrandIdentity,
    prompts: Sequence[GeoPrompt],
    store: ProbeStore,
    router: ModelRouter | None = None,
    competitors: Sequence[BrandIdentity] = (),
    budget: BudgetState | None = None,
    models: Sequence[RouteEntry] | None = None,
    max_models: int = DEFAULT_MODEL_LIMIT,
    max_prompts: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_tokens: int = DEFAULT_PROBE_MAX_TOKENS,
    task: TaskClass = PROBE_TASK,
) -> VisibilityReport:
    """Run the prompt set against N models, score it, diff it, and persist it.

    Raises `ValueError` for an empty prompt set -- scoring nothing would produce a
    `ShareOfVoice` of all zeros that looks exactly like a business nobody
    mentions.

    Never raises for a provider failure, a refusal, or an exhausted budget: those
    are recorded, and the sample they produce is reported with its own size.
    """
    if not prompts:
        raise ValueError(
            "probe_visibility needs at least one prompt: an empty prompt set would "
            "score as zero visibility, which is a claim we never measured"
        )
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    active_router = router if router is not None else ModelRouter()
    route = active_router.resolve(task)
    entries = (
        tuple(models)
        if models is not None
        else probe_models(active_router, task=task, limit=max_models)
    )
    selected = list(prompts if max_prompts is None else prompts[:max_prompts])

    # Read the baseline BEFORE writing this run, or "latest" becomes this run.
    previous = await store.latest_share_of_voice(business_id)

    outcomes, budget_exhausted = await _run_probes(
        prompts=selected,
        entries=entries,
        brand=brand,
        competitors=competitors,
        router=active_router,
        task=task,
        budget=budget,
        concurrency=concurrency,
        max_tokens=max_tokens,
    )

    sov = share_of_voice(outcomes)
    delta = diff_share_of_voice(previous, sov)
    saved = await store.save_outcomes(business_id, outcomes)

    return VisibilityReport(
        business_id=business_id,
        set_version=sov.set_version or (selected[0].set_version if selected else ""),
        models=[entry.model for entry in entries],
        prompts_planned=len(selected),
        probes_planned=len(selected) * len(entries),
        probes_run=len(outcomes),
        outcomes=outcomes,
        share_of_voice=sov,
        delta=delta,
        saved_outcomes=saved,
        budget_exhausted=budget_exhausted,
        spent_usd=budget.spent_usd if budget is not None else Decimal(0),
        using_fake_provider=route.using_fake,
        caveats=build_caveats(
            sov=sov,
            delta=delta,
            using_fake_provider=route.using_fake,
            budget_exhausted=budget_exhausted,
            probes_planned=len(selected) * len(entries),
            probes_run=len(outcomes),
        ),
    )


async def _run_probes(
    *,
    prompts: Sequence[GeoPrompt],
    entries: Sequence[RouteEntry],
    brand: BrandIdentity,
    competitors: Sequence[BrandIdentity],
    router: ModelRouter,
    task: TaskClass,
    budget: BudgetState | None,
    concurrency: int,
    max_tokens: int,
) -> tuple[list[ProbeOutcome], bool]:
    """Fan out every (prompt, model) pair under a bounded semaphore.

    Returns the outcomes in `prompt x model` order plus whether the budget ran
    out. Order comes from `asyncio.gather`, which resolves in argument order
    regardless of completion order -- so a run is reproducible even though the
    calls are concurrent.

    The budget flag is checked inside each task rather than only before the fan
    out: once one call has been refused, the remaining ones are pointless and
    skipping them is what makes the ceiling a ceiling. It is still best-effort at
    the margin -- `concurrency` calls can pass the guard before the first one
    reports back -- so the true worst case is the limit plus one batch, which is
    why the batch is small.
    """
    semaphore = asyncio.Semaphore(concurrency)
    exhausted = asyncio.Event()
    pinned = {entry: _single_model_router(router, entry, task) for entry in entries}

    async def probe(prompt: GeoPrompt, entry: RouteEntry) -> ProbeOutcome | None:
        async with semaphore:
            if exhausted.is_set():
                return None
            try:
                completion = await pinned[entry].complete(
                    task,
                    [Message(role=Role.USER, content=prompt.text)],
                    budget=budget,
                    # Never sent: current Claude models reject `temperature`
                    # outright, and a probe wants the model's default behaviour
                    # anyway -- that is what a real user would get.
                    temperature=None,
                    max_tokens=max_tokens,
                )
            except BudgetExceededError:
                exhausted.set()
                return None
            except LlmError as exc:
                return _no_answer(prompt, entry, error=f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                # A bug in one adapter, or a provider returning something
                # unparseable, must cost one probe and not the whole
                # measurement. The type name is recorded so it stays diagnosable
                # rather than becoming an anonymous gap in the sample.
                return _no_answer(prompt, entry, error=f"{type(exc).__name__}: {exc}")

        return _read_answer(
            prompt,
            entry,
            text=completion.text or "",
            brand=brand,
            competitors=competitors,
            usd=completion.usage.usd,
            latency_ms=completion.usage.latency_ms,
            provider=completion.usage.provider,
            model=completion.usage.model,
        )

    results = await asyncio.gather(
        *(probe(prompt, entry) for prompt in prompts for entry in entries)
    )
    return [result for result in results if result is not None], exhausted.is_set()


def _read_answer(
    prompt: GeoPrompt,
    entry: RouteEntry,
    *,
    text: str,
    brand: BrandIdentity,
    competitors: Sequence[BrandIdentity],
    usd: Decimal,
    latency_ms: int,
    provider: str,
    model: str,
) -> ProbeOutcome:
    """Turn one model response into one row.

    Detection runs *before* refusal classification on purpose: a response that
    named a business answered the question, whatever it apologised about first.
    Classifying first would discard real data as a refusal and inflate every
    percentage that follows.

    `provider` and `model` come from the completion's own usage block rather than
    from the route we asked for, so the row records what actually answered.
    """
    presence = detect_presence(text, brand=brand, competitors=competitors)
    named_any = presence.present or bool(presence.competitors_mentioned)
    status = classify_answer(text, named_any_brand=named_any)

    if status == "no_answer":
        return _no_answer(
            prompt,
            entry,
            error=REFUSAL_ERROR,
            excerpt=answer_excerpt(text),
            usd=usd,
            latency_ms=latency_ms,
            provider=provider,
            model=model,
        )

    return ProbeOutcome(
        prompt_id=prompt.prompt_id,
        prompt_text=prompt.text,
        category=prompt.category,
        set_version=prompt.set_version,
        prompt_contains_brand=prompt.contains_brand,
        provider=provider,
        model=model,
        status="answered",
        mentioned=presence.mentioned,
        cited=presence.cited,
        competitors_mentioned=presence.competitors_mentioned,
        competitors_cited=presence.competitors_cited,
        answer_excerpt=answer_excerpt(text),
        usd=usd,
        latency_ms=latency_ms,
    )


def _no_answer(
    prompt: GeoPrompt,
    entry: RouteEntry,
    *,
    error: str,
    excerpt: str = "",
    usd: Decimal = Decimal(0),
    latency_ms: int = 0,
    provider: str | None = None,
    model: str | None = None,
) -> ProbeOutcome:
    """A probe that produced no measurement, with the reason attached.

    Carries no presence signal at all -- the contract's validator enforces that,
    and the reason is the rule this whole module exists to protect: an outage is
    the absence of a measurement, not the absence of a brand.
    """
    return ProbeOutcome(
        prompt_id=prompt.prompt_id,
        prompt_text=prompt.text,
        category=prompt.category,
        set_version=prompt.set_version,
        prompt_contains_brand=prompt.contains_brand,
        provider=provider if provider is not None else entry.provider,
        model=model if model is not None else entry.model,
        status="no_answer",
        answer_excerpt=excerpt,
        error=error,
        usd=usd,
        latency_ms=latency_ms,
    )


#: Below this many usable answers the percentage moves several points per answer
#: and should not be read as a trend. 20 is not a statistical threshold, it is the
#: point where one answer is worth less than five percentage points.
MIN_MEANINGFUL_SAMPLE: Final = 20

#: A no_answer rate above this means the run mostly failed, whatever the
#: percentage says about the answers that survived.
HIGH_NO_ANSWER_RATIO: Final = 0.3


def build_caveats(
    *,
    sov: ShareOfVoice,
    delta: SovDelta,
    using_fake_provider: bool,
    budget_exhausted: bool,
    probes_planned: int,
    probes_run: int,
) -> list[str]:
    """The honest footnotes for one run, worst first.

    This exists because the predictable way this feature fails is not a bug: it is
    a true number rendered without its context. Generating the footnotes here --
    rather than hoping a UI remembers them -- means every consumer (dashboard,
    agent summary, PDF report) gets the same ones.
    """
    caveats: list[str] = []

    if using_fake_provider:
        caveats.append(
            "No model provider is configured, so these answers came from the built-in "
            "fake provider. This is not a measurement of AI visibility."
        )

    if budget_exhausted:
        caveats.append(
            f"The run stopped early on the cost budget: {probes_run} of {probes_planned} "
            "planned probes ran, so the sample is smaller than intended."
        )
    elif probes_run < probes_planned:
        caveats.append(
            f"Only {probes_run} of {probes_planned} planned probes ran, so the sample is "
            "smaller than intended."
        )

    if sov.usable_answers == 0:
        caveats.append(
            "No usable answers: every probe refused, failed, or returned nothing. Share "
            "of voice is unknown for this run -- it is not zero."
        )
    else:
        caveats.append(
            f"Sample, not a census: {sov.mentions} of {sov.usable_answers} usable answers "
            f"across {sov.models_probed} model(s), from {sov.prompts_probed} fixed questions. "
            "Model answers are non-deterministic, so repeat runs will vary."
        )
        if sov.usable_answers < MIN_MEANINGFUL_SAMPLE:
            caveats.append(
                f"Small sample ({sov.usable_answers} usable answers): one answer moves the "
                "percentage by several points, so treat this as indicative rather than a trend."
            )

    if sov.no_answer_count and sov.probes_total:
        ratio = sov.no_answer_count / sov.probes_total
        if ratio > HIGH_NO_ANSWER_RATIO:
            caveats.append(
                f"{sov.no_answer_count} of {sov.probes_total} probes returned no answer "
                f"({ratio:.0%}). Those are excluded from the percentage rather than counted "
                "as absence, but a run this incomplete is weak evidence either way."
            )

    if sov.models_probed == 1:
        caveats.append(
            "Only one model was probed, so this is one vendor's behaviour rather than the "
            "AI answer landscape."
        )

    if sov.unprompted_usable_answers == 0 and sov.usable_answers:
        caveats.append(
            "Every usable answer came from a question that already named the brand, so this "
            "measures recognition, not discoverability."
        )
    elif sov.unprompted_usable_answers and sov.unprompted_usable_answers < sov.usable_answers:
        unprompted = sov.unprompted_mention_share_pct
        caveats.append(
            "Questions naming the brand almost always yield a mention. The comparable "
            f"figure is the unprompted rate: {unprompted}% over "
            f"{sov.unprompted_usable_answers} answers to questions that did not name it."
        )

    caveats.append(delta.note)
    return caveats
