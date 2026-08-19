"""The `channel` engine: hashtag limits are arithmetic, so Python enforces them."""

from __future__ import annotations

import re

import pytest

from backend.app.engines.channel import enforce_hashtags
from evals.rubric import CHANNEL_LIMITS, Rendering, extract_hashtags, score_format


def test_a_channel_that_permits_no_hashtags_gets_none() -> None:
    """The blog_article case: max 0, and the model kept adding them anyway."""
    text = "Rohrreinigung in Koblenz spart Zeit. #Rohrreinigung #Koblenz #Notdienst"

    result = enforce_hashtags(text, minimum=0, maximum=0)

    assert extract_hashtags(result.text) == ()
    assert result.removed == 3
    assert "Rohrreinigung in Koblenz spart Zeit." in result.text


def test_trimming_keeps_the_earliest_hashtags() -> None:
    """The late ones are usually an appended filler block; the early ones carry sense."""
    text = "Wir kommen #sofort vorbei. #Koblenz #Notdienst #Rohr #Service"

    result = enforce_hashtags(text, minimum=0, maximum=2)

    assert result.kept == ("#sofort", "#Koblenz")
    assert extract_hashtags(result.text) == ("#sofort", "#Koblenz")
    assert result.removed == 3


def test_text_already_inside_the_limit_is_returned_untouched() -> None:
    """No cosmetic rewriting of copy that was already fine."""
    text = "Frisches Brot jeden Morgen.\n\n#Bäckerei #Handwerk #Frisch"

    result = enforce_hashtags(text, minimum=3, maximum=5)

    assert result.text == text
    assert result.removed == 0
    assert result.changed is False
    assert result.shortfall == 0


def test_a_shortfall_is_reported_and_never_fabricated() -> None:
    """Inventing a hashtag would be an engine writing marketing copy."""
    text = "Zahnreinigung ohne Wartezeit. #Zahnarzt"

    result = enforce_hashtags(text, minimum=3, maximum=5)

    assert result.text == text
    assert result.shortfall == 2
    assert result.removed == 0


def test_a_url_fragment_is_not_mistaken_for_a_hashtag() -> None:
    """Mangling a URL to satisfy a hashtag cap would break the CTA."""
    text = "Termine: https://example.test/praxis#termine buchen. #Zahnarzt #Koblenz"

    result = enforce_hashtags(text, minimum=0, maximum=0)

    assert "https://example.test/praxis#termine" in result.text
    assert result.removed == 2


def test_removal_leaves_no_double_space_or_space_before_punctuation() -> None:
    """A cap satisfied by leaving "Zeit ." behind is not a publishable fix."""
    text = "Wir sparen Zeit #Notdienst , und Geld #Koblenz ."

    result = enforce_hashtags(text, minimum=0, maximum=0)

    assert "  " not in result.text
    assert " ," not in result.text
    assert " ." not in result.text


def test_paragraph_structure_survives_removal() -> None:
    """Newlines are part of the deliverable, so only spaces are collapsed."""
    text = "Erster Absatz. #a\n\nZweiter Absatz. #b\n\nDritter Absatz. #c"

    result = enforce_hashtags(text, minimum=0, maximum=0)

    assert result.text.count("\n\n") == 2
    assert "\n\n\n" not in result.text


@pytest.mark.parametrize(("minimum", "maximum"), [(3, 1), (-1, 0), (0, -2)])
def test_an_impossible_spec_raises_rather_than_guessing(minimum: int, maximum: int) -> None:
    """A bad spec is a configuration bug, not a piece of bad copy."""
    with pytest.raises(ValueError):
        enforce_hashtags("text", minimum=minimum, maximum=maximum)


# --------------------------------------------------------------------------- #
# The property that matters: enforcement satisfies the scorer that was failing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("channel", ["blog_article", "linkedin", "facebook_post"])
def test_enforcement_clears_the_hashtag_violations_the_live_eval_found(channel: str) -> None:
    """The regression, reproduced and closed.

    These are the three channels whose `format` score collapsed in the RAG arm of
    the 2026-08-19 live run, every failure a hashtag cap. Scoring the enforced text
    with the real rubric is the only assertion that proves the two agree.
    """
    limits = CHANNEL_LIMITS[channel]
    tags = " ".join(f"#tag{index}" for index in range(9))

    # The body has to sit strictly inside this channel's own length window, or the
    # length rule floors the score too and the assertion below stops being about
    # hashtags at all. (First version of this test used one fixed body and did
    # exactly that: LinkedIn's 3,000-char hard cap made before and after both 0.0.)
    target = max(limits.min_chars + 200, 400)
    assert target < limits.max_chars - len(tags), f"no valid body length for {channel}"
    sentence = "Guter Text über den Service. "
    body = (sentence * (target // len(sentence) + 1))[:target]
    text = body + tags

    before = score_format(Rendering(text=text), channel)
    result = enforce_hashtags(text, minimum=limits.hashtags_min, maximum=limits.hashtags_max)
    after = score_format(Rendering(text=result.text), channel)

    assert before.score < 1.0, "the fixture must actually violate the cap"
    assert len(extract_hashtags(result.text)) <= limits.hashtags_max
    assert after.score > before.score
    assert not any("hashtag" in violation.lower() for violation in after.violations)


# --------------------------------------------------------------------------- #
# A `#` is not always a hashtag
# --------------------------------------------------------------------------- #


def test_a_protected_pattern_survives_enforcement() -> None:
    """The regression that destroyed grounding in the 2026-08-19 re-run.

    Chunk ids are `<case_id>#<ordinal>`, so a citation contains a `#`. Enforcement
    rewrote `[chunk:plumber-01#0]` into `[chunk:plumber-01]`, and the grounding
    scorer then reported -- correctly -- a citation to a chunk that was never
    retrieved. A formatter must not edit an identifier.
    """
    citation = re.compile(r"\[chunk:[^\]\s]+\]")
    text = "Der Notdienst kostet 89 Euro. [chunk:plumber-01#0] Anfahrt frei. #Notdienst"

    result = enforce_hashtags(text, minimum=0, maximum=0, protect=(citation,))

    assert "[chunk:plumber-01#0]" in result.text, "the citation id must be untouched"
    assert result.removed == 1, "only the real hashtag is removed"
    assert "#Notdienst" not in result.text


def test_without_protection_the_same_text_loses_its_citation() -> None:
    """The counterpart: proves `protect` is what saves it, not luck."""
    text = "Kosten 89 Euro. [chunk:plumber-01#0]"

    result = enforce_hashtags(text, minimum=0, maximum=0)

    assert "[chunk:plumber-01#0]" not in result.text
    assert result.removed == 1
