"""The eval runner: runs the 20 cases twice -- RAG off, RAG on -- and writes a report.

    uv run python evals/run.py            # hermetic: FakeProvider, no network, free
    uv run python evals/run.py --live     # real providers. SPENDS MONEY.

**The default is the fake provider, deliberately and explicitly.** ``.env`` in this
repo holds a real ``OPENROUTER_API_KEY``, and ``ModelRouter()`` reads
``os.environ``, so a runner that "just used the ambient configuration" would bill a
real account every time someone ran the eval suite -- straight through the
money guardrail in ``CLAUDE.md``. So the router is built with ``env={}`` unless
``--live`` is passed, and the report says which one produced its numbers. That
sentence is the difference between evidence and decoration
(`docs/CRITERIA_MAP.md` §7).

**What this harness measures, and what it does not.** It measures the deterministic
rubric, the retrieval loop, and the plumbing that connects them. Under the fake
provider it does **not** measure model quality at all: the generated text is a
canned string chosen by hashing the prompt. The report states that in its first
paragraph, per case, and again in its limitations section, because a reader who
skims a table of numbers will otherwise assume the opposite.

**The RAG comparison is reported under three evidence conditions**, not two:

===========  ====================================  ================================
Condition    Evidence offered to the scorer        What it tells you
===========  ====================================  ================================
``rag_off``  nothing                               claims are unverifiable
``rag_on``   the chunks retrieval actually kept    what the shipped loop delivered
``oracle``   every fact in the case                the ceiling perfect retrieval
                                                   would allow
===========  ====================================  ================================

The oracle column exists because the honest failure of a two-column comparison is
that it cannot tell "retrieval found nothing" from "there was nothing to find".
With the fake provider the grader returns no structured grades, so retrieval
correctly refuses to ground anything and ``rag_on`` collapses onto ``rag_off`` --
the oracle column is what makes that visible as a retrieval result rather than a
missing feature.

**Ragas is not installed** (it is phase-gated in ``pyproject.toml``). The
faithfulness and answer-relevancy columns are therefore rendered as ``n/a
(ragas not installed)``. They are not estimated, approximated, or filled in from
the deterministic scores.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

# A script run directly is not on the package path, unlike a test (pytest adds the
# rootdir via `pythonpath`) or the app (uvicorn starts from it). Same pattern as
# `scripts/grant_platform_admin.py`, so `uv run python evals/run.py` works from
# anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Note what is deliberately absent at import time: `load_dotenv`. The scripts in
# `scripts/` load `.env` because they need a database URL; this one must not do it
# here, because `.env` holds a real provider key and an eval runner that picked it up
# by default would bill every run. `--live` loads it explicitly inside `main()` -- and
# then asserts it actually got a provider, so the flag cannot claim to spend money
# while quietly measuring FakeProvider.
from backend.app.engines.channel import HashtagEnforcement, enforce_hashtags, spec_for
from backend.app.llm.contract import BudgetState, Message, ModelTier, Role, TaskClass
from backend.app.llm.router import TASK_TIERS, TIER_CHAINS, ModelRouter, config_status
from backend.app.obs import Tracer, get_tracer, llm_span_fields, tracing_status
from backend.app.services.kb_service import (
    RetrievedChunk,
    StoredChunk,
    retrieve,
)
from evals.dataset import CASES, EvalCase
from evals.rubric import (
    ChannelLimits,
    Rendering,
    RubricResult,
    aggregate,
    score_brand,
    score_coverage,
    score_format,
    score_grounding,
    score_seo,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_REPORT_PATH: Final = Path("evals/report.md")

#: A prompt builder: a case and its retrieved passages in, one user message out.
PromptBuilder = Callable[[EvalCase, Mapping[str, str] | None], str]

#: Declared here rather than beside `PROMPT_BUILDERS`, because `RunConfig` needs it
#: as a field default and that class is defined first.
DEFAULT_PROMPT_VERSION: Final = "v2"

#: Per-case ceiling. The product's own cap is $0.50 per run
#: (docs/AGENT_RUNTIME.md section 8), and a case is one run's worth of work.
DEFAULT_BUDGET_USD: Final = Decimal("0.50")

#: Prompt versions, so a report can be compared against a later one. These belong
#: to the harness, not to the product: the real GENERATE prompt lands with the
#: renderers in Phase 6, and this runner must then call it instead of composing
#: its own.
#: v2 states the length floor and spells out the hashtag rule (see `_format_rules`).
#: The RAG variant is v3: v2 also repeated the rules after the passage block, which
#: fixed `format` and destroyed `grounding` -- see the note in `_user_prompt`.
PROMPT_VERSION_RAG_OFF: Final = "eval.generate.v2"
PROMPT_VERSION_RAG_ON: Final = "eval.generate.rag.v3"

#: How a generated output is expected to cite: ``[chunk:plumber-01#0]``.
CITATION_RE: Final = re.compile(r"\[chunk:([^\]\s]+)\]")

#: Embedding width for the harness's own deterministic embedder. Small on purpose:
#: it is a hash bag-of-words, not a semantic model, and pretending otherwise by
#: using 1536 dimensions would be theatre.
EMBED_DIMS: Final = 64

RAGAS_NOTE: Final = "n/a (ragas not installed)"

type Arm = Literal["rag_off", "rag_on"]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RunConfig:
    """How this run is configured. Reported verbatim in the report header."""

    #: False -> ``env={}`` -> FakeProvider. True -> the real environment, real money.
    live: bool = False
    out_path: Path = DEFAULT_REPORT_PATH
    budget_usd: Decimal = DEFAULT_BUDGET_USD
    #: Restrict to these case ids. Empty means all 20.
    only: tuple[str, ...] = ()
    #: Route GENERATE to this tier instead of its default (STRONG).
    #:
    #: Two honest uses. Comparing tiers is the point of an eval harness: "is the
    #: strong model worth 40x the cheap one on our rubric" is answerable only by
    #: running both. And a credential may simply not reach every tier -- an
    #: OpenRouter account's data-policy guardrails can admit the cheap chain and
    #: refuse the strong one -- in which case the choice is a labelled cheap-tier
    #: run or no live measurement at all.
    #:
    #: Whatever it is set to, the report header names the tier and the models that
    #: actually served, so a cheap-tier run can never be read as a strong-tier one.
    tier: ModelTier | None = None
    #: Which generation prompt to run. See `PROMPT_BUILDERS`; `v1` is kept
    #: executable so the improvement over it can be re-measured on demand rather
    #: than taken on trust.
    prompt_version: str = DEFAULT_PROMPT_VERSION

    @property
    def env(self) -> Mapping[str, str] | None:
        """The environment the router and tracer should read.

        ``{}`` -- not ``None`` -- when not live: an empty mapping is what forces the
        fake provider and the no-op tracer regardless of what is in ``.env``.
        """
        return None if self.live else {}


# --------------------------------------------------------------------------- #
# A hermetic corpus: deterministic embedder + in-memory store
# --------------------------------------------------------------------------- #


class HashEmbedder:
    """A deterministic bag-of-words embedder. Satisfies ``kb_service.Embedder``.

    Not a semantic model and not pretending to be one: each token is hashed into a
    bucket and the vector is L2-normalised, so cosine similarity reduces to term
    overlap. That is enough to exercise the retrieval loop hermetically, and its
    weakness is named in the report -- a synonym will not retrieve, so the harness
    cannot tell a retrieval miss caused by a poor query rewrite from one caused by
    this embedder.
    """

    def __init__(self, dims: int = EMBED_DIMS) -> None:
        self._dims = dims

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per input text, in order."""
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        buckets = [0.0] * self._dims
        for token in re.findall(r"\w+", text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            buckets[digest[0] % self._dims] += 1.0
        norm = math.sqrt(sum(value * value for value in buckets))
        if norm == 0.0:
            return buckets
        return [value / norm for value in buckets]


class InMemoryChunkStore:
    """``kb_service.ChunkStore`` over a dict. No database, no network.

    Business-scoped like the real adapter, so a bug that ignored ``business_id``
    would show up here too rather than being masked by a single-tenant fixture.
    """

    def __init__(self) -> None:
        self._rows: dict[UUID, list[tuple[UUID, StoredChunk]]] = {}

    async def upsert(
        self,
        business_id: UUID,
        document_id: UUID,
        chunks: Sequence[StoredChunk],
    ) -> int:
        bucket = self._rows.setdefault(business_id, [])
        bucket.extend((document_id, chunk) for chunk in chunks)
        return len(chunks)

    async def search(
        self,
        business_id: UUID,
        embedding: Sequence[float],
        *,
        limit: int,
    ) -> list[RetrievedChunk]:
        scored: list[tuple[float, UUID, StoredChunk]] = []
        for document_id, chunk in self._rows.get(business_id, []):
            similarity = sum(a * b for a, b in zip(embedding, chunk.embedding, strict=False))
            # Cosine DISTANCE, to match pgvector's `<=>`: lower is nearer.
            scored.append((1.0 - similarity, document_id, chunk))
        scored.sort(key=lambda row: row[0])

        return [
            RetrievedChunk(
                chunk_id=_chunk_uuid(chunk.content_hash),
                document_id=document_id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                distance=distance,
                meta=dict(chunk.meta),
            )
            for distance, document_id, chunk in scored[:limit]
        ]

    async def existing_hashes(self, business_id: UUID, hashes: Sequence[str]) -> set[str]:
        stored = {chunk.content_hash for _, chunk in self._rows.get(business_id, [])}
        return stored & set(hashes)


def _chunk_uuid(readable_id: str) -> UUID:
    """A stable UUID for a readable chunk id, so ids survive a restart."""
    return uuid5(NAMESPACE_URL, f"evals/chunk/{readable_id}")


@dataclass(frozen=True, slots=True)
class CaseCorpus:
    """The case's own documents, indexed, with a map back to readable ids.

    The readable id (``plumber-01#0``) is what the report prints and what a
    generated citation names; the UUID is what the store speaks. Keeping both, and
    the mapping between them, is what lets a citation be followed by a human.
    """

    business_id: UUID
    store: InMemoryChunkStore
    embedder: HashEmbedder
    readable_by_uuid: Mapping[UUID, str]
    chunks: Mapping[str, str]


async def build_corpus(case: EvalCase) -> CaseCorpus:
    """Index one case's facts into a fresh in-memory store."""
    business_id = uuid5(NAMESPACE_URL, f"evals/business/{case.business.name}")
    document_id = uuid5(NAMESPACE_URL, f"evals/document/{case.case_id}")
    embedder = HashEmbedder()
    store = InMemoryChunkStore()

    readable_ids = case.chunk_ids()
    vectors = await embedder.embed(list(case.facts))
    await store.upsert(
        business_id,
        document_id,
        [
            StoredChunk(
                ordinal=index,
                content=text,
                # The readable id doubles as the content hash, which is what the
                # store echoes back as the chunk id. Deliberate: it keeps the
                # report's citations human-followable end to end.
                content_hash=readable_id,
                embedding=vector,
                meta={"case_id": case.case_id, "readable_id": readable_id},
            )
            for index, (readable_id, text, vector) in enumerate(
                zip(readable_ids, case.facts, vectors, strict=True)
            )
        ],
    )

    return CaseCorpus(
        business_id=business_id,
        store=store,
        embedder=embedder,
        readable_by_uuid={_chunk_uuid(rid): rid for rid in readable_ids},
        chunks=case.chunk_map(),
    )


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RetrievalSummary:
    """What the agentic retrieval loop did, flattened for the report."""

    outcome: str
    outcome_reason: str
    attempts: int
    relevant: int
    partial: int
    kept_chunk_ids: tuple[str, ...]
    model_calls: int
    cost_usd: Decimal
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GroundingTriple:
    """The same text scored against three evidence conditions."""

    off: RubricResult
    on: RubricResult
    oracle: RubricResult


@dataclass(frozen=True, slots=True)
class ArmResult:
    """One arm of one case."""

    arm: Arm
    text: str
    results: tuple[RubricResult, ...]
    grounding: GroundingTriple
    cited_chunk_ids: tuple[str, ...]
    retrieval: RetrievalSummary | None
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    error: str | None = None
    #: How many hashtags the deterministic channel engine had to remove, and how
    #: many the model left missing. Reported, never swallowed: a `format` column
    #: reading 1.00 because a renderer cleaned up after the model is the renderer's
    #: competence, not the model's, and a report that shows only the former is
    #: crediting the wrong component.
    hashtags_removed: int = 0
    hashtag_shortfall: int = 0

    @property
    def mean_score(self) -> float:
        """Mean over the scored dimensions. 0.0 when the arm failed."""
        return aggregate(self.results).mean_score


@dataclass(frozen=True, slots=True)
class CaseRow:
    """One case, both arms, plus the reference-answer control."""

    case_id: str
    vertical: str
    channel: str
    rag_off: ArmResult
    rag_on: ArmResult
    #: The human-written reference answer under the same three evidence conditions.
    #: The control: it shows what a *correct* output's grounding looks like, which
    #: is what separates "the model wrote nothing verifiable" from "the scorer
    #: cannot verify anything".
    reference_grounding: GroundingTriple
    #: Proof the rubric discriminates: the reference passes brand, the mutation
    #: with a banned claim appended does not.
    reference_brand_passed: bool
    violating_brand_passed: bool


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You write marketing copy for small German businesses. Write in German. Use "
    "only facts you were given; never invent a price, a duration or a guarantee. "
    "Obey the channel's length and hashtag limits."
)


