"""The `channel` engine: make generated copy satisfy a channel's mechanical limits.

Deliberately not an LLM, for the reason already written down in
`agents/nodes/__init__.py` about length: *counting is arithmetic, so Python
enforces it after generation rather than asking the model to count -- it will get
it wrong, and the platform will reject the post.* Hashtag counts are the same kind
of quantity, and the same conclusion follows.

That conclusion is not theoretical. Measured against `openai/gpt-4.1-mini`
(2026-08-19): a prompt whose final line was the literal instruction
``Keine Hashtags`` produced a German article with **21 hashtags**. The live
evaluation showed the same thing at scale -- with retrieved passages in the
prompt, `format` fell from 0.95 to 0.35, almost entirely on hashtag caps in
channels that permit none. A model will not reliably obey a negative count
instruction, so the count is not its job.

    from backend.app.engines.channel import enforce_hashtags

    result = enforce_hashtags(text, minimum=0, maximum=3)
    result.text          # <= 3 hashtags, guaranteed
    result.removed       # how many had to be taken out
    result.shortfall     # how many are still missing (never fabricated)

Limits arrive as **arguments**, not from a table in this module. There are already
two channel-limit tables in this repo that disagree with each other
(`agents/nodes.CHANNEL_LIMITS` holds plain int ceilings under different channel
names; `evals/rubric.CHANNEL_LIMITS` holds the richer spec the rubric grades
against), and adding a third would make the drift worse. A caller passes the
numbers it is held to; when the `channel_specs` table lands in Phase 6 it becomes
the single source and nothing here changes.

No I/O, no model, no database -- `tests/test_engine_boundary.py` enforces that.
"""

from backend.app.engines.channel.hashtags import HashtagEnforcement, enforce_hashtags

__all__ = ["HashtagEnforcement", "enforce_hashtags"]
