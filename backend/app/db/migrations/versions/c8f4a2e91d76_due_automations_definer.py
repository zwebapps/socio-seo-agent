"""due_automations(): the one cross-business read the scheduler needs

A worker asking "whose automation is due?" is asking a question no tenant-scoped
session can answer. ``automation_settings`` has ENABLE *and* FORCE ROW LEVEL SECURITY
with a policy keyed on ``app.current_business_id``, so with that GUC unset the
restricted application role reads zero rows and raises nothing -- the scan would
silently find no work forever, which is the worst possible failure for a scheduler:
green logs and nothing ever runs.

So this is the fourth member of the family ``resolve_short_link``,
``resolve_form_target`` (``7c1e4a90b2d5``) and ``resolve_landing_page``
(``4d2b7f9c1e83``) belong to, and it follows that family's rules exactly:

* it hard-codes its ``WHERE`` clause, and **takes no parameters at all**, so nothing
  a caller passes can widen what it sees -- not even the clock. `now()` is read
  inside the function, which also means a test controls it by setting
  ``next_run_at`` rather than by lying about the time;
* it is ``LANGUAGE sql`` with no dynamic SQL, so nothing can be injected into it;
* it is ``STABLE`` and reads only -- it cannot write, so a scheduler bug cannot
  advance anybody's schedule through this function;
* it pins ``search_path`` to ``public, pg_temp``, because a ``SECURITY DEFINER``
  function without one can be hijacked by a caller who creates a same-named object
  in a schema earlier on their path;
* it is ``REVOKE``d from ``PUBLIC`` and granted to ``sma_app`` alone.

**RLS is not weakened for the application role.** No privileged connection is opened.
The worker learns WHICH businesses are due from this function and then does every
read and write for each one through ``business_session(business_id)`` -- so the run it
creates, and the checkpoint it writes, are tenant-scoped in the ordinary way.

It returns the schedule fields as well as the id, deliberately: the alternative is a
second per-business read for values this row already has, and the worker would then be
holding a schedule it fetched at a different moment from the one it decided on.

Revision ID: c8f4a2e91d76
Revises: f2a7d61b40c8
Create Date: 2026-08-21 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c8f4a2e91d76"
down_revision: Union[str, Sequence[str], None] = "f2a7d61b40c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The role the application connects as. Hard-coded because a migration describes the
#: database at THIS revision, and this is the role `8ee986b398e9` created.
APP_ROLE = "sma_app"

SIGNATURE = "due_automations()"
STRANDED_SIGNATURE = "stranded_runs(integer)"

DUE_AUTOMATIONS = """
CREATE FUNCTION due_automations()
RETURNS TABLE (
    business_id uuid,
    cadence text,
    day_of_week integer,
    hour integer,
    timezone text,
    channels jsonb,
    goal_template text,
    last_run_at timestamptz,
    next_run_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT s.business_id,
           s.cadence::text,
           s.day_of_week,
           s.hour,
           s.timezone::text,
           s.channels,
           s.goal_template,
           s.last_run_at,
           s.next_run_at
    FROM automation_settings AS s
    JOIN businesses AS b ON b.id = s.business_id
    WHERE s.mode = 'scheduled_draft'
      AND s.paused_reason IS NULL
      AND s.next_run_at IS NOT NULL
      AND s.next_run_at <= now()
    ORDER BY s.next_run_at ASC
    LIMIT 100
$$
"""

#: The join to `businesses` is not decoration. An automation row whose business has
#: been removed would otherwise be selected forever, and the run creation would fail
#: on a foreign key every cycle -- a scheduler that spends its whole budget retrying a
#: business that no longer exists. LIMIT is a bound on one cycle's work for the same
#: reason `list_unsettled_orders` has one: accumulated garbage must not be able to
#: starve the real work behind it.


STRANDED_RUNS = """
CREATE FUNCTION stranded_runs(p_older_than_minutes integer)
RETURNS TABLE (id uuid, business_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT r.id, r.business_id
    FROM runs AS r
    WHERE r.state = 'running'
      AND r.updated_at < now() - make_interval(mins => greatest(p_older_than_minutes, 1))
    ORDER BY r.updated_at ASC
    LIMIT 200
$$
"""

#: The second definer, and it is a READ even though the sweep is a write.
#:
#: `runs` has FORCE ROW LEVEL SECURITY too, so an unscoped `UPDATE runs SET state =
#: 'failed'` matches ZERO rows and reports success -- the same silent no-op as the
#: automation scan, in the direction that is harder to notice, because a sweep that
#: cleans nothing looks exactly like a sweep with nothing to clean. So the scheduler
#: LEARNS which runs are stranded through this function and then writes each one through
#: `business_session(business_id)`, which keeps every mutation tenant-scoped.
#:
#: A writing SECURITY DEFINER would have been shorter and is deliberately not what this
#: is: a definer that can UPDATE is a function that can change any tenant's data if it is
#: ever called with the wrong argument, and the whole family this belongs to is STABLE and
#: read-only for that reason.
#:
#: The age bound is a parameter because the caller owns the policy (`STRANDED_AFTER` in
#: `worker/scheduler.py`), and it cannot widen what the function sees beyond
#: `state = 'running'` -- the clamp to at least one minute stops a zero or a negative
#: turning this into "every run in flight".


def upgrade() -> None:
    op.execute(DUE_AUTOMATIONS)
    op.execute(STRANDED_RUNS)
    op.execute(f"REVOKE ALL ON FUNCTION {STRANDED_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {STRANDED_SIGNATURE} TO {APP_ROLE}")
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {STRANDED_SIGNATURE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