def _grounded_block(passages: Mapping[str, str]) -> str:
    """Render the retrieved passages with the ids the model must cite."""
    return "\n".join(f"[chunk:{chunk_id}] {text}" for chunk_id, text in passages.items())


def _format_rules(channel: str, limits: ChannelLimits) -> str:
    """The mechanically-checked rules, stated so a model cannot misread them.

    Every line here corresponds to something `rubric.score_format` measures. Two
    defects in the first version of this prompt, both found by the 2026-08-19 live
    run and both visible in that report's `format` column:

    * **`min_chars` was never mentioned at all.** The rubric floors `blog_article`
      at 2,500 characters and `email` at 300, and the prompt only ever named a
      *maximum* -- so the model was failing a requirement it was never given. It is
      not a reluctance problem: asked directly for "mindestens 2800 Zeichen",
      `gpt-4.1-mini` returned 5,032. It simply was not asked.
    * **`0-0 hashtags`** is what an empty range rendered as, which reads like a
      formatting artefact rather than a prohibition. It is now spelled out.
    """
    rules: list[str] = []
    if limits.min_chars:
        rules.append(
            f"AT LEAST {limits.min_chars} characters. This is a floor, not a target: "
            "a shorter piece is rejected outright, so keep writing until you pass it."
        )
    rules.append(f"At most {limits.max_chars} characters.")
    if limits.hashtags_max == 0:
        rules.append("NO hashtags. Not one, not at the end, not inside a sentence.")
    elif limits.hashtags_min == limits.hashtags_max:
        rules.append(f"Exactly {limits.hashtags_max} hashtags.")
    else:
        rules.append(f"Between {limits.hashtags_min} and {limits.hashtags_max} hashtags.")
    rules.append(
        "A link in the body is fine."
        if limits.link_in_body
        else "NO URL in the body -- it would not be clickable on this channel."
    )
    # Said explicitly whenever hashtags are restricted, because the two instructions
    # otherwise contradict each other: a chunk id is `<case_id>#<ordinal>`, so an
    # obedient model reading "no hashtags" has a reason to strip the `#0` off a
    # citation and hand back an id that never existed -- which the grounding scorer
    # then reports, correctly, as a fabricated source.
    if limits.hashtags_max < 5:
        rules.append(
            "A citation marker such as [chunk:example#0] is NOT a hashtag. Reproduce "
            "any id you cite exactly as it was given to you, including the # and the "
            "number after it."
        )
    return f"Format rules for {channel}:\n" + "\n".join(f"- {rule}" for rule in rules)


