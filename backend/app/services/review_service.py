"""What the review screen shows: one run's output, projected out of its checkpoint.

Two projections live here, over the same checkpoint: the four review tabs
(:func:`project_review`) and the Tier 3 export pack (:func:`project_export_pack`).

The review surface has a tab per kind of output — the blog draft, the deterministic SEO
findings, the social posts per channel, and the "AI blocks" an answer engine can quote,
plus the retrieval trace and the delivery record. Those first four are
already produced by the graph and already persisted: ``AgentState`` carries ``draft``,
``seo_report``, ``renderings`` and ``outline``, and ``runs.checkpoint`` is that state
after :func:`backend.app.agents.state.to_checkpoint`. So this module invents no storage
and no new field. It reads the checkpoint and answers, per tab, *what is there and what
is not*.

Three decisions shape it.

**It is a PURE function over the checkpoint mapping.** No session, no engine, no model.
That is what lets the whole review contract be asserted without a database, and it keeps
the route thin, which is the layering rule in docs/ARCHITECTURE.md section 4.

**Absence is a first-class answer, and it names the node responsible.** Every section
carries ``available`` and, when it is false, a ``note`` that says which node produces it
and that the node has not run. "There is no draft" is information; an empty panel that
looks like a rendering bug is not. Fabricating placeholder content here would be the one
unforgivable failure on this screen — the whole product claim is that output is grounded,
so a review screen that invents a draft is worse than one that shows none.

**The checkpoint is JSONB, so it is read defensively.** It can hold whatever a previous
version of the state, a hand-run SQL statement, or a partially-written run put there. A
malformed checkpoint degrades to "not available" rather than raising: a run whose review
screen 500s cannot be reviewed at all, and the owner has no way to tell that apart from
an outage. `retrieval` is the sharpest case of this: the key postdates the earliest
checkpoints, so an older run simply has none, and that reads the same as a business with
no documents -- which is why its note describes the mechanism instead of naming a fault.

**No chunk text crosses this wire, and none is in the checkpoint to cross it.**
:func:`backend.app.agents.nodes.summarise_retrieval` drops every chunk body before the
state is written, so the retrieval panel carries chunk IDS, grades and decisions. That is
the auditable part -- an id plus a grade plus the grader's reason answers "is this claim
grounded, and how do we know" -- while the body is one indexed lookup away in
`document_chunks` and would otherwise be re-serialised on every one of a run's nodes.

One thing deliberately NOT returned: a per-channel character LIMIT. Two limit tables
already disagree in this repo (``agents.nodes.CHANNEL_LIMITS`` against
``evals.rubric.CHANNEL_LIMITS`` — see the open Phase 11 backlog item, which predicted
exactly this), so publishing one of them on the wire would make this module a third copy
and the review screen would eventually contradict the rubric that grades it. The measured
character count IS returned, because that is arithmetic over the text in hand and cannot
drift. REPACK has already enforced its ceiling by the time a rendering is stored.

------------------------------------------------------------------------------
The export pack (docs/CHANNELS.md section 2, Tier 3)
------------------------------------------------------------------------------

Tier 3 — "copy-paste ready" — is this product's actual publishing story: it works on
every platform on day one, with no App Review and no token, and for many customers it is
what they want anyway. :func:`project_export_pack` is what makes it a deliverable rather
than a copy button, and it is a SIBLING FUNCTION IN THIS MODULE rather than a module of
its own, for three reasons:

* it reads the same checkpoint through the same defensive readers. A separate module
  would either import ten private helpers across a boundary or grow a second copy of
  them, and a second ``_text``/``_strings``/``_int`` is a second set of edge cases for
  the same JSONB column;
* the two projections must agree. ``characters`` on the review screen and
  ``bodyCharacters`` in the pack are the same measurement of the same string, and the
  cheapest way to keep two numbers identical is for one function to be able to see the
  other;
* the absence contract is identical — name the node, invent nothing — and it is stated
  once, above, for both.

What the pack adds beyond the review screen is the *cost of posting it by hand*: the
character count against the channel's editorial target AND its platform ceiling, the
hashtag count against the cap, whether a link in the body is clickable at all
(``ChannelSpec.link_in_body`` — the Instagram/TikTok truth that
docs/CHANNELS.md section 1 calls the correction that matters most), and one block of text
per channel that is exactly what should be pasted.

Two honesty rules are load-bearing here, because this is the surface where a small
exaggeration becomes a double-posted or an unpostable post:

**Nothing in the pack claims to have been published.** No actuator is called, no
``actions`` row is written, and no wording in the rendered file says otherwise. The
``publish`` actuator exists (``actuators/contract.py``) and the EXPORT node that would
drive it is specified and not in the graph — so the pack says that, rather than implying
a connection that is not there.

**The tracked short link is REPORTED ABSENT rather than invented.** Short links are minted
by :func:`backend.app.services.landing_service.publish_landing_page`, which no route and
no node calls yet, so no run has one. A pack that printed a plausible ``/l/xxxxxxxx``
would be a URL that 404s in somebody's Instagram bio. The bio-link hub URL IS real —
``GET /go/{business_id}`` resolves permanently (see ``api/links.py``) — so the pack
carries that, with what it does and does not contain stated beside it.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from backend.app.engines.channel import CHANNEL_SPECS, ChannelSpec, canonical_channel

__all__ = [
    "AiBlocks",
    "Draft",
    "ExportChannel",
    "ExportCta",
    "ExportDistribution",
    "ExportPack",
    "ExportProofPoint",
    "Opportunity",
    "RetrievalAttempt",
    "RetrievalGrade",
    "RetrievalTrace",
    "ReviewFinding",
    "RunReview",
    "SeoReport",
    "SocialPost",
    "project_export_pack",
    "project_review",
    "render_export_markdown",
]


#: Why the publish and measure sections are empty, phrased as the MECHANISM.
#:
#: "EXPORT has not run" is the normal state of every unapproved run rather than a fault,
#: and saying so is the difference between a screen that reads as unfinished work and one
#: that reads as broken software. Both name the gate, because that is the thing the
#: reader can act on.
_NOT_PUBLISHED: Final = (
    "Nothing has been published yet. EXPORT runs only after a human approves the run at "
    "the review gate -- approving it is what lets it publish."
)
_NOT_MEASURED: Final = (
    "Nothing has been measured yet. MEASURE runs after EXPORT, so there is nothing to "
    "measure until something has been published."
)

#: Why a tab is empty, phrased as the mechanism rather than as an apology. Each names
#: the node that fills it, because "GENERATE has not run" is actionable and "no data"
#: is not.
_NO_CHECKPOINT: Final = (
    "This run has not saved any state yet, so there is nothing to review. "
    "Output appears here as the graph reaches each node."
)
_NO_DRAFT: Final = (
    "No draft was written. The page is produced by the GENERATE node, which has "
    "not completed for this run."
)
_NO_SEO: Final = (
    "No SEO score was recorded. Scoring is the VALIDATE node, which runs on a draft — "
    "so there is nothing to score until GENERATE has produced one."
)
_NO_SOCIAL: Final = (
    "No social posts were rendered. Per-channel copy is produced by the REPACK node, "
    "which has not completed for this run."
)
_NO_OUTLINE: Final = (
    "No answer blocks were produced. They come from the PLAN node's outline, which has "
    "not completed for this run."
)
_OUTLINE_WITHOUT_BLOCKS: Final = (
    "PLAN produced an outline but no answer blocks. They are an optional field on the "
    "outline, so the model returned none for this run — nothing has been hidden, and "
    "nothing has been invented to fill the tab."
)
#: Why there is no retrieval trace, phrased as the mechanism and NOT as a fault.
#:
#: This is the empty state that is easiest to get wrong, because the honest reading is
#: counter-intuitive: for a business that has uploaded no documents there is nothing to
#: retrieve, so nothing was retrieved, and that is a complete and successful run. A note
#: that said "retrieval failed" or "no results" would report the absence of a knowledge
#: base as a defect in the agent -- and would tell the owner to go looking for a bug
#: instead of uploading a PDF.
#:
#: It names HARVEST because HARVEST is the node that records the absence, and that
#: record is the thing the reader can already see: "uploaded documents" appears in
#: `fact_gaps` above the tabs. Deliberately contains no word for failure -- there is
#: nothing here to soften, and a hedge would imply there was.
_NO_RETRIEVAL: Final = (
    "No document retrieval ran for this run. Retrieval reads the business's own "
    "uploaded documents, and this business has none on record -- so the work was "
    "written from its confirmed profile and the live research instead, which HARVEST "
    "records above under what it was written without. That is a normal run: there was "
    "nothing to retrieve, so nothing was retrieved."
)

_NO_LANDING: Final = (
    "No destination was chosen, so the posts in this pack carry no link. The plan, "
    "its offer and its per-channel asks come from the CONVERT node, which has not "
    "completed for this run."
)

#: Why there is no per-channel tracked link, and what to do instead. Stated rather than
#: filled: a plausible-looking `/l/xxxxxxxx` in the pack is a dead link in somebody's
#: Instagram bio, which is worse than an honest gap because the failure is invisible.
#:
#: Worded as a fact about THIS pack — a short link is not in it — rather than as a claim
#: about which node is missing. That wording is what saved it: `publish.page` became a
#: real actuator, so the note is now the EXCEPTION rather than the rule, and it appears
#: only when a run genuinely published nothing. A sentence naming the missing node would
#: have gone stale on the day it landed.
_NO_TRACKED_LINK: Final = (
    "No tracked short link is in this pack. A short link is minted when a run is "
    "published to a public address, and that has not happened for this run — so put your "
    "own destination address in the posts that can carry one, and use the bio-link hub for "
    "the channels that cannot."
)

#: What the hub is, and what it is not. The route resolves for any business, so the URL
#: is always real; whether it has anything ON it depends on what has been approved, and
#: saying so is the difference between a working link and an apparently broken one.
_HUB_NOTE: Final = (
    "This is the business's own bio-link page and it is the entire conversion path for "
    "Instagram and TikTok, where a link in the body is not clickable. It lists the CTAs "
    "that have been approved for this business, so it is empty until one has been."
)

#: The one sentence the pack exists to make unmissable.
#:
#: Phrased as a fact about what THIS ROUTE does rather than about the product's
#: capabilities, and that is deliberate. "No platform is connected" is a claim about a
#: deployment: it stops being true the day a `publish` actuator has a credential, and a
#: stale reassurance is worse than none. What is permanently true is that projecting a
#: pack calls no actuator and sends nothing, so that is what it says.
_NOTHING_PUBLISHED: Final = (
    "This pack sends nothing to any platform. Every block below is text for a person to "
    "paste in themselves — which is why it works even on a channel we cannot publish to "
    "at all."
)


class _Wire(BaseModel):
    """camelCase on the wire, snake_case in Python — as everywhere else in this API."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class Draft(_Wire):
    """The page GENERATE wrote. Stored exactly as the model returned it."""

    title: str
    meta_description: str
    html: str


