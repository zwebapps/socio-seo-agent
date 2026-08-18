"""Prompt-set construction. Deterministic, versioned, and deliberately boring.

The prompt set is the *instrument*. Comparing this week's share of voice to last
week's is only valid if the questions did not change, so three properties matter
more than cleverness here:

* **Deterministic.** Same inputs produce the same prompts in the same order. No
  clock, no randomness, no set iteration leaking into output order.
* **Content-addressed.** A prompt's id is a hash of the prompt, so a reworded
  question cannot inherit the history of the old wording. Comparability becomes
  checkable rather than assumed.
* **Versioned twice.** `PROMPT_SET_VERSION` moves when the templates change;
  `prompt_set_fingerprint` moves when the *inputs* change (a new service, a
  dropped competitor). Both are needed: a version alone would call two runs
  comparable after someone added four services.

The shapes are the high-intent ones a customer actually cares about
(docs/ROADMAP.md section 2, constraint 2): "best {service} in {city}", "how much
does {service} cost", "{brand} vs {competitor}", "who offers {service} near
{city}", "is {brand} any good".
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Final

from .contract import (
    BRAND_NAMING_CATEGORIES,
    PROMPT_SET_VERSION,
    GeoPrompt,
    PromptCategory,
)
from .detect import fold_for_matching

__all__ = [
    "CATEGORY_ORDER",
    "SUPPORTED_LOCALES",
    "build_prompt_set",
    "prompt_id_for",
    "prompt_set_fingerprint",
    "resolve_locale",
]

# Brand-free categories first, brand-naming ones last. This is not cosmetic: a
# caller that has to bound a run truncates from the end, so the prompts it drops
# are the ones whose mentions mean least (a question containing the brand almost
# always yields a mention). Truncation therefore degrades the sample size, not
# the honesty of the number.
CATEGORY_ORDER: Final[tuple[PromptCategory, ...]] = (
    "best_in_city",
    "near_city",
    "cost",
    "comparison",
    "reputation",
)

#: Template languages we actually have wording for. Anything else falls back to
#: English *visibly* -- the resolved language is recorded on every prompt, so a
#: French business's set never silently masquerades as localised.
SUPPORTED_LOCALES: Final[tuple[str, ...]] = ("de", "en")

_TEMPLATES: Final[dict[str, dict[PromptCategory, str]]] = {
    "de": {
        "best_in_city": "Welcher Anbieter für {service} in {city} ist der beste?",
        "near_city": "Wer bietet {service} in der Nähe von {city} an?",
        "cost": "Was kostet {service}?",
        "comparison": "{brand} oder {competitor}: wer ist besser?",
        "reputation": "Ist {brand} empfehlenswert?",
    },
    "en": {
        "best_in_city": "What is the best {service} in {city}?",
        "near_city": "Who offers {service} near {city}?",
        "cost": "How much does {service} cost?",
        "comparison": "{brand} vs {competitor}: which is better?",
        "reputation": "Is {brand} any good?",
    },
}

#: Categories built once per service, in input order.
_SERVICE_CATEGORIES: Final[tuple[PromptCategory, ...]] = ("best_in_city", "near_city", "cost")


def resolve_locale(locale: str) -> str:
    """Map a requested locale onto a template language.

    `de-AT`, `de_CH` and `DE` all resolve to `de`; everything we have no wording
    for resolves to `en`. The result is stored on each prompt, which is what
    makes the fallback auditable instead of invisible.
    """
    language = locale.strip().lower().replace("_", "-").split("-")[0]
    return language if language in SUPPORTED_LOCALES else "en"


def prompt_id_for(*, set_version: str, locale: str, category: str, text: str) -> str:
    """The content hash that identifies one question.

    Canonical string is `version|locale|category|text`, SHA-256, first 16 hex
    characters. Stable across processes and machines (unlike `hash()`), and short
    enough to sit in a URL or a log line.

    16 hex characters is 64 bits. Collision risk across the few thousand prompts
    a deployment will ever hold is negligible, and a collision would merge two
    questions rather than corrupt money.
    """
    canonical = f"{set_version}|{locale}|{category}|{text}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def prompt_set_fingerprint(prompt_ids: Iterable[str]) -> str:
    """Fingerprint the *questions asked*, independent of the order they were asked in.

    Sorted and de-duplicated before hashing: a run that shuffled the same
    questions, or probed one of them twice, asked the same thing and stays
    comparable. A run that added or dropped a question did not, and the
    fingerprint changes so `diff_share_of_voice` refuses to subtract them.
    """
    joined = "\n".join(sorted(set(prompt_ids)))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _clean_unique(values: Sequence[str], *, exclude: frozenset[str] = frozenset()) -> list[str]:
    """Trim, drop blanks, drop `exclude`, and de-duplicate by folded form.

    De-duplication is by comparison form, so `Badsanierung`, `badsanierung ` and
    `BADSANIERUNG` produce one prompt rather than three. Three would triple that
    question's weight in the denominator -- a silently biased sample.

    The first spelling seen wins, because it is the one the business typed.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        trimmed = value.strip()
        if not trimmed:
            continue
        folded = fold_for_matching(trimmed)
        if not folded or folded in seen or folded in exclude:
            continue
        seen.add(folded)
        kept.append(trimmed)
    return kept