def _user_prompt_v1(case: EvalCase, passages: Mapping[str, str] | None) -> str:
    """The ORIGINAL prompt, kept so the improvement can be re-measured, not asserted.

    Preserved verbatim rather than described. Its three defects are the subject of
    the comparison and every one of them was found by running it:

    * the channel line names only a MAXIMUM length, so the rubric's 2,500-character
      floor on `blog_article` was a requirement the model was never given;
    * an empty hashtag range renders as ``0-0 hashtags``, which reads like a
      formatting artefact rather than a prohibition;
    * nothing tells the model that ``[chunk:plumber-01#0]`` is not a hashtag, so
      "no hashtags" gives it a reason to strip the ``#0`` off a citation.

    Keeping it executable is the point: `--prompt-version v1` reproduces the old
    numbers on demand, so "v2 is better" stays a measurement instead of a claim in a
    commit message. And it earned that immediately, by SHRINKING the credit the prompt
    change deserves. Three live runs, `--tier cheap` on `gpt-4.1-mini`, `rag_on` mean:

        v1, before the rubric was fixed .................. 0.67   (format 0.35)
        v1, after  the rubric was fixed .................. 0.81   (format 0.94)
        v2, after  the rubric was fixed .................. 0.84   (format 1.00)

    So of the 0.17 total, **0.14 was the measurement bug** (citation markers counted
    as hashtags, which penalised the RAG arm for citing) and **0.03 was this prompt**.
    Without a runnable v1 the whole 0.17 would have been attributed to the prompt
    rewrite, which would have been flattering and wrong.

    The prompt's 0.03 is real and is exactly where it was aimed: `format` 0.94 -> 1.00
    (the length floor, which v1 never states), `brand` 0.95 -> 1.00, and `grounding`
    0.38 -> 0.41. Note also that hashtag enforcement made ZERO corrections under BOTH
    prompts -- the model was never actually breaching a hashtag cap, so that engine is
    currently insurance rather than load-bearing.
    """
    limits = spec_for(case.channel)
    parts = [
        f"Business: {case.business.name}, {case.business.vertical} in "
        f"{case.business.city}. Services: {', '.join(case.business.services)}.",
        f"Channel: {case.channel} (max {limits.max_chars} characters, "
        f"{limits.hashtags_min}-{limits.hashtags_max} hashtags, "
        f"{'links allowed' if limits.link_in_body else 'NO clickable link in the body'}).",
        f"Target keyword: {case.target_keyword}.",
        f"Brief: {case.brief}",
        f"Must mention: {', '.join(case.must_contain)}.",
        f"Never claim: {', '.join(case.banned_claims)}.",
    ]
    if passages is None:
        parts.append("You have no source documents. Do not state any figure you cannot support.")
    else:
        parts.append(
            "Source passages. Cite the id in square brackets after any figure you "
            "take from one, exactly as shown:\n" + _grounded_block(passages)
        )
    return "\n\n".join(parts)


