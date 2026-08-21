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

`audit_site` is the other half, and it points the other way: `score_page` grades
a page the agent just WROTE, `audit_site` grades the pages the customer already
OWNS. `PROBLEM.md` is why that is the more valuable half -- "the fastest leads
come from fixing CONVERSION on pages that already get traffic", because new
Google content takes 6-12 weeks to rank. It reads `crawl.PageFacts` rather than
HTML, so it costs nothing on top of the crawl HARVEST already runs.

No I/O: `html` arrives as a string because fetching belongs to the `crawl`
engine, and this module does not import it (or anything else that could reach
the network, a model, or the database -- `tests/test_engine_boundary.py`
enforces that).
"""

from backend.app.engines.seo.audit import (
    THIN_CONTENT_WORDS,
    AuditFinding,
    PageAudit,
    SiteAuditResult,
    audit_site,
)
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
    "THIN_CONTENT_WORDS",
    "AuditFinding",
    "PageAudit",
    "PageFacts",
    "ReadabilityStats",
    "SeoFinding",
    "SeoFindingCode",
    "SeoScoreRequest",
    "SeoScoreResult",
    "SeoSeverity",
    "SiteAuditResult",
    "analyse_readability",
    "audit_site",
    "build_article_jsonld",
    "build_faq_jsonld",
    "build_local_business_jsonld",
    "extract_facts",
    "flesch_reading_ease",
    "render_jsonld_script",
    "score_page",
    "validate_jsonld",
]
