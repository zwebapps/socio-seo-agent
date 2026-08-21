"""runs.channels — the channel set a run targets

Per-run channel selection. Until now the channels a run rendered posts for were a
`NodeDeps` default (`linkedin`, `facebook`, `instagram`) that the one production
construction site never overrode, so the seam existed and nothing could use it.

The column is on `runs` rather than derived at execution time for two reasons. A
resumed run must target what it was started for -- rebuilding the set from the
current default means a run started for LinkedIn alone comes back targeting three
channels -- and "which channels did this run target" has to stay answerable in SQL
after the default set changes.

Revision ID: a1c9e4f27b31
Revises: e6a1c3f5b28d
Create Date: 2026-08-21 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "a1c9e4f27b31"
down_revision: Union[str, Sequence[str], None] = "e6a1c3f5b28d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the column, defaulted.

    NOT NULL with a server default, so the backfill is the default itself and no UPDATE
    runs -- which is also why this migration needs no `NO FORCE ROW LEVEL SECURITY`
    toggle. `runs` has FORCE RLS and its policy reads `app.current_business_id`, unset
    inside a migration, so a bare UPDATE here would silently affect zero rows (see
    `e6a1c3f5b28d`'s downgrade for that hazard in full). An ALTER is not subject to it.

    An empty array on an existing row is correct rather than a gap: empty means "nobody
    chose", which is exactly true of every run started before this column existed, and
    the executor resolves it to the default set at `new_state`.
    """
    op.add_column(
        "runs",
        sa.Column(
            "channels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Drop it. Lossy: which channels a past run targeted is not reconstructable."""
    op.drop_column("runs", "channels")
