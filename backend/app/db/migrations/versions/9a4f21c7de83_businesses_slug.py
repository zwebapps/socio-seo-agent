"""businesses.slug, so the public link hub has a readable address

Revision ID: 9a4f21c7de83
Revises: 7c1e4a90b2d5
Create Date: 2026-08-19 16:40:00.000000

``/go/{slug}`` is the bio link for Instagram and TikTok -- the channels with no
clickable link of their own, so for them it is the entire conversion path. It
currently takes a v4 UUID, because ``businesses`` had no slug to take. A UUID is
unreadable, unsayable, and impossible to put on a business card.

``api/links.py`` recorded why a slug had been rejected rather than deferred, and
both objections were about deriving one from the name AT READ TIME: it is
ambiguous the first time two customers share a name, and resolving it would mean a
full-table scan on a public endpoint. A stored column answers both. Uniqueness
becomes a database constraint rather than a hope, and the lookup becomes an indexed
equality on a unique column.

The backfill is plain SQL rather than a call into the application's slugifier, on
purpose: a migration has to describe the database at THIS revision, and importing
app code would make an old migration's behaviour change the next time that
function is edited.

**Existing UUID links keep working.** The hub route accepts either form, because
the old address may already be printed on a flyer or sitting in an Instagram bio,
and this project's rule is that a published link never dies.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a4f21c7de83"
down_revision: Union[str, Sequence[str], None] = "7c1e4a90b2d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: 80 characters: long enough for a real German business name once it is slugified
#: ("mueller-sanitaer-notdienst-koblenz" is 34), short enough to remain something a
#: person can read out over the phone, which is the entire point of the column.
SLUG_LENGTH = 80

#: The backfill, as one plain (non-interpolated) statement.
#:
#: German transliteration comes BEFORE the non-alphanumeric sweep, or "Müller"
#: slugifies to "m-ller". ``ß`` expands to two characters, so this is a chain of
#: ``replace`` rather than ``translate``, which is one-to-one only.
#:
#: `base` is the slug the name wants. `rn` orders same-base rows deterministically
#: so the winner is stable across a re-run: first by creation, then by id.
#:
#: Three cases, all of which occur in real data:
#:   * a unique base            -> use it
#:   * a collision              -> suffix the id's first 8 characters
#:   * a name with no usable characters at all ("!!!", or a script this cannot
#:     transliterate) -> fall back to the id prefix alone, because an empty slug
#:     would violate the NOT NULL added below and a bare "-" is not an address
#:
#: Written as a literal rather than built from the constants above: the values are
#: module constants with no caller input, but SQL assembled by string formatting is
#: a habit worth not having in a file that runs as the owner role. The 80 here is
#: ``SLUG_LENGTH``, kept in step by the test that asserts the column width.
BACKFILL = """
WITH slugged AS (
    SELECT
        id,
        created_at,
        nullif(
            trim(both '-' from regexp_replace(
                replace(replace(replace(replace(replace(replace(replace(
                    lower(name),
                'ä','ae'),'ö','oe'),'ü','ue'),'ß','ss'),'á','a'),'é','e'),'è','e'),
                '[^a-z0-9]+', '-', 'g'
            )),
            ''
        ) AS base
    FROM businesses
),
ranked AS (
    SELECT
        id,
        base,
        row_number() OVER (PARTITION BY base ORDER BY created_at, id) AS rn
    FROM slugged
)
UPDATE businesses AS b
SET slug = left(
    CASE
        WHEN r.base IS NULL THEN 'b-' || left(replace(b.id::text, '-', ''), 8)
        WHEN r.rn = 1 THEN r.base
        ELSE r.base || '-' || left(replace(b.id::text, '-', ''), 8)
    END,
    80
)
FROM ranked AS r
WHERE b.id = r.id
"""


#: NO server default, deliberately, and this is a considered exception to the
#: convention in ``ce3e9e923ca5_server_defaults_so_raw_sql_inserts_work``.
#:
#: That convention exists for columns with a natural default -- a status, a locale,
#: an empty JSONB. A slug has none: it is a UNIQUE PUBLIC ADDRESS, and the only
#: default a database could invent is a random string, which would silently hand a
#: business an unreadable permanent URL. Two further costs settled it: Postgres
#: normalises an expression default into its own textual form, so
#: ``compare_server_default=True`` would report drift on this column at EVERY future
#: autogenerate (a permanently noisy diff is how people learn to ignore
#: autogenerate); and a trigger that derived the slug from ``name`` would rewrite a
#: public address behind the caller's back.
#:
#: The cost is that a raw ``INSERT INTO businesses`` must now name the slug. Four
#: test fixtures do; they were updated with the column.


def upgrade() -> None:
    # Three steps, in this order: NOT NULL cannot be added to a column that has no
    # values yet, and UNIQUE cannot be added while duplicates remain.
    op.add_column("businesses", sa.Column("slug", sa.String(SLUG_LENGTH), nullable=True))
    op.execute(BACKFILL)
    op.alter_column("businesses", "slug", nullable=False)
    op.create_unique_constraint("uq_businesses_slug", "businesses", ["slug"])


def downgrade() -> None:
    op.drop_constraint("uq_businesses_slug", "businesses", type_="unique")
    op.drop_column("businesses", "slug")