class ReviewFinding(_Wire):
    """One SEO rule's verdict, as the review screen renders it.

    ``fix_hint`` is the actionable half and is carried through verbatim: it is
    quantitative by contract (it names the measured value and the target), and it is
    the same string the retry loop feeds back to GENERATE. A review screen that showed
    only ``message`` would tell the owner something is wrong without telling them what
    to change.
    """

    code: str
    severity: str
    message: str
    fix_hint: str
    measured: float | None
    expected: str


class SeoReport(_Wire):
    """The deterministic verdict. Not a model's opinion — arithmetic over the markup."""

    score: int
    passed: bool
    findings: tuple[ReviewFinding, ...]
    #: Set by VALIDATE when it could not score at all (no draft HTML), so the screen can
    #: distinguish "scored zero" from "nothing to score".
    note: str | None = None


class SocialPost(_Wire):
    """One channel's copy.

    ``characters`` is computed here rather than in the client so the count on the screen
    is the count the server measured.

    The limits DO ship now, and the module docstring records why that changed: there is
    one channel spec table (``engines/channel/specs.py``) that the runtime renders to and
    the eval grades against, so publishing the numbers is no longer minting a third copy
    that can contradict either.

    ``hashtags_removed`` is the uncomfortable field and the one most worth keeping. It is
    evidence about the MODEL: a screen that shows three tidy hashtags without saying five
    were cut out in code is reporting the renderer's competence as the model's.
    """

    channel: str
    body: str
    characters: int
    hashtags: tuple[str, ...] = ()
    #: How many hashtags code had to remove, and how many are still missing. Both zero
    #: on a post the model got right, which is the point of showing them.
    hashtags_removed: int = 0
    hashtags_shortfall: int = 0
    #: The channel's editorial target and its platform ceiling. ``None`` for a channel
    #: the spec table does not cover -- rendering "0 / 0" there would be a false limit.
    character_target: int | None = None
    character_limit: int | None = None
    hashtag_limit: int | None = None
    #: Over the editorial target but inside the platform limit: publishable, and longer
    #: than it should be. A different fact from "too long", and reported as one.
    over_target: bool = False


class AiBlocks(_Wire):
    """The content shaped for AI answer engines.

    ``blocks`` is ``outline.answer_blocks``: self-contained, quotable answers that must
    stand alone without the page, which is precisely what an answer engine can cite.
    The keyword and headings travel with them because a block is reviewed against the
    intent it was written for.
    """

    target_keyword: str | None
    blocks: tuple[str, ...]
    headings: tuple[str, ...]
    cta: str | None


