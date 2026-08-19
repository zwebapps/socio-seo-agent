"""Business memory: the store that makes run 2 obey what was said in run 1.

There are **three** memory stores in this product and they are not the same thing
(docs/AGENT_RUNTIME.md section 6). Stating the distinction plainly, because
conflating them is the single most common way an "agent with memory" turns out to
have none:

===================  ==========================================  =============
Store                Holds                                       Lifetime
===================  ==========================================  =============
**Working state**    the current ``AgentState``                   one run
**Business memory**  voice, audience, banned claims, preferences  permanent
**Episodic**         past approved pieces, past rejections        permanent
===================  ==========================================  =============

*This module owns the middle row, and only the middle row.*

**Run checkpointing is not memory.** The LangGraph checkpoint in ``runs`` lets a
crashed or paused run resume exactly where it stopped -- it is a resume point and
an audit record. It is scoped to one run, and a second run starts with an empty
one. Nothing a checkpoint holds can change how tomorrow's run writes. So a system
with checkpointing and nothing else cannot obey "never use exclamation marks"
next week, however good its resume story is.

Business memory is the opposite shape: it holds a handful of durable statements,
it is read once at ``INTAKE``, and it is carried in the *system prompt* of every
agent node from there (:func:`backend.app.agents.nodes.prompts.system` renders
``remembered``). That is what "the next run obeys it without being told again"
actually means mechanically.

Two rules in here are load-bearing, and both exist to protect the customer's
brand rather than to make the code neat.

**A preference is never inferred and silently applied.** Only an explicit
:func:`remember` call writes to ``businesses.dna``. Candidates distilled from
feedback go to ``learned_style`` with ``status="proposed"`` and reach memory only
through an approval (see ``feedback_service.approve_proposal``). An agent that
quietly rewrote a brand's voice from a few rejections would change the product's
voice without consent, and the owner would have no way to see why the output
drifted -- they would only notice, weeks later, that it no longer sounded like
them.

**Appends deduplicate, case- and whitespace-insensitively.** A preference stated
twice must not appear twice in a prompt: a duplicated instruction reads as
emphasis, and emphasis on an arbitrary rule is a behaviour change nobody asked
for. ``"Never use exclamation marks"`` and ``"never use  exclamation marks"`` are
the same rule, so the second one is a no-op rather than a second line.

Persistence is a plain ``AsyncSession`` rather than a port, because there is
nothing to abstract: this is two columns of one row. The transaction boundary
belongs to the CALLER -- these functions ``flush`` and never ``commit``, so a
route can wrap ``remember`` and an approval write in one transaction through
``backend.app.db.session.business_session``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Business

__all__ = [
    "MAX_PREFERENCES",
    "MAX_RULE_LENGTH",
    "BusinessMemory",
    "BusinessNotFoundError",
    "DuplicatePreferenceError",
    "EmptyRuleError",
    "MemoryServiceError",
    "PreferenceLimitError",
    "PreferenceNotFoundError",
    "RuleTooLongError",
    "forget",
    "known_keys",
    "load_memory",
    "memory_from_dna",
    "normalise_rule",
    "remember",
    "revise",
    "rule_key",
    "to_prompt_lines",
]

#: Ceiling on confirmed preferences. Every one of these is prepended to EVERY
#: model call in a run, so the list is a per-call cost as well as a prompt. Past
#: roughly two dozen instructions a model starts trading them off against each
#: other silently, which is worse than refusing the twenty-sixth: a refusal is
#: visible, and a quietly ignored rule is the drift this module exists to
#: prevent.
MAX_PREFERENCES: Final = 25

#: A preference is an instruction, not an essay. Anything longer is a briefing
#: that belongs in an uploaded document, where retrieval can put the relevant
#: part of it in front of the model instead of all of it every time.
MAX_RULE_LENGTH: Final = 200

#: ``dna`` keys this module owns. Everything else in ``dna`` (name, city,
#: services, website, industry, locale) belongs to onboarding and is not touched
#: here -- a partial write must never drop a field it does not understand.
_PREFERENCES_KEY: Final = "preferences"


class MemoryServiceError(Exception):
    """Base class for every refusal this module raises."""


class BusinessNotFoundError(MemoryServiceError):
    """No business with that id, or none this session is allowed to see."""

    def __init__(self, business_id: UUID) -> None:
        self.business_id = business_id
        super().__init__(f"No business {business_id}.")


class EmptyRuleError(MemoryServiceError):
    """The rule is blank once whitespace is collapsed."""


class RuleTooLongError(MemoryServiceError):
    """The rule is longer than :data:`MAX_RULE_LENGTH`."""

    def __init__(self, length: int) -> None:
        self.length = length
        super().__init__(
            f"A remembered preference must be at most {MAX_RULE_LENGTH} characters; "
            f"this one is {length}. Long guidance belongs in an uploaded document, "
            "where retrieval can surface the relevant part instead of carrying all "
            "of it in every prompt."
        )


class PreferenceNotFoundError(MemoryServiceError):
    """No preference in force matches the one the caller asked to change.

    Raised rather than treated as a no-op, because an edit is not idempotent the way a
    delete is: a caller who asked to change rule A into rule B and got silence would
    have no way to tell "done" from "A was already gone, so B was never written".
    """

    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(
            "That preference is not in force, so there is nothing to change. It may "
            "have been removed in another tab."
        )


class DuplicatePreferenceError(MemoryServiceError):
    """The edited text collides with a DIFFERENT preference already in force.

    Refused rather than merged. Silently collapsing two rules into one would remove a
    rule the owner never asked to remove, and the panel would show one fewer line than
    the person just confirmed -- which reads as data loss, because it is.
    """

    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(
            "You already have that preference, so this edit would merge two rules into "
            "one. Delete one of them instead."
        )


class PreferenceLimitError(MemoryServiceError):
    """The business already holds :data:`MAX_PREFERENCES` preferences."""

    def __init__(self, limit: int = MAX_PREFERENCES) -> None:
        self.limit = limit
        super().__init__(
            f"This business already has {limit} remembered preferences, which is the "
            "maximum. Remove one before adding another -- silently dropping the "
            "newest would leave the owner believing a rule is in force when it is not."
        )


class BusinessMemory(BaseModel):
    """What the agent durably knows about one business.

    Frozen: this is a snapshot handed to prompt assembly, and a caller that can
    edit it after the fact can change the instructions a model received without
    that change existing anywhere in the database.
    """

    model_config = ConfigDict(frozen=True)

    business_id: UUID
    #: Register: "professional", "friendly", "concise". Reaches the prompt through
    #: ``dna``, so :func:`to_prompt_lines` deliberately does not repeat it.
    tone: str | None = None
    #: Who the writing is for. Nothing else carries this into the prompt today,
    #: which is why :func:`to_prompt_lines` does emit it.
    audience: str | None = None
    #: Claims this business may never make -- regulated wording, guarantees it
    #: cannot honour. Also already rendered from ``dna``.
    banned_claims: tuple[str, ...] = ()
    #: Preferences the owner has CONFIRMED, in the order they were confirmed.
    #: Never inferred; see the module docstring.
    preferences: tuple[str, ...] = ()

    @property
    def remembered_count(self) -> int:
        """How many preferences the UI should claim to be applying.

        The UI says "applying 4 remembered preferences", and that number must be
        the length of the list the owner can see and edit -- not a count of
        everything in ``dna``, which would make the panel and the claim disagree.
        """
        return len(self.preferences)


def normalise_rule(rule: str) -> str:
    """Collapse whitespace and strip, preserving the author's own casing.

    Casing is preserved because the rule is shown back to the owner and read by a
    model: ``"Never use Sie"`` and ``"never use sie"`` mean the same thing to the
    dedup check (see :func:`rule_key`) but the first is what the owner typed.
    """
    return " ".join(rule.split())


def rule_key(rule: str) -> str:
    """The identity of a rule for deduplication: normalised and case-folded.

    ``casefold`` rather than ``lower`` because this product is German-first and
    ``"STRASSE"``/``"straße"`` only compare equal under ``casefold``.
    """
    return normalise_rule(rule).casefold()


def _clean_sequence(value: Any) -> tuple[str, ...]:
    """Read a ``dna`` list defensively, deduplicating by :func:`rule_key`.

    ``dna`` is JSONB: it can hold anything a previous version of this code, an
    onboarding draft, or a hand-run SQL statement put there. Anything that is not
    a non-empty string is dropped rather than raising -- a malformed preference
    must not make a business unreadable.
    """
    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalised = normalise_rule(item)
        if not normalised:
            continue
        key = normalised.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(normalised)
    return tuple(kept)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = normalise_rule(value)
    return stripped or None


def memory_from_dna(business_id: UUID, dna: Mapping[str, Any]) -> BusinessMemory:
    """Project a raw ``dna`` mapping into :class:`BusinessMemory`.

    Pure, and exported, so the projection is assertable without a database -- and
    so the defensive reading above has exactly one home.
    """
    return BusinessMemory(
        business_id=business_id,
        tone=_clean_text(dna.get("tone")),
        audience=_clean_text(dna.get("audience")),
        banned_claims=_clean_sequence(dna.get("banned_claims")),
        preferences=_clean_sequence(dna.get(_PREFERENCES_KEY)),
    )


def to_prompt_lines(memory: BusinessMemory) -> list[str]:
    """The exact lines the nodes receive as ``state["remembered"]``.

    Pure on purpose. This is the last hop before text reaches a model, so it is
    the one function whose output a test can pin *exactly* -- and "what did the
    model actually get told" stops being folklore.

    **What is emitted:** the audience, if one is on record, and then every
    confirmed preference verbatim in confirmation order.

    **What is deliberately NOT emitted: tone and banned claims.**
    :func:`backend.app.agents.nodes.prompts.system` already renders both from
    ``dna`` ("Tone: ..." and "Never claim: ..."), so repeating them here would put
    the same instruction in the prompt twice. That is not harmless: a duplicated
    instruction reads as emphasis, so the tone would quietly start outweighing
    the task. The rule is therefore "emit what nothing else carries", and
    ``audience`` qualifies because nothing else carries it today.

    An empty list is a normal, honest answer -- it is what a business that has
    confirmed nothing should contribute to a prompt.
    """
    lines: list[str] = []
    if memory.audience:
        lines.append(f"Write for this audience: {memory.audience}.")
    lines.extend(memory.preferences)
    return lines


async def _load(business_id: UUID, *, session: AsyncSession, lock: bool) -> Business:
    statement = select(Business).where(Business.id == business_id)
    if lock:
        # Two "remember" calls racing on the same row would otherwise read the
        # same preference list and one would overwrite the other's append -- a
        # lost rule that nobody would ever notice, because the UI would have
        # already confirmed it.
        statement = statement.with_for_update()
    business = (await session.execute(statement)).scalar_one_or_none()
    if business is None:
        raise BusinessNotFoundError(business_id)
    return business


async def load_memory(business_id: UUID, *, session: AsyncSession) -> BusinessMemory:
    """Read this business's durable memory.

    Called once per run, at ``INTAKE``. Raises :class:`BusinessNotFoundError`
    rather than returning an empty memory for an unknown id: an empty memory is a
    real state (a business that has confirmed nothing), and conflating it with
    "that business does not exist" would let a run proceed for a tenant that is
    not there.
    """
    business = await _load(business_id, session=session, lock=False)
    return memory_from_dna(business_id, business.dna or {})


def _validated(rule: str) -> str:
    normalised = normalise_rule(rule)
    if not normalised:
        raise EmptyRuleError(
            "A remembered preference cannot be blank. There is nothing for a model "
            "to obey, and the owner would see an empty row in the memory panel."
        )
    if len(normalised) > MAX_RULE_LENGTH:
        raise RuleTooLongError(len(normalised))
    return normalised


def _with_preferences(dna: Mapping[str, Any], preferences: Sequence[str]) -> dict[str, Any]:
    """A new ``dna`` dict, because JSONB mutation in place is not tracked.

    Mutating ``business.dna["preferences"]`` would leave SQLAlchemy unaware that
    anything changed, the flush would emit no UPDATE, and the write would appear
    to succeed. Building a new dict is the fix, and it also preserves every key
    this module does not own.
    """
    return {**dict(dna), _PREFERENCES_KEY: list(preferences)}


async def remember(business_id: UUID, *, rule: str, session: AsyncSession) -> None:
    """Append one owner-confirmed preference to business memory.

    Idempotent by :func:`rule_key`: stating the same preference again is a no-op,
    not a second line in every future prompt.

    Does not commit -- the caller owns the transaction, so approving a proposed
    rule and applying it can be one atomic unit rather than two writes with a
    window between them where a proposal is approved but not in force.
    """
    normalised = _validated(rule)
    business = await _load(business_id, session=session, lock=True)
    existing = _clean_sequence((business.dna or {}).get(_PREFERENCES_KEY))

    if normalised.casefold() in {item.casefold() for item in existing}:
        return
    if len(existing) >= MAX_PREFERENCES:
        raise PreferenceLimitError()

    business.dna = _with_preferences(business.dna or {}, [*existing, normalised])
    await session.flush()


async def forget(business_id: UUID, *, rule: str, session: AsyncSession) -> None:
    """Remove one preference from business memory, leaving the rest intact.

    Matched by :func:`rule_key`, so the owner does not have to reproduce their own
    capitalisation to delete something. Forgetting a rule that is not there is a
    no-op: the caller's intent ("this must not be in force") is already satisfied,
    and raising would make a double-clicked delete an error.
    """
    key = rule_key(rule)
    business = await _load(business_id, session=session, lock=True)
    existing = _clean_sequence((business.dna or {}).get(_PREFERENCES_KEY))
    kept = [item for item in existing if item.casefold() != key]
    if len(kept) == len(existing):
        return
    business.dna = _with_preferences(business.dna or {}, kept)
    await session.flush()


async def revise(business_id: UUID, *, old_rule: str, new_rule: str, session: AsyncSession) -> str:
    """Rewrite one preference IN PLACE, keeping its position in the list.

    Returns the normalised text now in force, so the caller does not have to guess how
    the rule was cleaned up.

    Position is preserved deliberately. ``forget`` followed by ``remember`` would be two
    lines of code and would look equivalent, but it moves the edited rule to the END of
    the list -- and the list's order is the order the owner confirmed things in, which
    the panel renders and which prompt assembly emits verbatim. Fixing a typo must not
    silently reorder the instructions a model receives.

    Matched by :func:`rule_key`, so the caller does not have to reproduce the stored
    capitalisation to address a rule.

    Does not commit: like the rest of this module, the caller owns the transaction.
    """
    normalised = _validated(new_rule)
    target = rule_key(old_rule)

    business = await _load(business_id, session=session, lock=True)
    existing = _clean_sequence((business.dna or {}).get(_PREFERENCES_KEY))

    position = next(
        (i for i, item in enumerate(existing) if item.casefold() == target),
        None,
    )
    if position is None:
        raise PreferenceNotFoundError(old_rule)

    replacement = normalised.casefold()
    if any(item.casefold() == replacement for i, item in enumerate(existing) if i != position):
        raise DuplicatePreferenceError(normalised)

    if existing[position] == normalised:
        # Nothing changed once whitespace was collapsed. Returning early keeps this a
        # read, so a no-op save does not take a row lock's worth of write traffic.
        return normalised

    updated = [*existing]
    updated[position] = normalised
    business.dna = _with_preferences(business.dna or {}, updated)
    await session.flush()
    return normalised


def known_keys(rules: Iterable[str]) -> set[str]:
    """The dedup keys of ``rules``.

    Used by the feedback distiller to avoid proposing a rule that is already in
    force. It lives here because the dedup rule must have one definition: a
    proposer using a different notion of "same rule" than the appender would
    propose duplicates forever.
    """
    return {rule_key(rule) for rule in rules}