def _user_prompt_v2(case: EvalCase, passages: Mapping[str, str] | None) -> str:
    limits = spec_for(case.channel)
    rules = _format_rules(case.channel, limits)
    parts = [
        f"Business: {case.business.name}, {case.business.vertical} in "
        f"{case.business.city}. Services: {', '.join(case.business.services)}.",
        f"Channel: {case.channel}.",
        rules,
        f"Target keyword: {case.target_keyword}.",
        f"Brief: {case.brief}",
        f"Must mention: {', '.join(case.must_contain)}.",
        f"Never claim: {', '.join(case.banned_claims)}.",
    ]
    if passages is None:
        parts.append("You have no source documents. Do not state any figure you cannot support.")
    else:
        parts.append(
            "Source passages. Cite the id in square brackets after any figure you "
            "take from one, exactly as shown:\n" + _grounded_block(passages)
        )
        # The citation instruction is the LAST thing in this prompt, and it has to
        # stay there. Measured, both directions, on `gpt-4.1-mini`:
        #
        #   v1  format rules mid-prompt, passages + citation last
        #       -> format 0.35, grounding 0.35
        #   v2a format rules mid-prompt, passages, then a format REMINDER last
        #       -> format 1.00, grounding 0.02  (19 of 20 cases cited nothing)
        #
        # Appending the reminder fixed one dimension by breaking the other: whatever
        # ends this message is what the model obeys, so the two instructions were
        # simply taking turns. The `oracle` column stayed healthy throughout, which
        # is what proves it was a citing failure and not a retrieval one.
        #
        # So the reminder is gone, and nothing is lost by removing it, because the
        # two things it was there to protect are now held elsewhere: hashtag counts
        # by `engines/channel` (arithmetic, enforced in code, which is why the model
        # ignoring them is survivable) and the length floor by the `_format_rules`
        # block above, which measured 0 pieces short of the minimum in both arms.
        # A prompt cannot end with two different instructions; the one that must win
        # is the one no code can enforce afterwards.
    return "\n\n".join(parts)


#: The selectable prompt variants. A dict rather than an if/else so adding v3 is a
#: line here and nothing else, and so `--prompt-version` can list them itself.
PROMPT_BUILDERS: Final[Mapping[str, PromptBuilder]] = {
    "v1": _user_prompt_v1,
    "v2": _user_prompt_v2,
}


#: What each variant reports as its trace label. The RAG arm of v2 is tagged v3
#: because it went through an intermediate form that fixed `format` by destroying
#: `grounding` -- see the note in `_user_prompt_v2`.
_PROMPT_LABELS: Final[Mapping[str, tuple[str, str]]] = {
    "v1": ("eval.generate.v1", "eval.generate.rag.v1"),
    "v2": (PROMPT_VERSION_RAG_OFF, PROMPT_VERSION_RAG_ON),
}


def _enforce_channel_format(case: EvalCase, text: str) -> HashtagEnforcement:
    """Apply the deterministic channel rules the product would apply before publishing.

    Scoring the raw completion would measure something that never ships: a real
    pipeline renders for a channel, and enforcing a hashtag count is that renderer's
    job. It lives in `engines/channel` rather than here precisely so this is not the
    eval marking its own homework -- the eval consumes a product engine, and
    `backend/tests/engines/test_channel.py` scores its output with this same rubric.

    Length is deliberately NOT enforced here. Truncating an article to fit a
    ceiling, or padding one to reach a floor, would be editing the copy rather than
    formatting it -- so length stays a genuine measurement of the model, which is
    why the `format` column can still fail on it.
    """
    limits = spec_for(case.channel)
    return enforce_hashtags(
        text,
        minimum=limits.hashtags_min,
        maximum=limits.hashtags_max,
        # Chunk ids are `<case_id>#<ordinal>`, so a citation contains a `#`. Without
        # this the enforcement rewrote `[chunk:plumber-01#0]` to
        # `[chunk:plumber-01]` and the grounding scorer -- correctly -- reported a
        # fabricated source.
        protect=(CITATION_RE,),
    )


async def _generate(
    case: EvalCase,
    *,
    router: ModelRouter,
    tracer: Tracer,
    budget: BudgetState,
    passages: Mapping[str, str] | None,
    prompt_version: str,
    build_prompt: PromptBuilder,
) -> tuple[str, str, int, int, Decimal]:
    """One GENERATE call, traced. Returns ``(text, model, in, out, usd)``.

    This is where the observability seam is exercised for real: the span carries
    every field in ``obs.REQUIRED_SPAN_FIELDS``, and with no Langfuse keys it costs
    nothing because the tracer is the no-op.
    """
    messages = [
        Message(role=Role.SYSTEM, content=_SYSTEM),
        Message(role=Role.USER, content=build_prompt(case, passages)),
    ]

    with tracer.span("evals.generate", run_id=case.case_id, node="GENERATE") as span:
        completion = await router.complete(TaskClass.GENERATE, messages, budget=budget)
        usage = completion.usage
        span.update(
            **llm_span_fields(
                run_id=case.case_id,
                business_id=case.business.name,
                node="GENERATE",
                prompt_version=prompt_version,
                usage=usage,
                outcome="ok",
            )
        )

    return (
        completion.text or "",
        usage.model,
        usage.tokens_in,
        usage.tokens_out,
        usage.usd,
    )


# --------------------------------------------------------------------------- #
# Scoring one arm
# --------------------------------------------------------------------------- #


def _as_article_html(case: EvalCase, body: str) -> str:
    """Wrap generated body copy in a minimal HTML skeleton.

    **Read the limitation before reading the SEO column.** The article renderer
    does not exist yet (it lands in Phase 6), so there is no shipped component that
    turns a brief into a complete page. This skeleton supplies the title, meta
    description, ``h1``, internal links and JSON-LD from the *case*, which means the
    SEO score reflects the skeleton for those rules and the model's text only for
    keyword density, readability and heading structure inside the body.

    It is here so the delegation to the seo engine is exercised end to end. When
    the renderer lands, this function must be deleted and replaced by a call to it.
    """
    paragraphs = "\n".join(f"<p>{part.strip()}</p>" for part in body.split("\n") if part.strip())
    keyword = case.target_keyword
    return (
        f'<html lang="{case.business.locale}"><head>'
        f"<title>{keyword.title()} | {case.business.name}</title>"
        f'<meta name="description" content="{keyword.title()} in '
        f'{case.business.city}: {case.brief[:110]}">'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Article","headline":'
        f'"{keyword.title()}","author":{{"@type":"Organization","name":'
        f'"{case.business.name}"}}}}'
        "</script></head><body>"
        f"<h1>{keyword.title()}</h1>{paragraphs}"
        f'<h2>Leistungen</h2><p><a href="/leistungen">Leistungen</a>, '
        f'<a href="/preise">Preise</a>, <a href="/kontakt">Kontakt</a>.</p>'
        f'<p><a href="https://www.dihk.de">Branchenverband</a></p>'
        f'<img src="/foto.jpg" alt="{keyword} bei {case.business.name}">'
        "</body></html>"
    )


