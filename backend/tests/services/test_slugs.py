"""``services/slugs``: the public address a business gets."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from backend.app.services import auth_service
from backend.app.services.slugs import (
    MAX_SLUG_LENGTH,
    business_slug,
    slugify_business_name,
    suffixed_slug,
)

BUSINESS_ID = UUID("bc0e9c9c-a19b-475f-b5ba-e04a782b97cd")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Müller Sanitär GmbH", "mueller-sanitaer-gmbh"),
        # ü -> ue, not u: "muller" is a different word to a German reader, and this
        # string goes on a business card.
        ("Zahnärzte Dr. Weiß", "zahnaerzte-dr-weiss"),
        ("  Café Größe  ", "cafe-groesse"),
        ("Steuerberatung Schmidt & Partner", "steuerberatung-schmidt-partner"),
        ("Bäckerei   am    Markt", "baeckerei-am-markt"),
        ("ACME Ltd.", "acme-ltd"),
        # A leading or trailing separator would produce "-acme-" as an address.
        ("--ACME--", "acme"),
    ],
)
def test_a_name_becomes_a_readable_ascii_slug(name: str, expected: str) -> None:
    assert slugify_business_name(name) == expected


@pytest.mark.parametrize("name", ["!!!", "   ", "...---...", "東京ベーカリー", "مخبز"])
def test_a_name_with_nothing_slugifiable_returns_none(name: str) -> None:
    """``None``, not ``""`` and not ``"-"``.

    The column is NOT NULL and UNIQUE, so an empty string would be an unusable
    permanent address that also collides with the next such name. Returning None
    hands the decision to the caller, which is the only place that knows the id to
    fall back on. The last two cases are real: this cannot transliterate Japanese or
    Arabic, and pretending otherwise would be worse than admitting it.
    """
    assert slugify_business_name(name) is None


def test_a_business_with_an_unslugifiable_name_still_gets_an_address() -> None:
    """It is ugly, and it exists, which is the right trade in that order."""
    slug = business_slug("東京ベーカリー", BUSINESS_ID)

    assert slug == "b-bc0e9c9c"
    assert slug  # non-empty satisfies the NOT NULL column


def test_the_first_holder_of_a_name_gets_the_clean_slug() -> None:
    """No gratuitous suffix. Most businesses are the only one with their name."""
    assert business_slug("Müller Sanitär GmbH", BUSINESS_ID) == "mueller-sanitaer-gmbh"


def test_the_suffixed_form_is_deterministic_and_derived_from_the_id() -> None:
    """Which is what makes the collision retry safe without a pre-check query.

    A suffix from the business's own UUID cannot collide unless the UUID repeats, so
    signup retries ONCE rather than looping -- see ``auth_service.signup``.
    """
    first = suffixed_slug("Müller Sanitär GmbH", BUSINESS_ID)
    second = suffixed_slug("Müller Sanitär GmbH", BUSINESS_ID)

    assert first == second == "mueller-sanitaer-gmbh-bc0e9c9c"


def test_two_businesses_sharing_a_name_get_different_slugs() -> None:
    """The case the old code called ambiguous and used to reject slugs over."""
    other = UUID("11111111-2222-3333-4444-555555555555")

    assert suffixed_slug("Bäckerei am Markt", BUSINESS_ID) != suffixed_slug(
        "Bäckerei am Markt", other
    )


def test_a_very_long_name_is_capped_to_the_column_width() -> None:
    """A slug longer than the column is not a better address, it is a failed INSERT."""
    slug = slugify_business_name("Sanitär " * 40)

    assert slug is not None
    assert len(slug) <= MAX_SLUG_LENGTH
    # And it must not end mid-separator, which would read as a typo.
    assert not slug.endswith("-")


def test_the_suffixed_form_also_respects_the_column_width() -> None:
    """The suffix must displace the base, not overflow past the limit."""
    slug = suffixed_slug("Sanitär " * 40, BUSINESS_ID)

    assert len(slug) <= MAX_SLUG_LENGTH
    assert slug.endswith("bc0e9c9c"), "the uniqueness half must survive the truncation"


# --------------------------------------------------------------------------- #
# The breach check, wired into signup
# --------------------------------------------------------------------------- #


class _BreachedChecker:
    """Reports every password as breached. Satisfies `PwnedChecker` structurally."""

    async def breach_count(self, password: str) -> int:
        return 24_230_577


class _CleanChecker:
    async def breach_count(self, password: str) -> int:
        return 0


class _ExplodingChecker:
    """Stands in for the service being unreachable in a way `core.pwned` did not catch."""

    async def breach_count(self, password: str) -> int:
        raise AssertionError("the offline policy should have rejected this first")


async def test_signup_refuses_a_password_found_in_a_breach() -> None:
    """The point of the whole module: a leaked password is already on an attacker's list.

    No database is touched, because the refusal must happen before anything is
    written -- so passing `session=None` is safe here and also proves it.
    """
    with pytest.raises(auth_service.WeakPasswordError) as caught:
        await auth_service.signup(
            "owner@example.test",
            "correct horse battery staple",
            "Müller Sanitär",
            session=cast("Any", None),
            pwned_checker=_BreachedChecker(),
        )

    assert "known data breach" in str(caught.value)


async def test_the_breach_check_runs_after_the_offline_policy() -> None:
    """Order matters for cost AND for privacy.

    A password already too short must never leave the process to be asked about. The
    exploding checker proves the order rather than assuming it: if the network check
    ran first, this would raise AssertionError instead of WeakPasswordError.
    """
    with pytest.raises(auth_service.WeakPasswordError) as caught:
        await auth_service.signup(
            "owner@example.test",
            "short",
            "Müller Sanitär",
            session=cast("Any", None),
            pwned_checker=_ExplodingChecker(),
        )

    assert "at least" in str(caught.value), "the length rule should be what refused this"


async def test_a_clean_password_passes_the_breach_check() -> None:
    """It must not become a blanket refusal.

    Reaching the database is the SUCCESS signal here: `session=None` means the next
    thing after the check raises AttributeError, which proves the check let it past.
    """
    with pytest.raises(AttributeError):
        await auth_service.signup(
            "owner@example.test",
            "eine ziemlich lange passphrase hier",
            "Müller Sanitär",
            session=cast("Any", None),
            pwned_checker=_CleanChecker(),
        )