class RetrievalGrade(_Wire):
    """One chunk, as retrieval judged it.

    There is no chunk TEXT here and there is none in the checkpoint either -- the id,
    the document and the ordinal are the citation, and the body is one indexed lookup
    away. See `agents.nodes.summarise_retrieval`: carrying bodies would put the
    business's knowledge base into a JSONB column that is rewritten eleven times a run.

    ``reason`` is the grader's own justification and is the field that makes the grade
    reviewable. A screen showing "irrelevant" with no reason is asking the owner to
    take the model's word for it, which is the opposite of what this panel is for.
    """

    chunk_id: str
    document_id: str
    ordinal: int
    #: ``relevant`` · ``partial`` · ``irrelevant``. A string, not a union: the
    #: retrieval service owns this vocabulary and can add to it.
    grade: str
    reason: str | None
    #: Vector distance. ``None`` when the trace did not record one -- which is not zero,
    #: and rendering it as zero would report the worst possible match as the best.
    distance: float | None


class RetrievalAttempt(_Wire):
    """One turn of the retrieval loop: the rewritten query, the grades, the decision.

    ``query`` is the REWRITE -- what was actually embedded, in the words the documents
    would use, rather than the node's own question. It is the single most load-bearing
    field on this panel: a system that embeds the user's words verbatim is doing vector
    search, and one that rewrites them, grades what comes back and decides what to do
    next is doing agentic retrieval. Showing the rewrite is how that claim is checkable
    rather than asserted.
    """

    attempt: int
    query: str
    query_rationale: str | None
    #: ``sufficient`` · ``retry`` · ``exhausted``. A string, for the same reason
    #: `RetrievalGrade.grade` is one.
    decision: str
    decision_reason: str | None
    relevant: int
    partial: int
    irrelevant: int
    grades: tuple[RetrievalGrade, ...]
    #: How many chunks were graded, against how many are listed above. Equal on every
    #: ordinary run; unequal says the summariser trimmed, which the reader is owed.
    grades_total: int
    notes: tuple[str, ...]


class RetrievalTrace(_Wire):
    """One node's whole retrieval: question, attempts, grades, and the final decision.

    ``outcome`` is the fallback decision and the reason this projection exists.
    ``fallback_to_web`` means the business's own documents did not answer, so the run
    went on with live research instead -- a decision the agent MADE, and one a reviewer
    has to be able to see it make. ``not_needed`` is likewise a decision and not a
    miss: judging that a step needs no business facts is one of the choices that makes
    this retrieval agentic.
    """

    #: 1-based ordinal over the run's retrieval calls, carried through the count cap.
    #: A panel whose first entry is not ``1`` has said that earlier calls were dropped.
    seq: int
    #: The graph node that asked. The trace itself does not know -- the same retrieval
    #: service is called from five nodes.
    node: str
    #: What the node asked for, before rewriting.
    question: str
    needed: bool
    need_reason: str | None
    #: ``sufficient`` · ``fallback_to_web`` · ``not_needed``.
    outcome: str
    outcome_reason: str | None
    prompt_version: str | None
    attempts: tuple[RetrievalAttempt, ...]
    attempts_total: int
    #: Chunks graded ``relevant``: the only citable evidence.
    grounding_chunk_ids: tuple[str, ...]
    #: Chunks retrieval stood behind, relevant and partial together. Larger than
    #: ``grounding_chunk_ids`` when partials were carried as weak context.
    chunk_count: int
    model_calls: int
    #: A string, like every money value on this wire.
    cost_usd: str | None
    notes: tuple[str, ...]


class Opportunity(_Wire):
    """Why this topic and not another. Context for the draft tab, not a tab of its own."""

    title: str
    rationale: str | None
    score: int | None


class PublishedTarget(_Wire):
    """One destination EXPORT tried, and what actually happened to it.

    `simulated` is the field this whole model exists to carry. A destination whose post
    never left the process must not be renderable as a success, and the only way to
    guarantee that on a screen is to hand the screen the fact.
    """

    action_type: str
    target: str
    status: str
    external_ref: str | None
    error: str | None
    simulated: bool
    #: The actuator's own one-line rendering, which already refuses to overstate. Carried
    #: verbatim so a surface that shows nothing else still cannot claim a real post.
    summary: str


class Published(_Wire):
    """What EXPORT did, per destination, plus the honest headline.

    `note` is the sentence EXPORT composed and is not recomputed here: it already folds
    "published N of M", the destinations that failed, and whether anything was simulated.
    A screen deriving its own headline from `targets` would be a second place for that
    arithmetic to be wrong.
    """

    note: str
    attempted: int
    succeeded: int
    simulated: bool
    notified: bool
    notify_note: str | None
    targets: tuple[PublishedTarget, ...]


class Measurement(_Wire):
    """What MEASURE could and could not measure.

    `leadsMeasured` is false and stays false until a visitor actually arrives through a
    tracked link. `gaps` names what was not measured and why, because a screen that
    shows only the numbers it has implies the rest were zero.
    """

    published_refs: int
    channels: tuple[str, ...]
    simulated: bool
    gaps: tuple[str, ...]
    leads_measured: bool
    attribution_note: str | None


class RunReview(_Wire):
    """Everything the review tabs need, plus the honesty carried alongside them.

    ``fact_gaps`` and ``errors`` are not decoration. They are what lets the screen say
    "written without live research" instead of implying research happened, which is the
    claims-discipline rule in docs/CRITERIA_MAP.md section 7 applied to UI copy.
    """

    has_output: bool
    draft: Draft | None
    draft_note: str | None
    seo: SeoReport | None
    seo_note: str | None
    social: tuple[SocialPost, ...]
    social_note: str | None
    ai_blocks: AiBlocks | None
    ai_blocks_note: str | None
    opportunity: Opportunity | None
    #: The agentic-RAG evidence, per node, in the order the run produced it. Empty for
    #: a business with no uploaded documents, which is a normal run -- see
    #: `retrieval_note`, which says so rather than leaving the panel to be read as a
    #: fault.
    retrieval: tuple[RetrievalTrace, ...]
    retrieval_note: str | None
    fact_gaps: tuple[str, ...]
    errors: tuple[dict[str, str], ...]
    #: What EXPORT did, or `None` when it never ran -- which is the case for every run
    #: that has not been approved, and is a different fact from "it published nothing".
    published: Published | None
    published_note: str | None
    measurement: Measurement | None
    measurement_note: str | None


# --------------------------------------------------------------------------- #
# Defensive reading. Everything below assumes the checkpoint may be malformed.
# --------------------------------------------------------------------------- #


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    """A string, or "" — never ``None`` leaking into a required field."""
    return value if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    text = _text(value).strip()
    return text or None