def _cited_ids(text: str) -> tuple[str, ...]:
    """The chunk ids a generated output claims to have used."""
    return tuple(dict.fromkeys(CITATION_RE.findall(text)))


def _grounding_triple(
    text: str,
    *,
    cited: Sequence[str],
    retrieved: Mapping[str, str],
    oracle: Mapping[str, str],
) -> GroundingTriple:
    """Score the same text under no evidence, retrieved evidence, and all facts.

    ``oracle`` deliberately treats every citation as available: it answers "if
    retrieval had been perfect, how much of this text would be verifiable?", which
    is the ceiling the ``rag_on`` column is measured against.
    """
    return GroundingTriple(
        off=score_grounding(text, (), {}),
        on=score_grounding(text, cited, retrieved),
        oracle=score_grounding(text, tuple(oracle), oracle),
    )


def _score_arm(
    case: EvalCase,
    *,
    arm: Arm,
    text: str,
    retrieved: Mapping[str, str],
    grounding: GroundingTriple,
) -> tuple[RubricResult, ...]:
    """Every rubric dimension that applies to this case's channel."""
    results: list[RubricResult] = [
        score_brand(text, case.banned_claims),
        score_format(Rendering(text=text), case.channel),
        score_coverage(text, case.must_contain),
        # The grounding dimension reported in the main table is the honest one:
        # what retrieval actually delivered for this arm.
        grounding.on if arm == "rag_on" else grounding.off,
    ]
    # SEO applies to the article channel only. A LinkedIn post has no title tag,
    # and scoring one would produce a number about nothing.
    if case.channel == "blog_article":
        results.insert(
            0, score_seo(_as_article_html(case, text), case.target_keyword, case.business.locale)
        )
    return tuple(results)


# --------------------------------------------------------------------------- #
# Running a case
# --------------------------------------------------------------------------- #


async def run_case(
    case: EvalCase, *, router: ModelRouter, tracer: Tracer, config: RunConfig
) -> CaseRow:
    """Run both arms of one case and score them."""
    corpus = await build_corpus(case)
    oracle = dict(corpus.chunks)

    # ---- arm 1: RAG off. No documents offered at all. ---- #
    off_budget = BudgetState(limit_usd=config.budget_usd)
    off_text, off_model, off_in, off_out, off_usd = await _generate(
        case,
        router=router,
        tracer=tracer,
        budget=off_budget,
        passages=None,
        prompt_version=_PROMPT_LABELS[config.prompt_version][0],
        build_prompt=PROMPT_BUILDERS[config.prompt_version],
    )
    off_enforced = _enforce_channel_format(case, off_text)
    off_text = off_enforced.text
    off_grounding = _grounding_triple(
        off_text, cited=_cited_ids(off_text), retrieved={}, oracle=oracle
    )
    rag_off = ArmResult(
        arm="rag_off",
        text=off_text,
        results=_score_arm(
            case, arm="rag_off", text=off_text, retrieved={}, grounding=off_grounding
        ),
        grounding=off_grounding,
        cited_chunk_ids=_cited_ids(off_text),
        retrieval=None,
        model=off_model,
        tokens_in=off_in,
        tokens_out=off_out,
        cost_usd=off_usd,
        hashtags_removed=off_enforced.removed,
        hashtag_shortfall=off_enforced.shortfall,
    )

    # ---- arm 2: RAG on. The real agentic retrieval loop. ---- #
    on_budget = BudgetState(limit_usd=config.budget_usd)
    retrieval: RetrievalSummary | None = None
    retrieved: dict[str, str] = {}
    error: str | None = None

    try:
        trace = await retrieve(
            f"{case.brief} Keyword: {case.target_keyword}.",
            business_id=corpus.business_id,
            router=router,
            embedder=corpus.embedder,
            store=corpus.store,
            budget=on_budget,
        )
    except Exception as exc:  # the harness must report a failure, never hide it
        error = f"{type(exc).__name__}: {exc}"
    else:
        kept = tuple(
            corpus.readable_by_uuid.get(chunk.chunk_id, str(chunk.chunk_id))
            for chunk in trace.chunks
        )
        retrieved = {
            chunk_id: corpus.chunks[chunk_id] for chunk_id in kept if chunk_id in corpus.chunks
        }
        retrieval = RetrievalSummary(
            outcome=trace.outcome,
            outcome_reason=trace.outcome_reason,
            attempts=trace.attempt_count,
            relevant=sum(attempt.relevant for attempt in trace.attempts),
            partial=sum(attempt.partial for attempt in trace.attempts),
            kept_chunk_ids=kept,
            model_calls=trace.model_calls,
            cost_usd=trace.cost_usd,
            notes=tuple(trace.notes),
        )

    on_text, on_model, on_in, on_out, on_usd = await _generate(
        case,
        router=router,
        tracer=tracer,
        budget=on_budget,
        passages=retrieved,
        prompt_version=_PROMPT_LABELS[config.prompt_version][1],
        build_prompt=PROMPT_BUILDERS[config.prompt_version],
    )
    on_enforced = _enforce_channel_format(case, on_text)
    on_text = on_enforced.text
    on_cited = _cited_ids(on_text)
    on_grounding = _grounding_triple(on_text, cited=on_cited, retrieved=retrieved, oracle=oracle)
    rag_on = ArmResult(
        arm="rag_on",
        text=on_text,
        results=_score_arm(
            case, arm="rag_on", text=on_text, retrieved=retrieved, grounding=on_grounding
        ),
        grounding=on_grounding,
        cited_chunk_ids=on_cited,
        retrieval=retrieval,
        model=on_model,
        tokens_in=on_in,
        tokens_out=on_out,
        cost_usd=on_usd,
        error=error,
        hashtags_removed=on_enforced.removed,
        hashtag_shortfall=on_enforced.shortfall,
    )

    # ---- the control: the human reference answer, same three conditions ---- #
    reference = case.reference_answer
    reference_grounding = _grounding_triple(
        reference,
        cited=tuple(retrieved),
        retrieved=retrieved,
        oracle=oracle,
    )

    row = CaseRow(
        case_id=case.case_id,
        vertical=case.business.vertical,
        channel=case.channel,
        rag_off=rag_off,
        rag_on=rag_on,
        reference_grounding=reference_grounding,
        reference_brand_passed=score_brand(reference, case.banned_claims).passed,
        violating_brand_passed=score_brand(case.violating_answer(), case.banned_claims).passed,
    )

    # Feedback-as-a-score is the second half of the observability deliverable, so
    # the harness uses the same path a human thumbs-up would.
    tracer.score(
        trace_id=case.case_id,
        name="rubric.mean.rag_on",
        value=rag_on.mean_score,
        comment=f"{case.channel} / {case.business.vertical}",
    )
    return row


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _dimension_score(results: Sequence[RubricResult], dimension: str) -> str:
    """One table cell. ``—`` means the dimension does not apply to this channel.

    ``n/e`` marks a score of 1.00 that was never actually tested -- no figures to
    trace, no banned claims configured. Rendering that identically to an earned 1.00
    would let the table overstate the result without a single false sentence.
    """
    for result in results:
        if result.dimension == dimension:
            return f"{_fmt(result.score)} n/e" if not result.exercised else _fmt(result.score)
    return "—"


