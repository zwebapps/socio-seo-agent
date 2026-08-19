"""a security-definer resolver for the public landing page

Revision ID: 4d2b7f9c1e83
Revises: b5e73c1a8f42
Create Date: 2026-08-19 18:20:00.000000

CONVERSION is the third link of the lead chain, and it is the one with no public
surface until now: a tracked short link pointing at a page that does not exist
earns nothing. ``GET /p/{piece_id}`` serves that page, and it is anonymous by
necessity -- a stranger arriving from an Instagram bio has no session and no
business context, so **the id in the URL is the only thing that names the tenant**.

``content_pieces`` has ``ENABLE`` *and* ``FORCE ROW LEVEL SECURITY`` with a policy
keyed on ``app.current_business_id``, so with that GUC unset the restricted
application role reads zero rows and raises nothing. This is therefore the third
member of the same family as ``resolve_short_link`` and ``resolve_form_target``
(migration ``7c1e4a90b2d5``), and it follows that family's rules exactly:

* it hard-codes its ``WHERE`` clause and returns at most one row, so no parameter
  can widen what it sees;
* it is ``LANGUAGE sql`` with no dynamic SQL, so nothing can be injected into it;
* it is ``STABLE`` and reads only -- it cannot write;
* it pins ``search_path`` to ``public, pg_temp``, because a ``SECURITY DEFINER``
  function without one can be hijacked by a caller who creates a same-named object
  in a schema earlier on their path;
* it is ``REVOKE``d from ``PUBLIC`` and granted to ``sma_app`` alone.

**RLS is not weakened for the application role.** No privileged connection is
opened; this runs on the ordinary restricted session, and every read and write that
follows the lookup uses ``business_session`` with the business id it returned.

Three specific decisions worth defending, because a reviewer should be able to
check the argument rather than take it on trust:

**It does not filter on ``status``.** The caller needs to tell "no such piece" from
"that piece is still a draft", and collapsing both into an empty result would make
an unapproved page indistinguishable from a typo. The route answers both with the
same 404 -- that choice belongs to the endpoint, where a reader will look for it,
not to the database.

**It does not filter on ``surface`` either**, for the same reason: the route refuses
anything that is not a landing page, and it needs to know the difference in order
to keep that refusal indistinguishable from the others.

**It joins ``businesses``**, which is legitimate and not a widening: that table is
the tenant table itself and carries no RLS policy (the same reasoning
``lead_store.business_name`` documents), and the page needs the business's display
name and locale to render at all. Only ``name`` and ``locale`` are returned -- not
the owner, not the DNA, not the website -- because a public page has no business
knowing the rest.

No table changes: ``content_pieces`` already has everything a landing page needs
(``surface``, ``title``, ``slug``, ``body_md``, a JSONB ``meta`` for the spec, and a
``status`` the approval flow already uses), so inventing a second table for one
would have split "a produced artifact" across two places.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d2b7f9c1e83"
down_revision: Union[str, Sequence[str], None] = "b5e73c1a8f42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The role the application connects as. Hard-coded because a migration describes
#: the database at THIS revision, and this is the role ``8ee986b398e9`` created.
APP_ROLE = "sma_app"

SIGNATURE = "resolve_landing_page(uuid)"

RESOLVE_LANDING_PAGE = """
CREATE FUNCTION resolve_landing_page(p_piece_id uuid)
RETURNS TABLE (
    business_id uuid, content_piece_id uuid, status varchar, surface varchar,
    title varchar, slug varchar, meta jsonb, business_name varchar, locale varchar
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
STABLE
AS $$
    SELECT cp.business_id,
           cp.id AS content_piece_id,
           cp.status,
           cp.surface,
           cp.title,
           cp.slug,
           cp.meta,
           b.name AS business_name,
           b.locale
    FROM content_pieces AS cp
    JOIN businesses AS b ON b.id = cp.business_id
    WHERE cp.id = p_piece_id
    LIMIT 1
$$
"""


def upgrade() -> None:
    op.execute(RESOLVE_LANDING_PAGE)
    # Revoke before granting: a function is executable by PUBLIC by default, so
    # creating it and only granting would leave the default in place and the grant
    # would be decoration.
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
