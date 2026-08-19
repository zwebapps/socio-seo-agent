"""serp: keyword expansion, search-intent classification, competitor discovery.

A read-only engine. It performs network reads through a provider seam (like the
crawl engine) but holds no state, writes nothing, and calls no model — intent is
decided by deterministic rules, because the answer has to be identical between runs
for a keyword list to be diffable.
"""

from backend.app.engines.serp.contract import (
    INTENT_RANK,
    CompetitorCandidate,
    Intent,
    KeywordCandidate,
    SerpConfigStatus,
    SerpError,
    SerpPage,
    SerpProviderError,
    SerpResult,
)
from backend.app.engines.serp.expand import (
    NON_COMPETITOR_HOSTS,
    discover_competitors,
    expand_keywords,
    is_competitor_host,
    normalise_host,
    normalise_term,
)
from backend.app.engines.serp.intent import classify_intent, fold
from backend.app.engines.serp.provider import (
    FakeSerpProvider,
    SerpProvider,
    TavilySerpProvider,
    get_serp_provider,
    serp_config_status,
)

__all__ = [
    "INTENT_RANK",
    "NON_COMPETITOR_HOSTS",
    "CompetitorCandidate",
    "FakeSerpProvider",
    "Intent",
    "KeywordCandidate",
    "SerpConfigStatus",
    "SerpError",
    "SerpPage",
    "SerpProvider",
    "SerpProviderError",
    "SerpResult",
    "TavilySerpProvider",
    "classify_intent",
    "discover_competitors",
    "expand_keywords",
    "fold",
    "get_serp_provider",
    "is_competitor_host",
    "normalise_host",
    "normalise_term",
    "serp_config_status",
]