def render_report(
    *,
    config: RunConfig,
    rows: Sequence[CaseRow],
    notes: Sequence[str],
) -> str:
    """Render the markdown report. Pure: no I/O, so it is testable.

    The header is the most important part of the document. A table of numbers
    produced against canned responses looks exactly like a table of numbers
    produced against a frontier model, and only the header can tell them apart.
    """
    providers = config_status(env=config.env)
    tracing = tracing_status(env=config.env)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        "# Evaluation report",
        "",
        f"Generated {generated_at} · `uv run python evals/run.py"
        f"{' --live' if config.live else ''}`",
        "",
        "## What produced these numbers",
        "",
    ]

    if providers.using_fake_provider:
        lines += [
            "> **Every number below was produced against the FAKE provider.** No model was "
            "called: `FakeProvider` returns a canned string chosen by hashing the prompt, so "
            "this report measures **the harness and the rubric, not the models**. Treat it as "
            "evidence that the evaluation machinery works and is honest, not as evidence that "
            "the generated copy is good. Run `uv run python evals/run.py --live` with "
            "`OPENROUTER_API_KEY` set to measure models — that spends real money.",
            "",
        ]
    else:
        lines += [
            "> **Live run.** Real providers were called and real money was spent. "
            f"{providers.message}",
            "",
        ]

    tier = config.tier or TASK_TIERS[TaskClass.GENERATE]
    chain = ", ".join(f"`{entry.provider}/{entry.model}`" for entry in TIER_CHAINS[tier])
    tier_note = (
        f"**{tier.value}**"
        + (" (default for GENERATE)" if config.tier is None else " (overridden by `--tier`)")
        + f" — chain in preference order: {chain}"
    )

    lines += [
        f"- **Model providers:** {providers.message}",
        f"- **Generation tier:** {tier_note}. Entries whose provider has no credential are "
        "skipped, and a 403/404 on one model falls through to the next, so the model that "
        "served is not always the first listed — the per-case rows carry the actual model.",
        f"- **Tracing:** {tracing.message}",
        f"- **Ragas faithfulness / answer relevancy:** {RAGAS_NOTE}. The columns are "
        "reserved and rendered empty; no value is estimated from the deterministic "
        "scores, because a faithfulness number that is really a rubric average would "
        "be a fabrication.",
        f"- **Generation prompt:** `{config.prompt_version}` "
        f"({', '.join(f'`{label}`' for label in _PROMPT_LABELS[config.prompt_version])}). "
        "Rerun with `--prompt-version v1` to reproduce the original numbers; v1 is kept "
        "executable so a prompt improvement stays a measurement rather than a claim.",
        f"- **Cases:** {len(rows)} of {len(CASES)} in the eval set.",
        "- **Scoring:** deterministic (`evals/rubric.py`). No model is used as a judge.",
        "",
    ]

    lines += _render_per_case(rows)
    lines += _render_aggregate(rows)
    lines += _render_rag_comparison(rows)
    lines += _render_self_check(rows)
    lines += _render_fatal(rows)

    if notes:
        lines += ["## Run notes", ""]
        lines += [f"- {note}" for note in notes]
        lines += [""]

    lines += _render_limitations()
    return "\n".join(lines)


