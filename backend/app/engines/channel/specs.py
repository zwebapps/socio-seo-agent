"""What each channel will accept: ONE table, for the product and the eval both.

This module exists because there were two, and they disagreed. ``agents/nodes``
held plain integer ceilings (``linkedin: 3000``) under the channel names the
product actually stores; ``evals/rubric`` held the richer spec the rubric grades
against (minimum length, hashtag ranges, whether a link works at all) under
DIFFERENT names -- ``facebook_post`` where the product says ``facebook``,
``instagram_caption`` where it says ``instagram``. The numbers conflicted too:
LinkedIn's 3,000 is the platform's *reject* threshold in one table and was being
used as the editorial target in the other. The rubric's own comment predicted the
consequence: "two copies of a platform limit is how the eval starts disagreeing
with the product it is grading."

So this is that single source, and three things follow from it.

**Canonical names are the PRODUCT's names.** ``linkedin``, ``facebook``,
``instagram``, ``x``, ``email`` -- the keys already in
``services/link_service._CHANNEL_TAGS``, already written into
``AgentState["renderings"]`` and already stored on every short link. The rubric's
names become aliases, so the twenty eval cases keep naming their channel exactly
as they do today and nothing in ``evals/dataset.py`` moves. ``blog_article`` keeps
an entry of its own: it is a deliverable, not a link channel, and it never had a
product name to collide with.

**Two length numbers, never one.** ``max_chars`` is the editorial target and
``hard_max_chars`` is the platform's own reject threshold. "Longer than we would
like" and "the API will refuse this" deserve different consequences: REPACK trims
at the hard limit, because a post the platform rejects is not a deliverable, and
reports being over the target instead of silently cutting good copy at a number
nobody would recognise.

**Numbers are starting values to verify against provider documentation**, exactly
as ``docs/CHANNELS.md`` §4 instructs ("treat any number here as a default to
check, never as truth"). They are sourced from §6 of that document.

Pure: no I/O, no model, no database -- ``tests/test_engine_boundary.py`` enforces
that, which is also why this table lives in an engine rather than in the agent
layer that first needed it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CHANNEL_ALIASES",
    "CHANNEL_SPECS",
    "ChannelSpec",
    "canonical_channel",
    "canonicalise_known",
    "hard_char_limits",
    "has_spec",
    "spec_for",
]


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    """What one channel will accept.

    Mirrors the ``ChannelSpec`` sketch in ``docs/CHANNELS.md`` §4, reduced to the
    fields a deterministic length/hashtag/link check needs. The rest of that sketch
    (media requirements, aspect ratios, tone shift) is real but is not something
    arithmetic can verify, so it is not pretended to here.
    """

    max_chars: int
    hard_max_chars: int
    hashtags_min: int
    hashtags_max: int
    #: False where a URL in the body is not clickable (Instagram, TikTok). A link
    #: there is not untidy, it is a dead CTA and lost attribution.
    link_in_body: bool
    min_chars: int = 0


CHANNEL_SPECS: Final[Mapping[str, ChannelSpec]] = {
    # Long-form article. The minimum is the real gate here: a 300-word "article"
    # cannot cover a commercial-intent query, whatever it scores on-page.
    "blog_article": ChannelSpec(
        max_chars=40_000,
        hard_max_chars=200_000,
        hashtags_min=0,
        hashtags_max=0,
        link_in_body=True,
        min_chars=2_500,
    ),
    # 1,300-1,700 chars, 3 hashtags max, 3,000 is the platform ceiling (§6).
    "linkedin": ChannelSpec(
        max_chars=1_700,
        hard_max_chars=3_000,
        hashtags_min=0,
        hashtags_max=3,
        link_in_body=True,
    ),
    # ~2,200 char ceiling, 3-5 hashtags, and never a caption URL. The editorial
    # target IS the ceiling here: there is no headroom to be "a bit long" in.
    "instagram": ChannelSpec(
        max_chars=2_200,
        hard_max_chars=2_200,
        hashtags_min=3,
        hashtags_max=5,
        link_in_body=False,
    ),
    # Short post (~80-150 words); the link preview does the work. 63,206 is
    # Facebook's real ceiling, which is why it is here and not in the target
    # column -- a 60,000-character "short post" is an editorial failure, not a
    # rejected request, and the two are reported differently.
    "facebook": ChannelSpec(
        max_chars=1_000,
        hard_max_chars=63_206,
        hashtags_min=0,
        hashtags_max=3,
        link_in_body=True,
    ),
    # Email body. Subject and preheader are separate deliverables.
    "email": ChannelSpec(
        max_chars=2_500,
        hard_max_chars=100_000,
        hashtags_min=0,
        hashtags_max=0,
        link_in_body=True,
        min_chars=300,
    ),
    # The one channel where the hard limit is the whole design. Two hashtags,
    # because at 280 characters a third is a word the post cannot afford.
    "x": ChannelSpec(
        max_chars=280,
        hard_max_chars=280,
        hashtags_min=0,
        hashtags_max=2,
        link_in_body=True,
    ),
}

#: Alternative names that mean an existing spec.
#:
#: These are not deprecations to migrate off. ``facebook_post`` names the
#: DELIVERABLE and ``facebook`` names the CHANNEL, and both readings are correct
#: in the place they are used -- the eval grades a deliverable, the short link
#: tags a channel. Mapping them is cheaper and less breakable than renaming
#: twenty eval cases and a column of stored channel tags to agree on one word.
CHANNEL_ALIASES: Final[Mapping[str, str]] = {
    "facebook_post": "facebook",
    "instagram_caption": "instagram",
    "instagram_post": "instagram",
    "linkedin_post": "linkedin",
    "twitter": "x",
    "article": "blog_article",
    "blog": "blog_article",
}


def canonical_channel(channel: str) -> str:
    """Fold a channel name to its canonical key. Case- and space-insensitive.

    Returns the folded name even when nothing has a spec for it, so a caller can
    report the name it was given rather than the name it guessed.
    """
    name = channel.strip().lower()
    return CHANNEL_ALIASES.get(name, name)


def spec_for(channel: str) -> ChannelSpec:
    """The spec for ``channel``.

    Raises ``KeyError`` for a channel with no spec, and that is deliberate --
    inherited from ``evals.rubric.score_format``, where it was already load-bearing:
    scoring 1.0 for a channel nobody has specified would let a harness bug read as a
    perfect result. A caller that legitimately does not know (REPACK, which renders
    whatever channels a run was configured for) asks with ``.get`` on
    :data:`CHANNEL_SPECS` instead.
    """
    return CHANNEL_SPECS[canonical_channel(channel)]


def has_spec(channel: str) -> bool:
    """Whether a spec exists for ``channel``, alias or canonical name.

    Exists so a caller can ask without catching :func:`spec_for`'s deliberate
    ``KeyError`` -- the eval dataset checks every case names a channel we can
    actually grade, and an exception is the wrong control flow for a question.
    """
    return canonical_channel(channel) in CHANNEL_SPECS


def canonicalise_known(channels: Iterable[str]) -> list[str]:
    """``channels`` canonicalised and deduplicated, or ``ValueError`` naming the strays.

    The single authority for "is this a channel a caller may ASK for", shared by every
    write that accepts a channel list from outside — starting a run and configuring an
    automation. It exists because those two had begun to answer the question separately,
    and two copies of a validation rule is how a channel becomes acceptable on one route
    and refused on the other.

    **Refusing beats dropping, and only because a human is present.** A request for
    ``["linkedin", "threads"]`` that quietly kept LinkedIn alone looks like a success
    that simply produced nothing for Threads. A channel with no entry in
    :data:`CHANNEL_SPECS` cannot be length- or link-checked, which is already why
    ``actuators/social.py`` refuses one, so accepting it here only defers the refusal to
    a point where it costs a model call. That is the opposite of
    :func:`backend.app.agents.state.normalise_channels`, which DROPS — deliberately: it
    reads a JSONB column written months ago, where the alternative to dropping is a run
    that cannot start because a spec was retired after the row was stored.

    Order is the caller's, deduplicated: it is the order the review screen and the export
    pack list the posts in.
    """
    canonical: dict[str, None] = {}
    unknown: list[str] = []
    for raw in channels:
        name = canonical_channel(raw)
        if name and has_spec(name):
            canonical[name] = None
        else:
            unknown.append(raw.strip() or raw)
    if unknown:
        known = ", ".join(sorted(CHANNEL_SPECS))
        raise ValueError(f"unknown channel(s): {', '.join(unknown)}. Known: {known}")
    return list(canonical)


def hard_char_limits() -> dict[str, int]:
    """Just the platform reject thresholds, keyed by canonical name.

    For the one caller that needs nothing else: REPACK enforces length in code
    after generation, because counting is arithmetic and a model gets it wrong.
    """
    return {name: spec.hard_max_chars for name, spec in CHANNEL_SPECS.items()}
