"""Feedback, and how it becomes a *proposed* change to business memory.

The wording matters and is fixed by docs/CRITERIA_MAP.md section 7: **the agent
updates persistent business preferences from explicit feedback.** Not "the agent
learns", not "the model retrains itself". Nothing here modifies a model, a weight
or a fine-tune; it writes rows a human can read, edit and revoke. Saying otherwise
would be a claim the code cannot support.

Three rules shape this module.

**Four axes, not one score.** "Bad" is not actionable. ``on_brand 2, accuracy 5``
is: it says the voice is wrong and the facts are right, which points at the brand
block of the prompt and not at retrieval. Each axis is 1-5, and a value outside
that range is refused rather than clamped -- a clamped 9 would silently become a 5
and read as praise.

**One rejection is an opinion; three is a pattern.** :func:`distil` proposes a
rule only when the same theme recurs at least ``min_occurrences`` times. Proposing
from a single complaint would let one bad day rewrite a brand's voice, and the
owner would carry that rule for months.

**A proposal is never applied until it is approved.** :func:`distil` writes to
``learned_style`` with ``status="proposed"`` and touches ``businesses.dna``
never. Only :func:`approve_proposal` calls ``memory_service.remember``. That is
the consent boundary, and it is the difference between a memory the owner controls
and drift they cannot explain.

**Grouping is deterministic, and that is a deliberate choice.** The obvious
implementation asks a model to cluster free-text complaints. The project rule is
"if the answer is computable, compute it" (CLAUDE.md), and this is computable: a
table of themes with the terms that indicate each, plus exact-repeat grouping for
anything unmatched. It costs nothing, it cannot hallucinate a theme nobody
complained about, and the same three rejections always produce the same proposal --
which is what makes the feedback loop demonstrable rather than anecdotal. The
trade-off is real and worth naming: a complaint phrased in words no theme lists
falls through to exact-repeat grouping, so it needs three *identically phrased*
rejections rather than three similar ones. That is the honest failure direction --
it proposes less, never something invented.

Transactions belong to the caller: these functions ``flush`` and never ``commit``,
so recording feedback, distilling, and applying an approved rule can each be one
atomic unit with whatever the route does around them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import ContentPiece, Feedback, LearnedStyle
from backend.app.services import memory_service

__all__ = [
    "AXES",
    "AXIS_MAX",
    "AXIS_MIN",
    "DEFAULT_MIN_OCCURRENCES",
    "VERDICTS",
    "ContentPieceNotFoundError",
    "FeedbackServiceError",
    "InvalidAxesError",
    "InvalidVerdictError",
    "Proposal",
    "ProposalNotFoundError",
    "approve_proposal",
    "distil",
    "list_proposals",
    "record",
    "themes_in",
]

#: The rubric. Four axes, because a single thumbs-down does not say which part of
#: the prompt to change (docs/BUILD_ORDER.md Phase 13).
AXES: Final[tuple[str, ...]] = ("on_brand", "accuracy", "seo", "usefulness")

#: Inclusive bounds per axis. A 1-5 scale rather than 1-10: people do not agree
#: on what 7 means, and the four axes already carry the detail a wider scale would
#: pretend to.
AXIS_MIN: Final = 1
AXIS_MAX: Final = 5

VERDICTS: Final[tuple[str, ...]] = ("approved", "rejected")

#: One rejection is an opinion, three is a pattern. See the module docstring.
DEFAULT_MIN_OCCURRENCES: Final = 3

#: How many source reasons are stored on a proposal as evidence. The owner needs
#: enough to recognise the pattern; the whole history would be a transcript.
MAX_DERIVED_FROM: Final = 10

#: How much of one reason is kept as evidence.
MAX_REASON_LENGTH: Final = 300


class FeedbackServiceError(Exception):
    """Base class for every refusal this module raises."""


class InvalidVerdictError(FeedbackServiceError):
    """The verdict is not one of :data:`VERDICTS`."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        super().__init__(f"Verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}.")


class InvalidAxesError(FeedbackServiceError):
    """An axis name is unknown, or a rating is not an integer in range.

    One error type for all three, because the caller's response is the same 422 and
    the message names which axis and why.
    """