def _render_per_case(rows: Sequence[CaseRow]) -> list[str]:
    lines = [
        "## Per case",
        "",
        "One row per case per arm. **`—`** means the dimension does not apply to the "
        "channel: a social post has no title tag, so scoring one would produce a number "
        "about nothing. **`n/e`** means *not exercised* — the cell scored 1.00 only "
        "because there was nothing to check (no figures to trace, no banned claim "
        "configured), which is an absence of risk rather than a pass.",
        "",
        "| case | channel | arm | seo | brand | format | grounding | coverage | mean | "
        "ragas faithfulness | ragas relevancy |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        for arm in (row.rag_off, row.rag_on):
            lines.append(
                f"| `{row.case_id}` | {row.channel} | {arm.arm} | "
                f"{_dimension_score(arm.results, 'seo')} | "
                f"{_dimension_score(arm.results, 'brand')} | "
                f"{_dimension_score(arm.results, 'format')} | "
                f"{_dimension_score(arm.results, 'grounding')} | "
                f"{_dimension_score(arm.results, 'coverage')} | "
                f"**{_fmt(arm.mean_score)}** | — | — |"
            )
    lines.append("")
    return lines


def _render_aggregate(rows: Sequence[CaseRow]) -> list[str]:
    lines = [
        "## Aggregate",
        "",
        "`not exercised` counts the cells that scored 1.00 with nothing to check. Read "
        "the mean against it: a high average carried by untested dimensions is not a "
        "high average.",
        "",
        "| arm | cases | mean | dimensions passed | failed | not exercised |",
        "|---|---|---|---|---|---|",
    ]
    for arm_name in ("rag_off", "rag_on"):
        arms = [getattr(row, arm_name) for row in rows]
        summary = aggregate([result for arm in arms for result in arm.results])
        lines.append(
            f"| {arm_name} | {len(arms)} | **{_fmt(summary.mean_score)}** | "
            f"{summary.passed} | {summary.failed} | {summary.not_exercised} |"
        )
    lines.append("")

    if rows:
        lines += ["Per dimension:", "", "| dimension | rag_off | rag_on |", "|---|---|---|"]
        off = aggregate([result for row in rows for result in row.rag_off.results])
        on = aggregate([result for row in rows for result in row.rag_on.results])
        for dimension in sorted(set(off.by_dimension) | set(on.by_dimension)):
            lines.append(
                f"| {dimension} | {_fmt(off.by_dimension.get(dimension, 0.0))} | "
                f"{_fmt(on.by_dimension.get(dimension, 0.0))} |"
            )
        lines.append("")

    lines += _render_enforcement(rows)
    return lines


def _render_enforcement(rows: Sequence[CaseRow]) -> list[str]:
    """What the deterministic channel engine had to correct.

    This sits directly under the `format` row on purpose. Hashtag counts are
    enforced in code, so `format` can read well *because* a renderer cleaned up
    after the model -- and a table that showed only the clean score would be
    reporting the renderer's competence as the model's. These two numbers are how
    the reader tells the difference, and how a future prompt change can be judged:
    fewer corrections means the prompt got better, not just the output.
    """
    lines = [
        "### Deterministic format enforcement",
        "",
        "Hashtag counts are enforced by `backend/app/engines/channel` before scoring, "
        "because counting is arithmetic and a model will not do it reliably — measured "
        "on `gpt-4.1-mini`, the bare instruction `Keine Hashtags` still produced 21. "
        "**Read the `format` row above together with this one:** a correction is work "
        "the model left for the renderer, so a clean `format` score with a high "
        "correction count is the renderer's competence and not the model's. **Zero "
        "corrections is therefore the good outcome, not a sign the check is idle** — "
        "it means the prompt carried the rule on its own. Length is deliberately *not* "
        "enforced: truncating or padding copy would be editing it rather than "
        "formatting it, so `format` can still fail on length.",
        "",
        "| arm | pieces corrected | hashtags removed | pieces left short of the minimum |",
        "|---|---|---|---|",
    ]
    for arm_name in ("rag_off", "rag_on"):
        arms = [getattr(row, arm_name) for row in rows]
        corrected = sum(1 for arm in arms if arm.hashtags_removed)
        removed = sum(arm.hashtags_removed for arm in arms)
        short = sum(1 for arm in arms if arm.hashtag_shortfall)
        lines.append(f"| {arm_name} | {corrected} of {len(arms)} | {removed} | {short} |")
    lines.append("")
    return lines


def _render_rag_comparison(rows: Sequence[CaseRow]) -> list[str]:
    lines = [
        "## RAG off vs RAG on vs oracle",
        "",
        "Grounding is the dimension RAG exists to move, so it is reported under three "
        "evidence conditions. **`rag_off`** offers the scorer no chunks. **`rag_on`** "
        "offers exactly the chunks the shipped agentic retrieval loop kept. **`oracle`** "
        "offers every fact in the case — the ceiling perfect retrieval would allow.",
        "",
        "The oracle column is the honest part: without it, a two-column table cannot "
        "distinguish *retrieval found nothing* from *there was nothing to find*.",
        "",
        "| case | retrieval outcome | kept | generated off | generated on | generated oracle "
        "| reference off | reference on | reference oracle |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        retrieval = row.rag_on.retrieval
        outcome = retrieval.outcome if retrieval else (row.rag_on.error or "not run")
        kept = len(retrieval.kept_chunk_ids) if retrieval else 0
        lines.append(
            f"| `{row.case_id}` | {outcome} | {kept} | "
            f"{_fmt(row.rag_off.grounding.off.score)} | "
            f"{_fmt(row.rag_on.grounding.on.score)} | "
            f"{_fmt(row.rag_on.grounding.oracle.score)} | "
            f"{_fmt(row.reference_grounding.off.score)} | "
            f"{_fmt(row.reference_grounding.on.score)} | "
            f"{_fmt(row.reference_grounding.oracle.score)} |"
        )
    lines.append("")

    if rows:
        kept_total = sum(
            len(row.rag_on.retrieval.kept_chunk_ids) if row.rag_on.retrieval else 0 for row in rows
        )
        grounded = sum(
            1 for row in rows if row.rag_on.retrieval and row.rag_on.retrieval.kept_chunk_ids
        )
        lines += [
            f"Retrieval kept **{kept_total} chunk(s) across {len(rows)} case(s)**, grounding "
            f"{grounded} of them.",
            "",
        ]
        if kept_total == 0:
            lines += [
                "> **Retrieval grounded nothing, and that is a real result rather than a bug.** "
                "`FakeProvider` fills a tool call's array argument with `[]`, so the grader "
                "returns no structured grades; `kb_service._grade` then treats every passage as "
                "`irrelevant` — the safe direction, because its contract is *cite only what was "
                "graded relevant*. The loop therefore reports `fallback_to_web` and refuses to "
                "ground a claim it cannot justify. The `oracle` column shows what the same text "
                "would score if those passages had been kept, which is the size of the gap a "
                "real grader has to close.",
                "",
            ]
    return lines


def _render_self_check(rows: Sequence[CaseRow]) -> list[str]:
    references_ok = sum(1 for row in rows if row.reference_brand_passed)
    mutations_caught = sum(1 for row in rows if not row.violating_brand_passed)
    return [
        "## Rubric self-check",
        "",
        "A rubric nobody has seen fail is a rubric nobody should believe. Each case "
        "carries a human-written correct answer and a mutation of it with a banned claim "
        "appended, so the rubric has to pass one and fail the other.",
        "",
        f"- Reference answers passing the brand check: **{references_ok}/{len(rows)}**",
        f"- Violating mutations correctly rejected: **{mutations_caught}/{len(rows)}**",
        "",
        "SEO and format discrimination are covered by unit tests "
        "(`backend/tests/evals/test_rubric.py`) rather than here, because they need "
        "markup and channel fixtures rather than prose.",
        "",
    ]


def _render_fatal(rows: Sequence[CaseRow]) -> list[str]:
    fatal: list[str] = []
    for row in rows:
        for arm in (row.rag_off, row.rag_on):
            for result in arm.results:
                if result.fatal:
                    fatal.extend(
                        f"`{row.case_id}` / {arm.arm} / {result.dimension}: {violation}"
                        for violation in result.violations
                    )
    lines = [
        "## Unpublishable outputs",
        "",
        "Listed separately from the averages on purpose: a banned claim, a channel that "
        "would reject the post, or a citation to a chunk that was never retrieved cannot "
        "be averaged away.",
        "",
    ]
    lines += [f"- {item}" for item in fatal] if fatal else ["- none", ""]
    if fatal:
        lines.append("")
    return lines


def _render_limitations() -> list[str]:
    return [
        "## What this harness cannot measure",
        "",
        "Stated here so the report cannot be read as claiming more than it does "
        "(`docs/CRITERIA_MAP.md` §7).",
        "",
        "1. **Whether the copy is any good.** Nothing here judges persuasion, tone, "
        "register or German grammar. A fluent, on-brand, useless paragraph scores the "
        "same as a good one.",
        "2. **Semantic faithfulness.** `score_grounding` checks that the *figures* in a "
        "claim appear in a cited chunk. A sentence that cites a real chunk and then "
        "misdescribes it in words passes. That is the gap Ragas would close, and Ragas "
        "is not installed.",
        "3. **Rankings.** The SEO column is a deterministic on-page audit. It does not "
        "predict Google positions, and a 1.00 is not a promise of traffic.",
        "4. **The article renderer.** It does not exist yet (Phase 6). For article cases "
        "the harness wraps the generated body in a minimal HTML skeleton built from the "
        "case, so the title, meta, link and schema rules score the skeleton and not a "
        "shipped renderer.",
        "5. **Retrieval quality beyond term overlap.** The harness embeds with a hashed "
        "bag of words, so a synonym does not retrieve. A retrieval miss here may be the "
        "embedder rather than the query rewrite.",
        "6. **Channel limits.** The rubric now grades against the SAME table the runtime "
        "renders to (`backend/app/engines/channel/specs.py`), so the eval can no longer "
        "disagree with the product it is grading -- the second copy that used to live here "
        "is gone. The numbers themselves still carry `docs/CHANNELS.md` §6's own "
        "instruction: verify every one against provider documentation.",
        "7. **Prompt v1 vs v2 and cheap vs strong model.** `docs/BUILD_ORDER.md` Phase 12 "
        "asks for both comparisons. Neither is implemented: there is one harness prompt "
        "per arm and the router picks the tier. The columns are absent rather than "
        "invented.",
        "",
    ]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: Sequence[str]) -> RunConfig:
    """Build a :class:`RunConfig` from the command line."""
    parser = argparse.ArgumentParser(
        prog="evals/run.py",
        description=(
            "Run the evaluation set and write a markdown report. Defaults to the "
            "fake provider: no network, no cost, and no risk of billing a real key "
            "that happens to be sitting in .env."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Use the real environment and real providers. THIS SPENDS MONEY: about "
            "two model calls plus retrieval per case, over 20 cases."
        ),
    )
    parser.add_argument(
        "--tier",
        choices=[tier.value for tier in ModelTier if tier is not ModelTier.EMBED],
        default=None,
        help=(
            "route GENERATE to this tier instead of its default (strong). Use to "
            "compare tiers, or when the configured credential cannot reach the "
            "strong chain. The report header names the tier either way."
        ),
    )
    parser.add_argument(
        "--prompt-version",
        choices=sorted(PROMPT_BUILDERS),
        default=DEFAULT_PROMPT_VERSION,
        help=(
            "which generation prompt to run. v1 is the original, kept executable so "
            "the improvement over it can be re-measured rather than trusted. The "
            "report header names whichever was used."
        ),
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT_PATH, help="report path")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="CASE_ID",
        help="run only this case id (repeatable)",
    )
    parsed = parser.parse_args(argv)
    return RunConfig(
        live=bool(parsed.live),
        out_path=parsed.out,
        only=tuple(parsed.case),
        tier=None if parsed.tier is None else ModelTier(parsed.tier),
        prompt_version=str(parsed.prompt_version),
    )


