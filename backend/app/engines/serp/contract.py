"""Types for the serp engine.

No timestamps anywhere in this module. An engine that reads the clock is an engine
whose output cannot be compared between runs, and comparing runs is the whole point
of rank tracking. The caller stamps time when it persists.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Intent(StrEnum):
    """What the searcher is trying to do, which is what the keyword is worth.

    Ordered by commercial value, and the order is used: ``expand_keywords`` sorts
    on it, because a list read top-down must lead with the terms that convert.
    """

    LOCAL = "local"
    COMMERCIAL = "commercial"
    COMPARISON = "comparison"
    INFORMATIONAL = "informational"


#: Sort weight. Local first: a place-name query is someone who wants the job done
#: now, and it converts several times better than a research query.
INTENT_RANK: dict[Intent, int] = {
    Intent.LOCAL: 0,
    Intent.COMMERCIAL: 1,
    Intent.COMPARISON: 2,
    Intent.INFORMATIONAL: 3,
}


class SerpResult(BaseModel):
    """One organic result."""

    model_config = ConfigDict(frozen=True)

    position: int
    url: str
    title: str
    snippet: str = ""
    host: str


class SerpPage(BaseModel):
    """One search, as the provider returned it."""

    model_config = ConfigDict(frozen=True)

    query: str
    locale: str
    results: list[SerpResult] = Field(default_factory=list)
    related_queries: list[str] = Field(default_factory=list)


class KeywordCandidate(BaseModel):
    """A term worth considering, with why."""

    model_config = ConfigDict(frozen=True)

    term: str
    intent: Intent
    #: Where it came from, so a human can judge it: "seed", "related", "title".
    source: str
    #: How many of the searched pages surfaced it. A weak proxy for demand, and
    #: labelled as such rather than dressed up as a volume figure we do not have.
    seen: int = 1


class CompetitorCandidate(BaseModel):
    """A host that keeps appearing where this business wants to be."""

    model_config = ConfigDict(frozen=True)

    host: str
    appearances: int
    best_position: int


class SerpConfigStatus(BaseModel):
    """Whether searches are real. Reported, never inferred by the caller."""

    provider: str
    using_fake: bool
    message: str


class SerpError(Exception):
    """Base for serp failures."""


class SerpProviderError(SerpError):
    """The provider could not be reached or refused the request."""
