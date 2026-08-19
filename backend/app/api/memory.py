"""Business memory: what the agent durably knows, and how the owner changes it.

This is the API behind the "What I remember about your business" panel. Four routes:

* ``GET    /memory``                        -- everything remembered
* ``POST   /memory/preferences``            -- remember one more thing
* ``PUT    /memory/preferences/{pref_id}``  -- fix the wording of one
* ``DELETE /memory/preferences/{pref_id}``  -- stop obeying one

Design points, in the order they matter.

**The business is derived from the session and does not appear in the path.** The sibling
route in this module is ``/businesses/{id}/proposals``, which takes the id and checks it,
so the difference is deliberate and worth stating. Taking an id the server then has to
verify buys addressability that nothing needs here -- there is one business per user --
and it costs a bootstrap problem the client cannot solve: ``GET /api/v1/auth/me`` returns
id, email, is_active and role, and NO business id, so a browser has no way to name its
own business until something tells it. Deriving it also removes the whole class of
cross-tenant path attacks rather than defending against it, which is the better trade
when nothing is given up. ``business_id`` IS returned in the response, so the panel can
go on to fetch its proposals from the id-taking sibling.

**A preference has no primary key, so the wire needs a stable identity that is not its
text.** Preferences live in ``businesses.dna['preferences']`` as a JSON array of strings
-- there is no row and no id, and adding one would be a schema change this task does not
own. Addressing an item by its ARRAY POSITION would be wrong: two tabs open on the same
panel, one deletes the second rule, and the other tab's "edit rule 3" then rewrites a
different rule than the one on screen. Addressing it by its raw TEXT in the path would
put a 200-character German sentence, complete with slashes and umlauts, into a URL
segment. So the identity is a short digest of the rule's dedup key
(:func:`backend.app.services.memory_service.rule_key`), returned with every item and
recomputed on write. It is stable across reordering, safe in a URL, and -- because it is
derived from the dedup key -- two rules that memory considers the same can never have
different ids.

**Editing is not delete-then-add.** ``memory_service.revise`` rewrites in place, so a
typo fix does not move the rule to the end of the list. The list's order is the order the
owner confirmed things in, and prompt assembly emits it verbatim, so reordering it is a
change to the instructions a model receives.

**Every write returns the WHOLE memory, not a bare 204.** The panel's next paint is then
the server's own account of what is in force rather than the client's optimistic guess,
which is what keeps two open tabs from disagreeing after an edit. It also means the
owner sees a de-duplicated no-op for what it is: they add a rule they already have, the
list comes back unchanged, and nothing pretends a twenty-sixth rule was stored.

**Only preferences are writable here, and that is deliberate rather than an omission.**
``tone``, ``audience`` and ``banned_claims`` are also part of ``dna`` and are returned so
the panel can show them, but ``memory_service`` owns exactly one ``dna`` key and says so;
the others belong to onboarding. They are marked read-only on the wire so the UI does not
have to guess. See the report accompanying this change for the gap that leaves.

The transaction-per-request shape is copied deliberately from ``api/feedback.py``: every
write happens inside ``business_session``, which owns the transaction, so an
``HTTPException`` raised from inside the block rolls the whole thing back and a refused
request writes nothing. That is also why the services flush rather than commit.
"""

import hashlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.runs import current_business
from backend.app.db.session import business_session
from backend.app.services import memory_service
from backend.app.services.memory_service import (
    MAX_PREFERENCES,
    MAX_RULE_LENGTH,
    BusinessMemory,
    BusinessNotFoundError,
    DuplicatePreferenceError,
    EmptyRuleError,
    PreferenceLimitError,
    PreferenceNotFoundError,
    RuleTooLongError,
    rule_key,
    to_prompt_lines,
)

#: No prefix: this router is mounted onto the ``/api/v1`` router in ``api/feedback.py``,
#: which is where ``/businesses/{id}/proposals`` already lives. Keeping memory beside the
#: proposals that feed it means one path family for "what this business has learned".
router = APIRouter(tags=["memory"])

