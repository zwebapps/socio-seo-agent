"""Typed contract for the `claims` engine.

The shapes here cross two boundaries, so they are a published contract rather
than an internal detail:

* `VALIDATE` writes :class:`ClaimCheckResult` into ``AgentState["claim_check"]``,
  which is checkpointed to a JSONB column and rendered on the review screen.
* :attr:`ClaimCheckResult.fix_hint` is fed back into `GENERATE` **verbatim** on a
  retry, exactly as `SeoFinding.fix_hint` is (`docs/AGENT_RUNTIME.md` section 7).

Every field is primitive for the same reason the seo contract's are: the result
crosses a process boundary as JSON, so nothing here may carry behaviour.
"""

from pydantic import BaseModel, Field


class ClaimHit(BaseModel):
    """One banned claim, found once.

    Both the configured phrase and the text that actually matched are kept.
    They differ whenever a tolerance fired -- a different case, a line break, an
    inflected ending -- and a reviewer who is told only the configured phrase
    cannot find the sentence to fix. `start`/`end` are offsets into the
    *markup-stripped* text, which is what the matcher searched.
    """

    claim: str
    matched: str
    start: int
    end: int
    context: str


class ClaimCheckRequest(BaseModel):
    """Content to check, and the claim list to check it against.

    `content` is a string rather than a URL or a draft id because this engine
    performs no I/O: the caller has the draft already, and passing it in keeps
    the check deterministic and unit-testable.

    `contains_markup` defaults to True because the first caller is `VALIDATE`,
    whose draft is HTML. A social rendering is plain text and passes False, so
    that a literal ``<`` in a post is not mistaken for a tag.
    """

    content: str
    banned_claims: list[str] = Field(default_factory=list)
    contains_markup: bool = True


class ClaimCheckResult(BaseModel):
    """The verdict. Binary, because these are compliance rules, not preferences.

    A German dentist may not promise a treatment outcome (HWG) and a
    Steuerberater may not promise a tax saving (StBerG). "Only one forbidden
    promise" is not a partial success: the piece cannot be published either way.
    So there is no score -- there is `passed`, and there is the list of hits.

    `exercised` distinguishes "checked, clean" from "there was nothing to
    check". A business that has configured no banned claims passes vacuously,
    and reporting that as an earned pass would overstate what the gate did.
    """

    passed: bool
    exercised: bool
    checked: int
    hits: list[ClaimHit] = Field(default_factory=list)
    detail: str = ""
    #: Written for the content model, not for a human: it names the exact phrase
    #: to remove and forbids paraphrasing it. Empty when `passed`.
    fix_hint: str = ""

    @property
    def claims_found(self) -> tuple[str, ...]:
        """The distinct configured claims that were hit, in configuration order."""
        seen: list[str] = []
        for hit in self.hits:
            if hit.claim not in seen:
                seen.append(hit.claim)
        return tuple(seen)