def _strings(value: Any) -> tuple[str, ...]:
    """A list of non-empty strings, dropping anything that is not one.

    Junk is dropped rather than raising for the same reason ``memory_service`` does it:
    one malformed entry must not make a whole run unreviewable.
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _draft(checkpoint: Mapping[str, Any]) -> Draft | None:
    raw = _mapping(checkpoint.get("draft"))
    title = _text(raw.get("title"))
    html = _text(raw.get("html"))
    if not title and not html:
        # A draft with neither a title nor a body is not a draft. Reporting it as
        # present would give the owner an empty page to approve.
        return None
    return Draft(
        title=title,
        meta_description=_text(raw.get("meta_description")),
        html=html,
    )


def _seo(checkpoint: Mapping[str, Any]) -> SeoReport | None:
    raw = checkpoint.get("seo_report")
    if not isinstance(raw, Mapping):
        return None
    findings_raw = raw.get("findings")
    findings: list[ReviewFinding] = []
    if isinstance(findings_raw, Sequence) and not isinstance(findings_raw, str | bytes):
        for item in findings_raw:
            entry = _mapping(item)
            code = _text(entry.get("code"))
            if not code:
                continue
            findings.append(
                ReviewFinding(
                    code=code,
                    severity=_text(entry.get("severity")) or "info",
                    message=_text(entry.get("message")),
                    fix_hint=_text(entry.get("fix_hint")),
                    measured=_float_or_none(entry.get("measured")),
                    expected=_text(entry.get("expected")),
                )
            )
    return SeoReport(
        score=_int(raw.get("score")),
        passed=bool(raw.get("passed", False)),
        findings=tuple(findings),
        note=_optional_text(raw.get("note")),
    )


@dataclass(frozen=True, slots=True)
class _StoredPost:
    """One rendering as the checkpoint holds it, decoded once for both projections.

    Its reason to exist is that the review screen and the export pack must never
    disagree about the same post: two decoders for the two stored shapes below would be
    two chances to read ``hashtags_removed`` differently, and the numbers would drift on
    two screens that show the same run.
    """

    channel: str
    body: str
    hashtags: tuple[str, ...]
    hashtags_removed: int
    hashtags_shortfall: int
    over_target: bool
    #: The channel's spec, or ``None`` for a channel the table does not cover. Resolved
    #: here so neither projection has to remember to fold the alias first.
    spec: ChannelSpec | None


def _stored_posts(checkpoint: Mapping[str, Any]) -> tuple[_StoredPost, ...]:
    """Every usable rendering in the checkpoint, in the order the mapping yields.

    Two shapes, and both are real rows in the database. REPACK used to store the bare
    body string; it now stores a mapping so the hashtags it asked the model for survive
    to this screen. Nothing migrates a JSONB display field, so an older run must keep
    rendering -- as a post with no hashtag information, which is true of it.
    """
    raw = checkpoint.get("renderings")
    if not isinstance(raw, Mapping):
        return ()

    posts: list[_StoredPost] = []
    for channel, value in raw.items():
        if isinstance(value, Mapping):
            body: Any = value.get("body")
            tags = _strings(value.get("hashtags"))
            removed = _int(value.get("hashtags_removed"))
            shortfall = _int(value.get("hashtags_shortfall"))
            over = bool(value.get("over_target", False))
        else:
            body, tags, removed, shortfall, over = value, (), 0, 0, False

        name = _text(channel).strip()
        text = _text(body).strip()
        if not name or not text:
            continue

        posts.append(
            _StoredPost(
                channel=name,
                body=text,
                hashtags=tags,
                hashtags_removed=removed,
                hashtags_shortfall=shortfall,
                over_target=over,
                spec=CHANNEL_SPECS.get(canonical_channel(name)),
            )
        )
    return tuple(posts)


def _social(checkpoint: Mapping[str, Any]) -> tuple[SocialPost, ...]:
    """Channels in whatever order the stored mapping yields, with no re-sorting.

    Worth stating plainly, because it is tempting to claim more: this order is NOT the
    order REPACK wrote. The checkpoint is a JSONB column and Postgres normalises object
    key order on the way in (by key length, then bytewise), so a `linkedin, facebook,
    instagram` rendering comes back as `facebook, linkedin, instagram`. Verified against a
    real row, not assumed.

    The order is therefore arbitrary but deterministic. This function deliberately does
    not impose one of its own: any sort we invented here would look like a priority the
    product does not have, and channel priority is a decision for whoever publishes, not
    for a projection function.
    """
    return tuple(
        SocialPost(
            channel=post.channel,
            body=post.body,
            characters=len(post.body),
            hashtags=post.hashtags,
            hashtags_removed=post.hashtags_removed,
            hashtags_shortfall=post.hashtags_shortfall,
            character_target=post.spec.max_chars if post.spec else None,
            character_limit=post.spec.hard_max_chars if post.spec else None,
            hashtag_limit=post.spec.hashtags_max if post.spec else None,
            over_target=post.over_target,
        )
        for post in _stored_posts(checkpoint)
    )


def _published(checkpoint: Mapping[str, Any]) -> tuple[Published | None, str | None]:
    """What EXPORT did, and the reason there is nothing when there is nothing.

    A pair, because "EXPORT never ran" and "EXPORT ran and published nothing" are
    different facts and only the second one is about the platforms. The first is the
    normal state of every run that has not been approved -- REVIEW is an interrupt, and
    EXPORT sits after it -- so the note names the gate rather than implying a failure.
    """
    raw = _mapping(checkpoint.get("published"))
    if not raw:
        return None, _NOT_PUBLISHED

    targets: list[PublishedTarget] = []
    succeeded = 0
    for entry in raw.get("refs") or ():
        row = _mapping(entry)
        status = _text(row.get("status"))
        if not status:
            continue
        if status == "succeeded":
            succeeded += 1
        targets.append(
            PublishedTarget(
                action_type=_text(row.get("action_type")),
                target=_text(row.get("target")),
                status=status,
                external_ref=_optional_text(row.get("external_ref")),
                error=_optional_text(row.get("error")),
                simulated=bool(row.get("fake", False)),
                summary=_text(row.get("summary")),
            )
        )

    return (
        Published(
            # EXPORT's own sentence, not a recomputed one: it already folds "N of M",
            # the destinations that failed and whether anything was simulated, and a
            # second place for that arithmetic is a second place for it to be wrong.
            note=_text(raw.get("note")),
            attempted=_int(raw.get("attempted")),
            succeeded=succeeded,
            simulated=bool(raw.get("simulated", False)),
            notified=bool(raw.get("notified", False)),
            notify_note=_optional_text(raw.get("notify_note")),
            targets=tuple(targets),
        ),
        None,
    )


def _measurement(checkpoint: Mapping[str, Any]) -> tuple[Measurement | None, str | None]:
    """What MEASURE measured, and what it could not.

    `leads_measured` is carried rather than derived from a count, because a count of
    zero and "nobody has arrived through a tracked link yet" are the same number and
    different claims -- and this product's whole argument is that the second one must
    never be printed as the first.
    """
    raw = _mapping(checkpoint.get("measurement"))
    if not raw:
        return None, _NOT_MEASURED

    attribution = _mapping(raw.get("attribution"))
    return (
        Measurement(
            published_refs=_int(raw.get("published_refs")),
            channels=_strings(raw.get("channels")),
            simulated=bool(raw.get("simulated", False)),
            gaps=_strings(raw.get("gaps")),
            leads_measured=bool(attribution.get("leads_measured", False)),
            attribution_note=_optional_text(attribution.get("note")),
        ),
        None,
    )


def _ai_blocks(checkpoint: Mapping[str, Any]) -> tuple[AiBlocks | None, str | None]:
    """Answer blocks, and the reason there are none when there are none.

    Returned as a pair because "no outline at all" and "an outline that carried no
    blocks" are different facts about the run, and only the second one means the model
    made a choice.
    """
    raw = checkpoint.get("outline")
    if not isinstance(raw, Mapping):
        return None, _NO_OUTLINE

    blocks = _strings(raw.get("answer_blocks"))
    payload = AiBlocks(
        target_keyword=_optional_text(raw.get("target_keyword")),
        blocks=blocks,
        headings=_strings(raw.get("headings")),
        cta=_optional_text(raw.get("cta")),
    )
    return payload, (None if blocks else _OUTLINE_WITHOUT_BLOCKS)


def _opportunity(checkpoint: Mapping[str, Any]) -> Opportunity | None:
    raw = _mapping(checkpoint.get("opportunity"))
    title = _optional_text(raw.get("title"))
    if title is None:
        return None
    score = raw.get("score")
    # An absent score and a score of zero are different facts about an opportunity, so
    # `_int`'s default cannot be used here: it would report "not scored" as 0.
    numeric = isinstance(score, int | float) and not isinstance(score, bool)
    return Opportunity(
        title=title,
        rationale=_optional_text(raw.get("rationale")),
        score=_int(score) if numeric else None,
    )


def _retrieval(checkpoint: Mapping[str, Any]) -> tuple[tuple[RetrievalTrace, ...], str | None]:
    """The retrieval traces, and the reason there are none when there are none.

    Read defensively like everything else here, and with one extra reason to be: this
    key postdates the first checkpoints ever written, so an older run legitimately has
    no `retrieval_traces` at all. That is indistinguishable, from here, from a run whose
    business had no documents — both get the same honest note, because from the reader's
    side both mean "there is no retrieval evidence for this run" and neither means the
    agent failed at anything.

    A malformed entry is DROPPED rather than raising, per this module's contract: one
    bad row must not make a whole run unreviewable. An entry with no node is dropped
    too, because the panel is organised per node and a trace that cannot say which node
    asked is evidence about nothing.
    """
    raw = checkpoint.get("retrieval_traces")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return (), _NO_RETRIEVAL

    traces: list[RetrievalTrace] = []
    for index, item in enumerate(raw, start=1):
        entry = _mapping(item)
        node = _optional_text(entry.get("node"))
        if node is None:
            continue
        attempts = tuple(
            _retrieval_attempt(_mapping(attempt)) for attempt in _sequence(entry.get("attempts"))
        )
        traces.append(
            RetrievalTrace(
                # Position in the list is the fallback, not zero: a trace written before
                # `seq` existed still has a place in the run's order, and numbering it 0
                # would claim earlier calls were dropped when none were.
                seq=_int(entry.get("seq"), index),
                node=node,
                question=_text(entry.get("question")),
                needed=bool(entry.get("needed", False)),
                need_reason=_optional_text(entry.get("need_reason")),
                outcome=_text(entry.get("outcome")),
                outcome_reason=_optional_text(entry.get("outcome_reason")),
                prompt_version=_optional_text(entry.get("prompt_version")),
                attempts=attempts,
                # Falls back to the number of attempts LISTED rather than to zero: this
                # field exists to reveal a trim, and a zero here would claim a trim of
                # everything on a checkpoint that simply never wrote the count.
                attempts_total=_int(entry.get("attempts_total"), len(attempts)),
                grounding_chunk_ids=_strings(entry.get("grounding_chunk_ids")),
                chunk_count=_int(entry.get("chunk_count")),
                model_calls=_int(entry.get("model_calls")),
                cost_usd=_optional_text(entry.get("cost_usd")),
                notes=_strings(entry.get("notes")),
            )
        )

    return tuple(traces), (None if traces else _NO_RETRIEVAL)


def _retrieval_attempt(entry: Mapping[str, Any]) -> RetrievalAttempt:
    grades = tuple(
        RetrievalGrade(
            chunk_id=_text(grade.get("chunk_id")),
            document_id=_text(grade.get("document_id")),
            ordinal=_int(grade.get("ordinal")),
            grade=_text(grade.get("grade")),
            reason=_optional_text(grade.get("reason")),
            distance=_float_or_none(grade.get("distance")),
        )
        for grade in (_mapping(item) for item in _sequence(entry.get("grades")))
    )
    return RetrievalAttempt(
        attempt=_int(entry.get("attempt")),
        query=_text(entry.get("query")),
        query_rationale=_optional_text(entry.get("query_rationale")),
        decision=_text(entry.get("decision")),
        decision_reason=_optional_text(entry.get("decision_reason")),
        relevant=_int(entry.get("relevant")),
        partial=_int(entry.get("partial")),
        irrelevant=_int(entry.get("irrelevant")),
        grades=grades,
        grades_total=_int(entry.get("grades_total"), len(grades)),
        notes=_strings(entry.get("notes")),
    )


def _sequence(value: Any) -> tuple[Any, ...]:
    """A list of anything, or empty. Strings are not sequences of items here."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(value)