#: Length of the wire identity for a preference. 16 hex characters is 64 bits of a
#: SHA-256 over the dedup key -- far more than enough to separate at most
#: :data:`MAX_PREFERENCES` strings, and short enough to read in a URL. It is an
#: identifier, never a security boundary: the rule it names is already visible to the
#: only caller who can reach this route.
_ID_LENGTH: Final = 16


def preference_id(rule: str) -> str:
    """The wire identity of one remembered preference.

    Derived from :func:`rule_key`, so it is insensitive to case and whitespace in exactly
    the way memory's own dedup is. Two strings memory treats as the same rule therefore
    get the same id, and an id can never address a rule that dedup would have merged.
    """
    return hashlib.sha256(rule_key(rule).encode("utf-8")).hexdigest()[:_ID_LENGTH]


BusinessSessionOpener = Callable[[UUID], AbstractAsyncContextManager[AsyncSession]]


def get_business_session_opener() -> BusinessSessionOpener:
    """The real, row-level-security-scoped session opener. Overridden in tests.

    Its own dependency rather than a reuse of the feedback module's, so overriding one
    module's database access in a test cannot silently redirect the other's.
    """
    return business_session


BusinessId = Annotated[UUID, Depends(current_business)]
OpenSession = Annotated[BusinessSessionOpener, Depends(get_business_session_opener)]


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RememberedPreference(CamelModel):
    """One rule in force, with the id the panel uses to edit or delete it."""

    id: str
    rule: str


class MemoryOut(CamelModel):
    """Everything the panel renders.

    ``prompt_lines`` is the honest centrepiece: it is the EXACT text the next run's
    system prompt receives, produced by the same
    :func:`backend.app.services.memory_service.to_prompt_lines` the graph calls. A panel
    that listed rules without showing this would be claiming an effect it could not
    demonstrate -- and this project's claims discipline is that "the agent updates
    persistent business preferences" has to be a mechanism, not an assertion.

    ``tone`` and ``banned_claims`` are shown but not writable here; see the module
    docstring.
    """

    business_id: UUID
    tone: str | None
    audience: str | None
    banned_claims: list[str]
    preferences: list[RememberedPreference]
    remembered_count: int
    prompt_lines: list[str]
    max_preferences: int
    max_rule_length: int
    #: Which of the fields above this API can change. Sent rather than hardcoded in the
    #: client so the panel's read-only markers cannot drift from what the server accepts.
    editable_fields: list[str]


class PreferenceRequest(CamelModel):
    """One rule's text.

    No ``max_length`` constraint on purpose. The service refuses an over-long rule with a
    message that states the limit and says where long guidance belongs, and that is a
    better answer than a bare 422 from the schema. Declaring the constraint here would
    also pre-empt the service's own validation, which the graph's callers rely on.
    """

    rule: str = Field(min_length=1)


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


_NOT_FOUND = _error(
    "not_found",
    "That item does not exist, or is not part of your business.",
)


def _project(memory: BusinessMemory) -> MemoryOut:
    """One place that turns a :class:`BusinessMemory` into the wire shape.

    Every route returns through here, so the four responses cannot describe the same
    memory differently.
    """
    return MemoryOut(
        business_id=memory.business_id,
        tone=memory.tone,
        audience=memory.audience,
        banned_claims=list(memory.banned_claims),
        preferences=[
            RememberedPreference(id=preference_id(rule), rule=rule) for rule in memory.preferences
        ],
        remembered_count=memory.remembered_count,
        prompt_lines=to_prompt_lines(memory),
        max_preferences=MAX_PREFERENCES,
        max_rule_length=MAX_RULE_LENGTH,
        editable_fields=["preferences"],
    )


def _resolve(memory: BusinessMemory, pref_id: str) -> str:
    """The rule text behind a wire id, or 404.

    Comparison is on the derived id rather than on a stored one because there is nothing
    stored to compare -- see the module docstring. An id the panel is holding from before
    someone else's edit simply no longer resolves, which is the correct answer: the rule
    it named is gone.
    """
    for rule in memory.preferences:
        if preference_id(rule) == pref_id:
            return rule
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)