class ContentPieceNotFoundError(FeedbackServiceError):
    """No such piece **in this business**.

    Deliberately the same error for "does not exist" and "belongs to someone
    else": the route answers both with 404, because whether a piece exists is
    itself information the caller has no legitimate way to hold.
    """

    def __init__(self, content_piece_id: UUID, business_id: UUID) -> None:
        self.content_piece_id = content_piece_id
        self.business_id = business_id
        super().__init__(f"No content piece {content_piece_id} in business {business_id}.")


class ProposalNotFoundError(FeedbackServiceError):
    """No such proposed rule in this business."""

    def __init__(self, proposal_id: UUID, business_id: UUID) -> None:
        self.proposal_id = proposal_id
        self.business_id = business_id
        super().__init__(f"No proposal {proposal_id} in business {business_id}.")


class Proposal(BaseModel):
    """A rule awaiting the owner's approval, as the UI renders it.

    ``derived_from`` is not decoration: it is the diff's evidence. "We are
    proposing this because you said these three things" is what makes the proposal
    reviewable instead of an assertion.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    rule: str
    derived_from: tuple[str, ...]
    status: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def _validate_verdict(verdict: str) -> str:
    if verdict not in VERDICTS:
        raise InvalidVerdictError(verdict)
    return verdict


def _validate_axes(axes: Mapping[str, Any]) -> dict[str, int]:
    """Reject unknown axes and out-of-range ratings; allow a partial rubric.

    A partial rubric is legitimate -- someone marking "the voice is wrong" should
    not have to invent an SEO score to say so -- but an axis nobody defined is a
    typo, and storing it would put a key in the JSONB that no aggregate ever reads.

    ``bool`` is refused explicitly: ``True`` is an ``int`` in Python and would
    otherwise be stored as the rating 1, turning a checkbox into the worst possible
    score.
    """
    clean: dict[str, int] = {}
    for name, value in axes.items():
        if name not in AXES:
            raise InvalidAxesError(
                f"Unknown feedback axis {name!r}. The rubric is: {', '.join(AXES)}."
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidAxesError(
                f"Axis {name!r} must be a whole number from {AXIS_MIN} to {AXIS_MAX}; "
                f"got {value!r}."
            )
        if not AXIS_MIN <= value <= AXIS_MAX:
            raise InvalidAxesError(
                f"Axis {name!r} must be between {AXIS_MIN} and {AXIS_MAX}; got {value}. "
                "Out-of-range ratings are refused rather than clamped: a clamped 9 "
                f"would become a {AXIS_MAX} and read as praise."
            )
        clean[name] = value
    return clean


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    collapsed = " ".join(reason.split())
    return collapsed[:MAX_REASON_LENGTH] or None


async def record(
    content_piece_id: UUID,
    business_id: UUID,
    *,
    verdict: str,
    axes: Mapping[str, Any] | None = None,
    reject_reason: str | None = None,
    user_id: UUID | None = None,
    session: AsyncSession,
) -> None:
    """Store one rating against one produced piece.

    Validation runs BEFORE the database is touched, in that order on purpose: a
    malformed rubric is the caller's problem and must not cost a query, and a
    route that returns 422 for it should never have opened a transaction.

    A rejection with no reason is accepted. It contributes nothing to
    :func:`distil`, but refusing it would also throw away the axis scores, and the
    axes are the part that tells us which half of the prompt was wrong.
    """
    checked_verdict = _validate_verdict(verdict)
    checked_axes = _validate_axes(axes or {})
    reason = _clean_reason(reject_reason)

    piece = (
        await session.execute(
            select(ContentPiece.id).where(
                ContentPiece.id == content_piece_id,
                # Explicit, even though row-level security already scopes the
                # session. Belt AND braces: this predicate is what makes the
                # refusal true for a migration, a script, or any caller that runs
                # as the table owner, where RLS does not apply.
                ContentPiece.business_id == business_id,
            )
        )
    ).scalar_one_or_none()
    if piece is None:
        raise ContentPieceNotFoundError(content_piece_id, business_id)

    session.add(
        Feedback(
            business_id=business_id,
            content_piece_id=content_piece_id,
            user_id=user_id,
            verdict=checked_verdict,
            axes=checked_axes,
            reject_reason=reason,
        )
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# Distilling: reject reasons -> proposed rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Theme:
    """One recognised style complaint and the rule it would propose.

    ``terms`` are matched case-insensitively as substrings of the reason. German
    and English both appear because the product is German-first and its operators
    write notes in either.
    """

    key: str
    rule: str
    terms: tuple[str, ...]


#: The theme table. Adding a theme is one entry, and the rule it proposes is
#: written out here rather than generated, so what the owner will be asked to
#: approve is reviewable in the diff that adds it.
THEMES: Final[tuple[_Theme, ...]] = (
    _Theme(
        key="exclamation",
        rule="Never use exclamation marks.",
        terms=("exclamation", "!!", "ausrufezeichen"),
    ),
    _Theme(
        key="emoji",
        rule="Do not use emoji.",
        terms=("emoji", "smiley", "emojis"),
    ),
    _Theme(
        key="hype",
        rule="Avoid hype and superlatives; state only what the business can evidence.",
        terms=(
            "salesy",
            "sales-y",
            "hype",
            "hyperbole",
            "superlative",
            "marketing speak",
            "werblich",
            "reisserisch",
            "reißerisch",
            "uebertrieben",
            "übertrieben",
        ),
    ),
    _Theme(
        key="length",
        rule="Keep it short: no filler, no repetition, no restating the heading.",
        terms=("too long", "verbose", "wordy", "waffle", "padding", "zu lang", "geschwätzig"),
    ),
    _Theme(
        key="jargon",
        rule="Write in plain language; no jargon and no buzzwords.",
        terms=("jargon", "buzzword", "corporate speak", "fachjargon", "floskel"),
    ),
    _Theme(
        key="formality",
        rule="Address the reader formally (Sie), never informally (du).",
        terms=("too casual", "too informal", "duzen", "geduzt", "zu locker", "siezen"),
    ),
    _Theme(
        key="unsupported",
        rule=(
            "Every factual claim must be traceable to the business's own documents; "
            "omit anything the evidence does not support."
        ),
        terms=(
            "made up",
            "made-up",
            "invented",
            "unsupported",
            "not true",
            "untrue",
            "wrong price",
            "erfunden",
            "stimmt nicht",
            "falsche angabe",
        ),
    ),
    _Theme(
        key="stuffing",
        rule="Do not repeat the target keyword unnaturally.",
        terms=("keyword stuffing", "keyword-stuffing", "stuffed", "keyword spam"),
    ),
    _Theme(
        key="language",
        rule="Write in German.",
        terms=("in english", "englisch", "wrong language", "falsche sprache"),
    ),
)


def themes_in(reason: str) -> tuple[str, ...]:
    """Which themes one reject reason matches, if any.

    Pure and exported so the matcher is assertable without a database. A reason may
    match several -- "far too long and full of exclamation marks" is genuinely two
    complaints, and counting it once would make a real pattern take six rejections
    to surface instead of three.
    """
    haystack = reason.casefold()
    return tuple(theme.key for theme in THEMES if any(term in haystack for term in theme.terms))


_THEMES_BY_KEY: Final[Mapping[str, _Theme]] = {theme.key: theme for theme in THEMES}


def _reason_key(reason: str) -> str:
    """Identity of an unmatched reason, for exact-repeat grouping."""
    return " ".join(reason.split()).casefold()


@dataclass
class _Candidate:
    """A rule with the reasons that produced it, accumulated during a distil."""

    rule: str
    reasons: list[str]


def _candidates(reasons: Sequence[str], *, min_occurrences: int) -> list[_Candidate]:
    """Group reject reasons and keep only those that recur enough. Pure.

    Two passes over the same reasons, and the second one is not redundant: a
    complaint whose wording no theme recognises still forms a pattern when it is
    made repeatedly, and dropping it would mean the loop only ever learns the nine
    things this file already knows about.
    """
    by_theme: dict[str, _Candidate] = {}
    unmatched: dict[str, _Candidate] = {}

    for reason in reasons:
        matched = themes_in(reason)
        if matched:
            for key in matched:
                theme = _THEMES_BY_KEY[key]
                by_theme.setdefault(key, _Candidate(theme.rule, [])).reasons.append(reason)
            continue
        key = _reason_key(reason)
        # The owner's own wording is proposed verbatim. It is their sentence, they
        # approve it before it takes effect, and rewriting it here would be this
        # module inventing a rule from a complaint.
        unmatched.setdefault(key, _Candidate(" ".join(reason.split()), [])).reasons.append(reason)

    return [
        candidate
        for candidate in (*by_theme.values(), *unmatched.values())
        if len(candidate.reasons) >= min_occurrences
    ]


async def distil(
    business_id: UUID,
    *,
    session: AsyncSession,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
) -> list[str]:
    """Turn recurring reject reasons into PROPOSED business rules.

    Returns the rules newly proposed by this call -- an empty list is the normal
    answer, and the honest one, for a business whose rejections have no pattern
    yet.

    Nothing here touches ``businesses.dna``. This function proposes; the owner
    approves; :func:`approve_proposal` applies. That ordering is the whole consent
    guarantee, and it is asserted by a test that reads ``dna`` before approval.

    Three things are deliberately NOT re-proposed:

    * a rule already in force (it is in ``dna.preferences``);
    * a rule already sitting as a proposal (it would appear twice in the panel);
    * a rule the owner has REJECTED. That last one matters most -- without it, a
      declined proposal would come back on every run, which is nagging, and a
      product that nags gets its notifications switched off.
    """
    if min_occurrences < 1:
        raise ValueError("min_occurrences must be at least 1")

    rows = (
        await session.execute(
            select(Feedback.reject_reason)
            .where(
                Feedback.business_id == business_id,
                Feedback.verdict == "rejected",
                Feedback.reject_reason.is_not(None),
            )
            .order_by(Feedback.created_at)
        )
    ).scalars()
    reasons = [reason for reason in rows if reason and reason.strip()]

    candidates = _candidates(reasons, min_occurrences=min_occurrences)
    if not candidates:
        return []

    memory = await memory_service.load_memory(business_id, session=session)
    existing_rules = (
        await session.execute(
            select(LearnedStyle.rule).where(LearnedStyle.business_id == business_id)
        )
    ).scalars()
    # One notion of "the same rule" for memory and for proposals. Two would
    # propose duplicates forever.
    taken = memory_service.known_keys(memory.preferences) | memory_service.known_keys(
        list(existing_rules)
    )

    proposed: list[str] = []
    for candidate in candidates:
        if memory_service.rule_key(candidate.rule) in taken:
            continue
        session.add(
            LearnedStyle(
                business_id=business_id,
                rule=candidate.rule,
                derived_from=candidate.reasons[:MAX_DERIVED_FROM],
                status="proposed",
            )
        )
        taken.add(memory_service.rule_key(candidate.rule))
        proposed.append(candidate.rule)

    if proposed:
        await session.flush()
    return proposed


# --------------------------------------------------------------------------- #
# Approval: the only path from a proposal into memory
# --------------------------------------------------------------------------- #


async def list_proposals(
    business_id: UUID,
    *,
    session: AsyncSession,
    status: str | None = "proposed",
) -> list[Proposal]:
    """The rules awaiting approval, oldest first.

    ``status=None`` returns every status, which is what an audit view wants:
    "what did we propose and what did you do about it" is a more useful question
    than "what is pending".
    """
    statement = select(LearnedStyle).where(LearnedStyle.business_id == business_id)
    if status is not None:
        statement = statement.where(LearnedStyle.status == status)
    rows = (await session.execute(statement.order_by(LearnedStyle.created_at))).scalars()
    return [
        Proposal(
            id=row.id,
            rule=row.rule,
            derived_from=tuple(str(item) for item in (row.derived_from or [])),
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def approve_proposal(
    proposal_id: UUID,
    business_id: UUID,
    *,
    session: AsyncSession,
) -> str:
    """Approve one proposed rule and apply it to business memory.

    Returns the rule that is now in force.

    Approving is what applies it -- the two happen here, in one transaction, so
    there is no window in which a rule is approved but not in force (or worse, in
    force but not approved). Idempotent: approving an already-approved rule
    re-applies nothing, because ``memory_service.remember`` deduplicates.
    """
    proposal = (
        await session.execute(
            select(LearnedStyle).where(
                LearnedStyle.id == proposal_id,
                # Explicit business predicate for the same reason as in `record`.
                LearnedStyle.business_id == business_id,
            )
        )
    ).scalar_one_or_none()
    if proposal is None or proposal.status == "rejected":
        # A rejected proposal is not approvable by resubmitting its id: the owner
        # has already answered. It reads as "not found" rather than as a distinct
        # error, so a stale UI cannot reverse a decision by retrying.
        raise ProposalNotFoundError(proposal_id, business_id)

    proposal.status = "approved"
    proposal.approved_at = datetime.now(UTC)
    await memory_service.remember(business_id, rule=proposal.rule, session=session)
    await session.flush()
    return proposal.rule
