"""`geo` engine: AI-answer visibility, measured by probing.

**The product's differentiator, and the reason it needs its own honesty rules.**
Google rankings move over 6-12 weeks, which is longer than any demo and longer
than the gap to a first invoice. AI-answer visibility moves in days, so it is the
only metric that can show a business something real early
(docs/ROADMAP.md section 2, constraint 2).

There is no citation API for ChatGPT or Perplexity. So visibility is measured by
**probing**: a fixed prompt set is asked of N models and each answer is read for a
brand mention and a domain citation. Everything this package produces is
therefore a **sample, not a census** -- and every function in it is built to keep
that visible rather than to round it away.

Pure by construction: no I/O, no database, no LLM, no network
(docs/ARCHITECTURE.md section 3, enforced by tests/test_engine_boundary.py). The
model calls live one layer up in `backend/app/services/geo_service.py`, because
probing *is* a model call and an engine may not make one.

    prompts  = build_prompt_set(business_name=..., city=..., services=[...])
    presence = detect_presence(answer, brand=brand, competitors=rivals)
    sov      = share_of_voice(outcomes)
    delta    = diff_share_of_voice(previous_sov, sov)
"""

from .contract import (
    BRAND_NAMING_CATEGORIES,
    PROMPT_SET_VERSION,
    AnswerStatus,
    BrandIdentity,
    CategoryShare,
    CompetitorShare,
    GeoPrompt,
    ModelShare,
    PresenceResult,
    ProbeOutcome,
    PromptCategory,
    ShareOfVoice,
    SovDelta,
)
from .detect import (
    answer_excerpt,
    classify_answer,
    detect_presence,
    extract_hosts,
    fold_for_matching,
    looks_like_refusal,
    mentions_name,
    normalise_host,
)
from .prompts import (
    CATEGORY_ORDER,
    SUPPORTED_LOCALES,
    build_prompt_set,
    prompt_id_for,
    prompt_set_fingerprint,
    resolve_locale,
)
from .score import diff_share_of_voice, share_of_voice

__all__ = [
    "BRAND_NAMING_CATEGORIES",
    "CATEGORY_ORDER",
    "PROMPT_SET_VERSION",
    "SUPPORTED_LOCALES",
    "AnswerStatus",
    "BrandIdentity",
    "CategoryShare",
    "CompetitorShare",
    "GeoPrompt",
    "ModelShare",
    "PresenceResult",
    "ProbeOutcome",
    "PromptCategory",
    "ShareOfVoice",
    "SovDelta",
    "answer_excerpt",
    "build_prompt_set",
    "classify_answer",
    "detect_presence",
    "diff_share_of_voice",
    "extract_hosts",
    "fold_for_matching",
    "looks_like_refusal",
    "mentions_name",
    "normalise_host",
    "prompt_id_for",
    "prompt_set_fingerprint",
    "resolve_locale",
    "share_of_voice",
]
