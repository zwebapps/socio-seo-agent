"""security-definer resolvers so the public link path drops the privileged connection

Revision ID: 7c1e4a90b2d5
Revises: 5b532c05f131
Create Date: 2026-08-19 16:05:00.000000

Two public, anonymous entry points have to read a row before they know which
tenant it belongs to, which is the one thing RLS cannot express:

* ``/l/{code}`` -- a visitor follows a short link. The code is the only thing that
  names the business.
* the public lead form -- an anonymous submitter posts against a content-piece id,
  and that id is the only thing that names the business.

Until now both ran on the **migration-role connection**, deliberately rationed
(one bound statement, single row, ``READ ONLY`` transaction) but still a second,
privileged pool inside a request served to the public. Worse, it worked only where
that role bypasses RLS: ``FORCE ROW LEVEL SECURITY`` binds the table owner too, so
a deployment whose migration role is a plain owner would 404 every short link
while the rows sat in the table -- invisible locally and in CI, where the role is
a superuser. That failure mode is why ``resolver_can_bypass_rls`` existed.

This migration replaces both with narrow ``SECURITY DEFINER`` functions, the same
posture the project already uses for cross-tenant reads. Each one:

* hard-codes its ``WHERE`` clause and returns at most one row, so there is no
  parameter that can widen what it sees;
* is ``LANGUAGE sql`` with no dynamic SQL, so nothing can be injected into it;
* is ``STABLE`` and reads only -- it cannot write;
* pins ``search_path`` to ``public, pg_temp``. This is not decoration: a
  ``SECURITY DEFINER`` function without a pinned ``search_path`` can be hijacked
  by a caller who creates a same-named object in a schema earlier on their path;
* is ``REVOKE``d from ``PUBLIC`` and granted to ``sma_app`` alone.

**RLS is not weakened for the application role.** Every other read and write in
those requests still runs under ``business_session`` with the business id these
functions return, so a wrong answer here would land writes in the wrong tenant --
which is exactly why the resolvers are tested directly.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1e4a90b2d5"
down_revision: Union[str, Sequence[str], None] = "5b532c05f131"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The role the application connects as. Hard-coded because a migration describes
#: the database at THIS revision, and this is the role ``8ee986b398e9`` created.
APP_ROLE = "sma_app"


RESOLVE_SHORT_LINK = """
CREATE FUNCTION resolve_short_link(p_code varchar)
RETURNS TABLE (
    id uuid, business_id uuid, code varchar, target_url varchar,
    content_piece_id uuid, channel varchar, campaign varchar, click_count integer
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
STABLE
AS $$
    SELECT id, business_id, code, target_url,
           content_piece_id, channel, campaign, click_count
    FROM short_links
    WHERE code = p_code
    LIMIT 1
$$
"""

#: Mirrors ``_RESOLVE_FORM`` in ``adapters/lead_store.py`` exactly, including the
#: ``id AS content_piece_id`` alias the caller reads by name.
#:
#: It deliberately does NOT filter on ``status``. The caller needs to tell "no such
#: piece" apart from "that piece is still a draft" -- collapsing both into an empty
#: result would make an unpublished form indistinguishable from a typo, and the
#: status is returned so the API layer can choose the right answer.
RESOLVE_FORM_TARGET = """
CREATE FUNCTION resolve_form_target(p_piece_id uuid)
RETURNS TABLE (
    business_id uuid, content_piece_id uuid, status varchar, title varchar
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
STABLE
AS $$
    SELECT business_id, id AS content_piece_id, status, title
    FROM content_pieces
    WHERE id = p_piece_id
    LIMIT 1
$$
"""

FUNCTIONS = (
    ("resolve_short_link(varchar)", RESOLVE_SHORT_LINK),
    ("resolve_form_target(uuid)", RESOLVE_FORM_TARGET),
)


def upgrade() -> None:
    for signature, body in FUNCTIONS:
        op.execute(body)
        # Revoke before granting: a function is executable by PUBLIC by default,
        # so creating it and only granting would leave the default in place and the
        # grant would be decoration.
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE}")


def downgrade() -> None:
    for signature, _ in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
