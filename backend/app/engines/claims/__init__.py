"""The `claims` engine: the regulated-claim guard, deterministic and free.

Some businesses may not say certain things. A German dentist may not promise a
treatment outcome (Heilmittelwerbegesetz), a Steuerberater may not promise a tax
saving (Steuerberatungsgesetz), and any business that promises "100% Garantie" in
copy is making a claim it cannot honour. The business lists those phrases at
onboarding as `dna.banned_claims`; this engine is what makes the list bite.

    from backend.app.engines.claims import ClaimCheckRequest, check_claims

    result = check_claims(ClaimCheckRequest(content=draft_html, banned_claims=banned))
    if not result.passed:
        retry_hint = result.fix_hint   # fed to GENERATE verbatim

The claim list is also placed in the system prompt ("Never claim: ..."), and this
engine exists because that is a request, not a control: a model can be talked out
of an instruction by the untrusted page text it is summarising, and it can simply
forget. The prompt reduces how often a banned claim is written; the engine is
what decides whether one is ever published.

No I/O and no model, so the same draft always yields the same verdict
(`tests/test_engine_boundary.py` enforces the imports).
"""

from backend.app.engines.claims.contract import (
    ClaimCheckRequest,
    ClaimCheckResult,
    ClaimHit,
)
from backend.app.engines.claims.match import (
    ADJECTIVE_ENDINGS,
    CONTEXT_WINDOW,
    MIN_INFLECTION_STEM,
    MIN_SUFFIXABLE_WORD,
    check_claims,
    claim_pattern,
    normalise,
    strip_markup,
)

__all__ = [
    "ADJECTIVE_ENDINGS",
    "CONTEXT_WINDOW",
    "MIN_INFLECTION_STEM",
    "MIN_SUFFIXABLE_WORD",
    "ClaimCheckRequest",
    "ClaimCheckResult",
    "ClaimHit",
    "check_claims",
    "claim_pattern",
    "normalise",
    "strip_markup",
]