async def run(config: RunConfig) -> tuple[str, list[CaseRow]]:
    """Run the configured cases and render the report."""
    # `task_tiers` is a constructor argument the router already exposes for tests,
    # so a tier override needs no production change at all.
    task_tiers = None if config.tier is None else {**TASK_TIERS, TaskClass.GENERATE: config.tier}
    router = ModelRouter(env=config.env, task_tiers=task_tiers)
    tracer = get_tracer(env=config.env)

    cases = [case for case in CASES if not config.only or case.case_id in config.only]
    notes: list[str] = []
    if config.only:
        notes.append(f"Restricted to {len(cases)} case(s) by --case, so the aggregate is a subset.")

    rows: list[CaseRow] = []
    for case in cases:
        row = await run_case(case, router=router, tracer=tracer, config=config)
        if row.rag_on.error:
            notes.append(f"`{case.case_id}`: retrieval failed — {row.rag_on.error}")
        rows.append(row)

    return render_report(config=config, rows=rows, notes=notes), rows


def main(argv: Sequence[str] | None = None) -> int:
    """Write the report and print where it went."""
    config = parse_args(list(sys.argv[1:] if argv is None else argv))

    if config.live:
        # --live has to LOAD the credentials it intends to spend, and then PROVE it got
        # them, because the first version did neither.
        #
        # It read the ambient environment only, and this project keeps its key in `.env`
        # (loaded at process start by backend/app/asgi.py, which an eval script does not
        # import). So `--live` found no credential, the router fell back to FakeProvider,
        # and the run printed "This spends money" while spending nothing and producing
        # numbers identical to a fake run. The report header was honest about the actual
        # provider — which is the only reason this was not a silent lie — but the flag
        # was doing nothing.
        #
        # Now: load `.env` (without overriding a real environment variable, so a
        # container still wins), then REFUSE to continue if the router still resolves to
        # the fake. A live report produced on canned responses is worse than no report,
        # so this fails loudly rather than measuring nothing under a --live banner.
        from dotenv import load_dotenv

        load_dotenv(override=False)

        status = config_status()
        if status.using_fake_provider:
            print(
                "--live was requested but no model provider is configured, so every call "
                "would be served by FakeProvider and the numbers would measure the "
                "harness rather than the models.\n\n"
                f"  {status.message}\n\n"
                "Set OPENROUTER_API_KEY (or ANTHROPIC_API_KEY) in .env or the "
                "environment, or drop --live to run the hermetic evaluation.",
                file=sys.stderr,
            )
            return 2

        tier = config.tier or TASK_TIERS[TaskClass.GENERATE]
        print(
            "--live: calling real providers. THIS SPENDS MONEY. "
            f"Available: {', '.join(status.available_providers)}. "
            f"GENERATE tier: {tier.value}.",
            file=sys.stderr,
        )

    report, rows = asyncio.run(run(config))
    config.out_path.parent.mkdir(parents=True, exist_ok=True)
    config.out_path.write_text(report, encoding="utf-8")

    off = aggregate([result for row in rows for result in row.rag_off.results])
    on = aggregate([result for row in rows for result in row.rag_on.results])
    print(
        f"{len(rows)} case(s) scored. rag_off mean {off.mean_score:.2f}, "
        f"rag_on mean {on.mean_score:.2f}. Report: {config.out_path}"
    )
    if not config.live:
        print(
            "Ran on the FAKE provider: this measures the harness, not the models. "
            "The report says so in its header."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
