"""The `seo` engine: deterministic on-page scoring and JSON-LD.

This is the quality gate every piece of generated content passes through
(`docs/AGENT_RUNTIME.md` §7). It is **deliberately not an LLM**: counting
characters, words and links is arithmetic, not language, and a model doing it
would be non-deterministic, billable and untestable at exactly the point where
the system has to be trustworthy. The same HTML always scores the same, so a
score can be stored, compared across runs, and defended to a customer.

    from backend.app.engines.seo import SeoScoreRequest, score_page

    result = score_page(SeoScoreRequest(html=html, target_keyword="notdienst"))
    if not result.passed:
        retry_prompt_hints = result.fix_hints   # fed to GENERATE verbatim

No I/O: `html` arrives as a string because fetching belongs to the `crawl`
engine, and this module does not import it (or anything else that could reach
the network, a model, or the database -- `tests/test_engine_boundary.py`
enforces that).
"""

from backend.app.engines.seo.contract import (
    SeoFinding,
    SeoFindingCode,
    SeoScoreRequest,
    SeoScoreResult,
    SeoSeverity,
)
from backend.app.engines.seo.jsonld import (
    SCHEMA_CONTEXT,
    build_article_jsonld,
    build_faq_jsonld,
    build_local_business_jsonld,
    render_jsonld_script,
    validate_jsonld,
)
from backend.app.engines.seo.readability import (
    ReadabilityStats,
    analyse_readability,
    flesch_reading_ease,
)
from backend.app.engines.seo.rules import PageFacts, extract_facts
from backend.app.engines.seo.score import (
    FINDING_ORDER,
    PASS_SCORE,
    RULE_WEIGHTS,
    SEVERITY_PENALTY,
    score_page,
)

__all__ = [
    "FINDING_ORDER",
    "PASS_SCORE",
    "RULE_WEIGHTS",
    "SCHEMA_CONTEXT",
    "SEVERITY_PENALTY",
    "PageFacts",
    "ReadabilityStats",
    "SeoFinding",
    "SeoFindingCode",
    "SeoScoreRequest",
    "SeoScoreResult",
    "SeoSeverity",
    "analyse_readability",
    "build_article_jsonld",
    "build_faq_jsonld",
    "build_local_business_jsonld",
    "extract_facts",
    "flesch_reading_ease",
    "render_jsonld_script",
    "score_page",
    "validate_jsonld",
]
