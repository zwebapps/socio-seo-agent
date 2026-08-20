"""run rejected state

A seventh value for `runs.state`, written only by `POST /api/v1/runs/{id}/reject`.

Its own value rather than a reuse of `partial`: `partial` means a node fell short, and
`rejected` means a person refused the output. Sharing one value would make "how often do
reviewers refuse what we produce" unanswerable in SQL, and would paint a deliberate human
decision as a machine shortfall.

Revision ID: e6a1c3f5b28d
Revises: d4f18a6c93b7
Create Date: 2026-08-20 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e6a1c3f5b28d"
down_revision: Union[str, Sequence[str], None] = "d4f18a6c93b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SIX = "state in ('queued','running','awaiting_approval','done','failed','partial')"
_SEVEN = "state in ('queued','running','awaiting_approval','done','failed','partial','rejected')"


def upgrade() -> None:
    """Widen the state constraint. No data write: no existing row can be `rejected`."""
    op.drop_constraint(op.f("ck_runs_state_valid"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_state_valid"), "runs", _SEVEN)


def downgrade() -> None:
    """Narrow it back, mapping the rows that would otherwise be refused.

    The UPDATE comes FIRST and is not optional: a `rejected` row still present when the
    six-value constraint is created makes the CREATE fail, so the downgrade would break on
    exactly the data it exists to handle.

    It is LOSSY, and knowingly. `partial` is the closest surviving terminal state, so a
    human's "no" comes back indistinguishable in `state` from a node that fell short --
    `finished_reason` still carries the reviewer's words, which is the only part that
    cannot be reconstructed.

    **The FORCE RLS toggle is what makes the UPDATE do anything at all.** `runs` has FORCE
    ROW LEVEL SECURITY and its policy reads `app.current_business_id`, which is unset
    inside a migration -- so `current_setting(..., true)` is NULL, the policy matches no
    row, and a bare UPDATE reports zero rows affected while appearing to succeed. The
    constraint then fails on rows this function believes it has already fixed. The
    core-schema migration (`3b8336ae2975`) states this rule above its RLS block; this is
    it being followed. The toggle is safe here because a migration runs in one transaction
    and FORCE is restored before it commits.
    """
    op.execute("ALTER TABLE runs NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE runs SET state = 'partial' WHERE state = 'rejected'")
    op.execute("ALTER TABLE runs FORCE ROW LEVEL SECURITY")

    op.drop_constraint(op.f("ck_runs_state_valid"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_state_valid"), "runs", _SIX)