def _errors(checkpoint: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    raw = checkpoint.get("errors")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    out: list[dict[str, str]] = []
    for item in raw:
        entry = _mapping(item)
        message = _text(entry.get("message"))
        if not message:
            continue
        out.append(
            {
                "node": _text(entry.get("node")),
                "code": _text(entry.get("code")),
                "message": message,
            }
        )
    return tuple(out)


def project_review(checkpoint: Mapping[str, Any] | None) -> RunReview:
    """Project one run's checkpoint into the review surface.

    Pure. Given the same checkpoint it returns the same review, which is what makes the
    contract testable without a database and what keeps the route a two-liner.
    """
    if not checkpoint:
        return RunReview(
            has_output=False,
            draft=None,
            draft_note=_NO_CHECKPOINT,
            seo=None,
            seo_note=_NO_CHECKPOINT,
            social=(),
            social_note=_NO_CHECKPOINT,
            ai_blocks=None,
            ai_blocks_note=_NO_CHECKPOINT,
            opportunity=None,
            retrieval=(),
            retrieval_note=_NO_CHECKPOINT,
            fact_gaps=(),
            errors=(),
            published=None,
            published_note=_NO_CHECKPOINT,
            measurement=None,
            measurement_note=_NO_CHECKPOINT,
        )

    draft = _draft(checkpoint)
    seo = _seo(checkpoint)
    social = _social(checkpoint)
    ai_blocks, ai_note = _ai_blocks(checkpoint)

    published, published_note = _published(checkpoint)
    measurement, measurement_note = _measurement(checkpoint)
    retrieval, retrieval_note = _retrieval(checkpoint)

    return RunReview(
        has_output=bool(draft or seo or social or (ai_blocks and ai_blocks.blocks)),
        draft=draft,
        draft_note=None if draft else _NO_DRAFT,
        seo=seo,
        seo_note=None if seo else _NO_SEO,
        social=social,
        social_note=None if social else _NO_SOCIAL,
        ai_blocks=ai_blocks,
        ai_blocks_note=ai_note,
        opportunity=_opportunity(checkpoint),
        retrieval=retrieval,
        retrieval_note=retrieval_note,
        fact_gaps=_strings(checkpoint.get("fact_gaps")),
        errors=_errors(checkpoint),
        published=published,
        published_note=published_note,
        measurement=measurement,
        measurement_note=measurement_note,
    )


# --------------------------------------------------------------------------- #
# The export pack: what a human pastes, and what it will cost them to get right
# --------------------------------------------------------------------------- #


#: How a link reaches a reader on this channel. ``bio_hub`` is not a preference, it is
#: the only route Instagram feed and TikTok have — a URL in the body there is plain text.
type LinkMechanism = Literal["inline", "bio_hub", "unknown"]


class ExportChannel(_Wire):
    """One channel's block, ready to paste, with the cost of pasting it stated.

    ``paste_text`` is the field that matters: it is the exact string to put in the
    composer, and it is assembled HERE rather than in the client so that the count next
    to it is a count of the same string. A client that joined the body and the hashtags
    itself would be measuring one thing and pasting another.

    Two character counts, and the distinction is not pedantry. ``body_characters`` is the
    number the review screen shows, so the two screens cannot appear to disagree;
    ``paste_characters`` is what the platform will actually receive, which is larger when
    a declared hashtag was not already in the body. The limits are compared against the
    second one, because that is the string being posted.
    """

    channel: str
    body: str
    #: Exactly what to paste: the body, plus any declared hashtag the body was missing.
    paste_text: str
    hashtags: tuple[str, ...]
    #: Declared hashtags that are NOT already written into the body, and are therefore
    #: appended to ``paste_text``. Usually empty — REPACK keeps the model's inline tags.
    appended_hashtags: tuple[str, ...]
    body_characters: int
    paste_characters: int
    #: The channel's editorial target and its platform reject threshold, from the one
    #: spec table the runtime renders to and the eval grades against. ``None`` for a
    #: channel that table does not cover: "0 / 0" would be a limit we made up.
    character_target: int | None
    character_limit: int | None
    hashtag_count: int
    hashtag_minimum: int | None
    hashtag_limit: int | None
    hashtags_removed: int
    hashtags_shortfall: int
    #: Over the editorial target, inside the platform limit: publishable, and longer than
    #: it should be.
    over_target: bool
    #: Over the platform's own reject threshold. REPACK trims the body to it, so this can
    #: only be true when appending the declared hashtags pushed it past — which is worth
    #: saying, because the platform will refuse the paste and not explain why.
    over_limit: bool
    #: ``None`` when no spec covers this channel. Not defaulted to ``True``: telling
    #: somebody a link works when we do not know is how a CTA becomes plain text.
    link_in_body: bool | None
    link_mechanism: LinkMechanism
    #: What this channel will cost the poster, one honest sentence each. Rendered by the
    #: screen AND by the downloaded file, so the wording cannot drift between them.
    notes: tuple[str, ...]


class ExportProofPoint(_Wire):
    """One reason to believe the offer, and the source it came from.

    ``source`` travels with the text and is never dropped. It is required by
    ``engines.landing``'s contract for the reason that applies twice as hard on an export
    the customer will paste under their own name: an unsourced proof point is an invented
    claim about their business.
    """

    text: str
    source: str


class ExportCta(_Wire):
    """The ask, written for one channel."""

    channel: str
    text: str


class ExportDistribution(_Wire):
    """Where the clicks are meant to land, as CONVERT chose it.

    It replaced `ExportLandingPage` when the founder ruled that we host no page
    (`CLAUDE.md`, 2026-08-21): there is no headline, offer, form or consent line to
    export, because there is no page — only the address on the business's OWN site and
    the ask that earns the click.
    """

    destination_url: str
    channel_ctas: tuple[ExportCta, ...]


class ExportTrackedLink(_Wire):
    """One channel's tracked short link, as actually minted.

    Every field here is read back from what the landing actuator reported doing — there is
    no code path that constructs a `code` from anything but a `short_links` row that
    already exists. That is the whole prohibition this type has to keep: a
    plausible-looking `/l/xxxxxxxx` in a pack is a dead link in somebody's Instagram bio,
    and the failure is invisible because it looks exactly like a working one.
    """

    channel: str
    #: The ask that goes with the link, so the paste is the whole post.
    text: str
    code: str
    url: str


class ExportPack(_Wire):
    """Everything Tier 3 needs, and every gap in it named.

    ``notice`` is on the wire rather than left to the client on purpose: "this pack sends
    nothing to any platform" is the one claim this payload must carry into every surface
    that renders it, including the plain-text file, and a sentence each client writes for
    itself is a sentence one client can forget.
    """

    has_pack: bool
    notice: str
    channels: tuple[ExportChannel, ...]
    channels_note: str | None
    distribution: ExportDistribution | None
    distribution_note: str | None
    ai_blocks: AiBlocks | None
    ai_blocks_note: str | None
    #: The bio-link hub for this business. Real and permanent (``GET /go/{id}``), which is
    #: why it is here at all: it is the only TRACKED address this pack can honestly offer,
    #: and the only route at all on the channels that cannot carry a link.
    hub_url: str | None
    hub_note: str | None
    #: The public address of the page this run PUBLISHED, when it published one. An
    #: address to paste, which is what this pack is for — not a publish claim, which the
    #: class note below correctly keeps out.
    published_page_url: str | None
    #: One per channel CTA, minted by the landing actuator. Empty until a run publishes.
    tracked_links: tuple[ExportTrackedLink, ...]
    #: Why there is no tracked link — and `None` once there are some, because a note
    #: explaining an absence that is not there is just a contradiction on the screen.
    tracked_link_note: str | None
    fact_gaps: tuple[str, ...]
    errors: tuple[dict[str, str], ...]
    # Deliberately NOT carrying `published`/`measurement`. The pack is the thing a human
    # pastes into a composer; what EXPORT did with it belongs on the review screen, and
    # putting publish status here would invite a client to render a publish claim on a
    # surface whose whole point is that it publishes nothing. See `RunReview`.


def _missing_hashtags(body: str, hashtags: Sequence[str]) -> tuple[str, ...]:
    """The declared tags the body does not already carry, in the order declared.

    Matched on a word boundary rather than by substring, because ``#notar`` is a
    substring of ``#notarkoblenz``: a substring test would report a tag as present when
    it is not, and the paste would go out one hashtag short of what the screen showed.
    """
    return tuple(
        tag
        for tag in hashtags
        if not re.search(rf"{re.escape(tag)}(?!\w)", body, flags=re.IGNORECASE)
    )


def _channel_notes(
    post: _StoredPost, appended: Sequence[str], paste_length: int
) -> tuple[str, ...]:
    """What this channel will cost the poster. Empty when it costs nothing.

    Every sentence is arithmetic over the text in hand or a fact from the spec table.
    Nothing here is advice a model produced, and nothing is softened: an over-limit post
    is refused by the platform, and the sentence says so.
    """
    spec = post.spec
    notes: list[str] = []

    if spec is None:
        notes.append(
            f"No channel spec covers {post.channel}, so no length limit, hashtag cap or "
            "link behaviour is claimed for it. Check the platform's own rules before posting."
        )
    elif not spec.link_in_body:
        notes.append(
            f"A link in the body does not work on this channel — on {post.channel} a URL in "
            "the body is plain text, not a clickable link. Use the bio hub instead."
        )

    if spec is not None and paste_length > spec.hard_max_chars:
        notes.append(
            f"{paste_length - spec.hard_max_chars:,} characters over this channel's "
            f"{spec.hard_max_chars:,}-character platform limit — it will be refused as it "
            "stands. Shorten it before pasting."
        )
    elif spec is not None and paste_length > spec.max_chars:
        notes.append(
            f"{paste_length - spec.max_chars:,} characters over this channel's "
            f"{spec.max_chars:,}-character editorial target, inside the platform limit — "
            "publishable, and longer than it should be."
        )

    if appended:
        notes.append(
            f"{len(appended)} hashtag{'' if len(appended) == 1 else 's'} "
            f"({' '.join(appended)}) were listed for this post but not written into it, so "
            "they are appended at the end of the block above."
        )
    if post.hashtags_removed > 0:
        notes.append(
            f"{post.hashtags_removed} hashtag{'' if post.hashtags_removed == 1 else 's'} "
            "were removed in code to stay inside this channel's cap."
        )
    if post.hashtags_shortfall > 0:
        notes.append(
            f"{post.hashtags_shortfall} short of this channel's hashtag minimum — none "
            "were invented to fill the gap, so add your own if you want them."
        )
    return tuple(notes)


def _export_channels(checkpoint: Mapping[str, Any]) -> tuple[ExportChannel, ...]:
    """One paste-ready block per rendered channel, in the stored order.

    The order is the same arbitrary-but-deterministic order :func:`_social` documents,
    and it is left alone here for the same reason: a sort invented in a projection reads
    as a channel priority the product has not decided.
    """
    out: list[ExportChannel] = []
    for post in _stored_posts(checkpoint):
        spec = post.spec
        appended = _missing_hashtags(post.body, post.hashtags)
        paste = f"{post.body}\n\n{' '.join(appended)}" if appended else post.body
        mechanism: LinkMechanism = (
            "unknown" if spec is None else ("inline" if spec.link_in_body else "bio_hub")
        )
        out.append(
            ExportChannel(
                channel=post.channel,
                body=post.body,
                paste_text=paste,
                hashtags=post.hashtags,
                appended_hashtags=appended,
                body_characters=len(post.body),
                paste_characters=len(paste),
                character_target=spec.max_chars if spec else None,
                character_limit=spec.hard_max_chars if spec else None,
                hashtag_count=len(post.hashtags),
                hashtag_minimum=spec.hashtags_min if spec else None,
                hashtag_limit=spec.hashtags_max if spec else None,
                hashtags_removed=post.hashtags_removed,
                hashtags_shortfall=post.hashtags_shortfall,
                # Recomputed against the paste rather than trusting the stored flag:
                # REPACK measured the body, and the appended hashtags are part of what
                # gets posted. The stored flag stays authoritative on the review screen,
                # which is showing the body.
                over_target=bool(spec and len(paste) > spec.max_chars),
                over_limit=bool(spec and len(paste) > spec.hard_max_chars),
                link_in_body=spec.link_in_body if spec else None,
                link_mechanism=mechanism,
                notes=_channel_notes(post, appended, len(paste)),
            )
        )
    return tuple(out)


def _distribution(checkpoint: Mapping[str, Any]) -> ExportDistribution | None:
    """What CONVERT chose, read defensively.

    Never `model_validate`: a checkpoint written by an earlier version of the state, or
    half-written by a run that died, would raise — and a pack that 500s is a pack nobody
    can export. A plan with no destination is treated as absent rather than reported as
    an empty one.
    """
    raw = _mapping(checkpoint.get("distribution"))
    destination = _text(raw.get("destination_url")).strip()
    if not destination:
        return None

    ctas: list[ExportCta] = []
    stored = raw.get("ctas")
    if isinstance(stored, Sequence) and not isinstance(stored, str | bytes):
        for item in stored:
            entry = _mapping(item)
            channel = _optional_text(entry.get("channel"))
            text = _optional_text(entry.get("text"))
            if channel is None or text is None:
                continue
            ctas.append(ExportCta(channel=channel, text=text))

    return ExportDistribution(destination_url=destination, channel_ctas=tuple(ctas))


def _published_addresses(
    checkpoint: Mapping[str, Any],
) -> tuple[str | None, tuple[ExportTrackedLink, ...]]:
    """The page URL and tracked links a run actually published, or nothing.

    Read out of the `publish.page` outcome's own `detail`, which is the only source that
    can honestly supply them: the landing actuator wrote the `content_pieces` row and
    minted each `short_links` row in the same call, so a code appearing here is a code
    that exists. Nothing is reconstructed, and nothing is inferred from the presence of a
    landing-page SPEC — a spec means CONVERT wrote a page, not that anyone published it.

    Only a `succeeded` outcome counts, and `fake` is refused outright. A simulated publish
    carries a `fake://` reference, and putting that in a pack a human pastes is precisely
    the invisible failure the tracked-link note was written to avoid.
    """
    published = checkpoint.get("published")
    if not isinstance(published, Mapping):
        return None, ()

    refs = published.get("refs")
    if not isinstance(refs, Sequence):
        return None, ()

    for row in refs:
        if not isinstance(row, Mapping):
            continue
        if row.get("action_type") != "publish.page" or row.get("status") != "succeeded":
            continue
        if row.get("fake"):
            # Simulated. It has no address, and inventing one is the whole hazard.
            continue

        detail = row.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        url = _text(row.get("external_ref")) or None

        links: list[ExportTrackedLink] = []
        raw = detail.get("ctas")
        for cta in raw if isinstance(raw, Sequence) else ():
            if not isinstance(cta, Mapping):
                continue
            code = _text(cta.get("code"))
            link_url = _text(cta.get("url"))
            channel = _text(cta.get("channel"))
            # All three or none: a link with no code cannot be attributed, and a link
            # with no URL is not a link. Dropping the row is better than half of one.
            if not (code and link_url and channel):
                continue
            links.append(
                ExportTrackedLink(
                    channel=channel,
                    text=_text(cta.get("text")),
                    code=code,
                    url=link_url,
                )
            )
        return url, tuple(links)

    return None, ()


def project_export_pack(
    checkpoint: Mapping[str, Any] | None,
    *,
    hub_url: str | None = None,
) -> ExportPack:
    """Project one run's checkpoint into the Tier 3 export pack.

    Pure, like :func:`project_review`, and for the same reason — the whole pack contract
    is assertable without a database, and the route stays a projection call plus a
    header.

    ``hub_url`` is a PARAMETER rather than something read from settings here, which keeps
    the purity and mirrors ``landing_service``'s rule that an absolute URL comes from
    configuration and never from a request: the caller holds both the configured base URL
    and the authenticated business id, and this function holds neither.
    """
    if not checkpoint:
        return ExportPack(
            has_pack=False,
            notice=_NOTHING_PUBLISHED,
            channels=(),
            channels_note=_NO_CHECKPOINT,
            distribution=None,
            distribution_note=_NO_CHECKPOINT,
            ai_blocks=None,
            ai_blocks_note=_NO_CHECKPOINT,
            hub_url=hub_url,
            hub_note=_HUB_NOTE if hub_url else None,
            published_page_url=None,
            tracked_links=(),
            tracked_link_note=_NO_TRACKED_LINK,
            fact_gaps=(),
            errors=(),
        )

    channels = _export_channels(checkpoint)
    distribution = _distribution(checkpoint)
    ai_blocks, ai_note = _ai_blocks(checkpoint)
    page_url, tracked = _published_addresses(checkpoint)

    return ExportPack(
        has_pack=bool(channels or distribution or (ai_blocks and ai_blocks.blocks)),
        notice=_NOTHING_PUBLISHED,
        channels=channels,
        channels_note=None if channels else _NO_SOCIAL,
        distribution=distribution,
        distribution_note=None if distribution else _NO_LANDING,
        ai_blocks=ai_blocks,
        ai_blocks_note=ai_note,
        hub_url=hub_url,
        hub_note=_HUB_NOTE if hub_url else None,
        published_page_url=page_url,
        tracked_links=tracked,
        # The note explains an absence, so it goes the moment there is nothing absent.
        tracked_link_note=None if tracked else _NO_TRACKED_LINK,
        fact_gaps=_strings(checkpoint.get("fact_gaps")),
        errors=_errors(checkpoint),
    )


# --------------------------------------------------------------------------- #
# The same pack as text, because the whole point of Tier 3 is that a human pastes it
# --------------------------------------------------------------------------- #


def _fence(text: str) -> str:
    """A code fence long enough to hold ``text``.

    Not a fixed ```````: a post containing three backticks would close the fence early
    and the rest of it would be read as prose, so the paste would be silently truncated
    at exactly the character that caused it. CommonMark allows a longer fence, so the
    fence is measured against the content.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _block(text: str) -> str:
    fence = _fence(text)
    return f"{fence}text\n{text}\n{fence}"


def render_export_markdown(pack: ExportPack) -> str:
    """The pack as Markdown: readable as plain text, pasteable block by block.

    Markdown rather than JSON for the download because the deliverable is for a PERSON —
    docs/CHANNELS.md section 2 is explicit that Tier 3's value is a restaurant owner
    posting the draft in ten seconds — and a person cannot paste a JSON string that has
    had its newlines escaped.

    Pure, and derived entirely from ``pack``: the file and the screen therefore say the
    same thing about the same run, including the notes and the absence notices.
    """
    lines: list[str] = ["# Export pack", "", pack.notice, ""]

    if not pack.has_pack:
        lines += ["This run produced nothing to export yet.", ""]

    lines += ["## Channels", ""]
    if pack.channels:
        for channel in pack.channels:
            lines += [f"### {channel.channel}", ""]
            counts = f"{channel.paste_characters:,} characters"
            if channel.paste_characters != channel.body_characters:
                counts += f" ({channel.body_characters:,} before the appended hashtags)"
            if channel.character_target is not None:
                counts += f" · target {channel.character_target:,}"
            if channel.character_limit is not None:
                counts += f" · platform limit {channel.character_limit:,}"
            lines.append(counts)

            tags = f"{channel.hashtag_count} hashtag{'' if channel.hashtag_count == 1 else 's'}"
            if channel.hashtag_limit is not None:
                tags += f" · at most {channel.hashtag_limit}"
            if channel.hashtag_minimum:
                tags += f" · at least {channel.hashtag_minimum}"
            lines.append(tags)

            lines.append(
                "Link: put it in the body — it is clickable here."
                if channel.link_mechanism == "inline"
                else (
                    "Link: use the bio hub. A URL in the body is not clickable on this channel."
                    if channel.link_mechanism == "bio_hub"
                    else "Link: unknown for this channel — check the platform's own rules."
                )
            )
            lines.append("")
            if channel.notes:
                lines += [f"- {note}" for note in channel.notes] + [""]
            lines += [_block(channel.paste_text), ""]
    else:
        lines += [pack.channels_note or "No channel copy was rendered.", ""]

    lines += ["## Where the clicks go", ""]
    plan = pack.distribution
    if plan is None:
        lines += [pack.distribution_note or "No destination was chosen.", ""]
    else:
        # The destination is on the business's OWN site, so it is stated plainly rather
        # than described: it is a URL the owner recognises and can check.
        lines += [f"Destination: {plan.destination_url}", ""]
        if plan.channel_ctas:
            lines += ["The ask, per channel:", ""]
            lines += [f"- {cta.channel}: {cta.text}" for cta in plan.channel_ctas]
            lines.append("")

    lines += ["## Answer blocks for AI engines", ""]
    blocks = pack.ai_blocks
    if blocks is None or not blocks.blocks:
        lines += [pack.ai_blocks_note or "No answer blocks were produced.", ""]
    else:
        if blocks.target_keyword:
            lines += [f"Written for: {blocks.target_keyword}", ""]
        lines += [f"{i}. {block}" for i, block in enumerate(blocks.blocks, start=1)]
        lines.append("")

    lines += ["## Links", ""]
    # The published page first: every tracked link below points at it, so a reader who
    # pastes one wants to know where it lands.
    if pack.published_page_url:
        lines += [f"Published page: {pack.published_page_url}", ""]
    if pack.hub_url:
        lines += [f"Bio-link hub: {pack.hub_url}", ""]
        if pack.hub_note:
            lines += [pack.hub_note, ""]
    if pack.tracked_links:
        lines += ["Tracked links — one per channel, so each channel's clicks are its own:", ""]
        lines += [
            f"- {link.channel}: {link.url}" + (f" — {link.text}" if link.text else "")
            for link in pack.tracked_links
        ]
        lines.append("")
    if pack.tracked_link_note:
        lines += [pack.tracked_link_note, ""]

    if pack.fact_gaps or pack.errors:
        lines += ["## What this was written without", ""]
        lines += [f"- {gap}" for gap in pack.fact_gaps]
        lines += [f"- {error['node']}: {error['message']}" for error in pack.errors]
        lines.append("")

    # One trailing newline, not several: this is a file somebody opens in an editor.
    return "\n".join(lines).rstrip() + "\n"