def _refuse(exc: Exception) -> HTTPException:
    """Map a service refusal onto a status, keeping the service's own message.

    The messages are written for the person in the panel -- they name the limit and say
    what to do instead -- so replacing them with a generic string here would throw away
    the useful half.
    """
    if isinstance(exc, EmptyRuleError | RuleTooLongError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_error("invalid_rule", str(exc)),
        )
    if isinstance(exc, PreferenceLimitError):
        # 409, not 422: the rule itself is fine, the state of the list is what refuses
        # it. A 422 would tell the owner to fix their sentence, which would not help.
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_error("preference_limit", str(exc)),
        )
    if isinstance(exc, DuplicatePreferenceError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_error("duplicate_preference", str(exc)),
        )
    if isinstance(exc, PreferenceNotFoundError | BusinessNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    raise exc


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get(
    "/memory",
    response_model=MemoryOut,
    response_model_by_alias=True,
    summary="What the agent durably remembers about this business",
)
async def get_memory(
    business_id: BusinessId,
    open_session: OpenSession,
) -> MemoryOut:
    async with open_session(business_id) as session:
        try:
            memory = await memory_service.load_memory(business_id, session=session)
        except BusinessNotFoundError as exc:
            raise _refuse(exc) from exc
    return _project(memory)


@router.post(
    "/memory/preferences",
    response_model=MemoryOut,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Remember one more preference, from now on, in every run",
)
async def add_preference(
    payload: PreferenceRequest,
    business_id: BusinessId,
    open_session: OpenSession,
) -> MemoryOut:
    """201 with the whole memory.

    Idempotent by the service's dedup, so re-stating a rule is a no-op rather than a
    second line in every future prompt -- and the response makes that visible instead of
    implying a write happened.
    """
    async with open_session(business_id) as session:
        try:
            await memory_service.remember(business_id, rule=payload.rule, session=session)
            memory = await memory_service.load_memory(business_id, session=session)
        except (
            BusinessNotFoundError,
            EmptyRuleError,
            PreferenceLimitError,
            RuleTooLongError,
        ) as exc:
            raise _refuse(exc) from exc
    return _project(memory)


@router.put(
    "/memory/preferences/{pref_id}",
    response_model=MemoryOut,
    response_model_by_alias=True,
    summary="Reword one remembered preference, keeping its place in the list",
)
async def update_preference(
    pref_id: str,
    payload: PreferenceRequest,
    business_id: BusinessId,
    open_session: OpenSession,
) -> MemoryOut:
    """Read and write in ONE transaction.

    The id must be resolved to its current text before the rewrite, and doing that in a
    separate transaction would leave a window in which another tab's delete lands
    between the two -- the edit would then either fail confusingly or resurrect a rule
    the owner had just removed. ``revise`` takes the row lock for the write half.
    """
    async with open_session(business_id) as session:
        try:
            memory = await memory_service.load_memory(business_id, session=session)
            await memory_service.revise(
                business_id,
                old_rule=_resolve(memory, pref_id),
                new_rule=payload.rule,
                session=session,
            )
            memory = await memory_service.load_memory(business_id, session=session)
        except (
            BusinessNotFoundError,
            DuplicatePreferenceError,
            EmptyRuleError,
            PreferenceNotFoundError,
            RuleTooLongError,
        ) as exc:
            raise _refuse(exc) from exc
    return _project(memory)


@router.delete(
    "/memory/preferences/{pref_id}",
    response_model=MemoryOut,
    response_model_by_alias=True,
    summary="Stop obeying one remembered preference",
)
async def delete_preference(
    pref_id: str,
    business_id: BusinessId,
    open_session: OpenSession,
) -> MemoryOut:
    """200 with the remaining memory rather than 204.

    The panel repaints from this response, so a delete cannot leave it showing a rule
    that is no longer in force. A 204 would make the client's optimistic removal the
    only account of what happened.
    """
    async with open_session(business_id) as session:
        try:
            memory = await memory_service.load_memory(business_id, session=session)
            await memory_service.forget(
                business_id, rule=_resolve(memory, pref_id), session=session
            )
            memory = await memory_service.load_memory(business_id, session=session)
        except BusinessNotFoundError as exc:
            raise _refuse(exc) from exc
    return _project(memory)


__all__ = [
    "BusinessSessionOpener",
    "MemoryOut",
    "get_business_session_opener",
    "preference_id",
    "router",
]