def build_prompt_set(
    *,
    business_name: str,
    city: str,
    services: Sequence[str],
    competitors: Sequence[str] = (),
    locale: str = "de",
) -> list[GeoPrompt]:
    """Build the fixed prompt set for one business.

    Ordered by category (`CATEGORY_ORDER`) and, within a category, by the order
    the services or competitors were given. Raises `ValueError` for a blank
    business name or city -- a prompt set built around an empty string would
    still run, still cost money, and measure nothing.

    An empty `services` list is legal and yields the brand-shaped prompts only;
    that is a thin instrument, and the score's `unprompted_usable_answers` will
    read zero, which is the correct signal to the caller.
    """
    brand = business_name.strip()
    if not brand:
        raise ValueError("business_name must not be blank: a prompt set needs a brand to ask about")
    town = city.strip()
    if not town:
        raise ValueError("city must not be blank: local intent is the whole point of these prompts")

    language = resolve_locale(locale)
    templates = _TEMPLATES[language]

    clean_services = _clean_unique(services)
    # A "competitor" that folds onto the brand itself is the same business under
    # another spelling. "Müller Sanitär vs Mueller Sanitaer" is not a comparison.
    clean_competitors = _clean_unique(competitors, exclude=frozenset({fold_for_matching(brand)}))

    prompts: list[GeoPrompt] = []
    for category in CATEGORY_ORDER:
        template = templates[category]
        if category in _SERVICE_CATEGORIES:
            for service in clean_services:
                prompts.append(
                    _make(
                        template.format(service=service, city=town, brand=brand),
                        category=category,
                        locale=language,
                        subject=service,
                    )
                )
        elif category == "comparison":
            for competitor in clean_competitors:
                prompts.append(
                    _make(
                        template.format(brand=brand, competitor=competitor),
                        category=category,
                        locale=language,
                        subject=competitor,
                    )
                )
        else:  # reputation -- exactly one, and only about us
            prompts.append(
                _make(
                    template.format(brand=brand),
                    category=category,
                    locale=language,
                    subject=None,
                )
            )
    return prompts


def _make(
    text: str,
    *,
    category: PromptCategory,
    locale: str,
    subject: str | None,
) -> GeoPrompt:
    """Assemble one prompt, deriving its id and its brand-naming flag."""
    return GeoPrompt(
        prompt_id=prompt_id_for(
            set_version=PROMPT_SET_VERSION,
            locale=locale,
            category=category,
            text=text,
        ),
        text=text,
        category=category,
        locale=locale,
        set_version=PROMPT_SET_VERSION,
        contains_brand=category in BRAND_NAMING_CATEGORIES,
        subject=subject,
    )
