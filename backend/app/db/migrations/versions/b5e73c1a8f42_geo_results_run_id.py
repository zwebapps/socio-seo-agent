"""geo_results.run_id, so a probe run has an identity instead of a time window

Revision ID: b5e73c1a8f42
Revises: 9a4f21c7de83
Create Date: 2026-08-19 17:20:00.000000

``geo_results`` carried no run id, so ``adapters/probe_store.py`` had to define a
run BY ITS TIMESTAMP: one save writes one ``probed_at`` across the batch, and a
re-send within ``RUN_DEDUPE_WINDOW`` (six hours) is treated as a retry of that run
rather than a new one.

That heuristic exists for a good reason -- without it, a worker that died halfway
through and re-ran would report twelve answers to six questions, and share of voice
would be computed from a sample that never happened. But it is a heuristic, and the
module says so: "the window is the honest cost of having no run id". Two ways it is
wrong. A legitimate re-probe inside six hours (an operator investigating a bad
result) silently folds into the previous run instead of recording a new one. And a
run that straddles the window boundary can split itself in two.

This adds the identity. ``probed_at`` STAYS -- it is the run's time, which is
independently useful and already indexed -- but it is no longer asked to double as
the run's name.

**Nullable, and no backfill of a synthetic id.** Existing rows genuinely have no run
identity; inventing one (grouping by ``probed_at`` and minting a UUID per group)
would manufacture a fact that was never recorded, and the timestamp grouping it
would rest on is exactly the approximation being replaced. Old rows keep resolving
by timestamp; new ones carry an id. Nothing about this migration is a
``geo_results`` -> ``runs`` foreign key: probing is not driven from an agent run
today (``geo_service`` has no caller outside tests), so an FK would assert a
relationship the product does not have and would refuse every standalone probe.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b5e73c1a8f42"
down_revision: Union[str, Sequence[str], None] = "9a4f21c7de83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "geo_results",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Indexed because it becomes the lookup key for "this run's rows", which is what
    # `latest_share_of_voice` and the retry-vs-new-run decision both do. Composite
    # with business_id: every read is already tenant-scoped by RLS, so the planner
    # wants both columns together rather than run_id alone.
    op.create_index(
        "ix_geo_results_business_run",
        "geo_results",
        ["business_id", "run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_geo_results_business_run", table_name="geo_results")
    op.drop_column("geo_results", "run_id")
