"""What the review screen shows: one run's output, projected out of its checkpoint.

The review surface has four tabs — the blog draft, the deterministic SEO findings, the
social posts per channel, and the "AI blocks" an answer engine can quote. All four are
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
an outage.

One thing deliberately NOT returned: a per-channel character LIMIT. Two limit tables
already disagree in this repo (``agents.nodes.CHANNEL_LIMITS`` against
``evals.rubric.CHANNEL_LIMITS`` — see the open Phase 11 backlog item, which predicted
exactly this), so publishing one of them on the wire would make this module a third copy
and the review screen would eventually contradict the rubric that grades it. The measured
character count IS returned, because that is arithmetic over the text in hand and cannot
drift. REPACK has already enforced its ceiling by the time a rendering is stored.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

__all__ = [
    "AiBlocks",
    "Draft",
    "Opportunity",
    "ReviewFinding",
    "RunReview",
    "SeoReport",
    "SocialPost",
    "project_review",
]


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
    is the count the server measured. See the module docstring for why no limit ships.
    """

    channel: str
    body: str
    characters: int


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


class Opportunity(_Wire):
    """Why this topic and not another. Context for the draft tab, not a tab of its own."""

    title: str
    rationale: str | None
    score: int | None


class RunReview(_Wire):
    """Everything the four tabs need, plus the honesty carried alongside them.

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
    fact_gaps: tuple[str, ...]
    errors: tuple[dict[str, str], ...]


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
    raw = checkpoint.get("renderings")
    if not isinstance(raw, Mapping):
        return ()
    posts: list[SocialPost] = []
    for channel, body in raw.items():
        name = _text(channel).strip()
        text = _text(body).strip()
        if not name or not text:
            continue
        posts.append(SocialPost(channel=name, body=text, characters=len(text)))
    return tuple(posts)


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
            fact_gaps=(),
            errors=(),
        )

    draft = _draft(checkpoint)
    seo = _seo(checkpoint)
    social = _social(checkpoint)
    ai_blocks, ai_note = _ai_blocks(checkpoint)

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
        fact_gaps=_strings(checkpoint.get("fact_gaps")),
        errors=_errors(checkpoint),
    )
