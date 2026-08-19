"""make RLS policies null-safe

Revision ID: 5b532c05f131
Revises: 9281c4db5cd6
Create Date: 2026-08-19 14:16:38.525395

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "5b532c05f131"
down_revision: Union[str, Sequence[str], None] = "9281c4db5cd6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every business-scoped table. Listed rather than derived because a migration must
#: describe the database at THIS revision, not whatever the ORM says later.
TENANT_TABLES = (
    "documents",
    "kb_chunks",
    "crawl_pages",
    "runs",
    "run_events",
    "model_usage",
    "actions",
    "opportunities",
    "content_pieces",
    "geo_prompts",
    "geo_results",
    "short_links",
    "link_clicks",
    "leads",
    "feedback",
    "learned_style",
)


def upgrade() -> None:
    """Rebuild every tenant policy so an unset tenant means ZERO ROWS, not an error.

    The bug, found by probing the running database rather than by reading the code:

    `set_config('app.current_business_id', ..., true)` is transaction-local, so the value
    is dropped at COMMIT. But the GUC does not become *unset* — it becomes the EMPTY
    STRING. The policies cast it directly:

        business_id = current_setting('app.current_business_id', true)::uuid

    and `''::uuid` raises `invalid input syntax for type uuid: ""`. So the first unscoped
    query on a pooled connection that had previously served a scoped transaction returns
    a 500 instead of zero rows.

    That is worse than it looks in three ways. It only happens on a RECYCLED connection,
    so it is load-dependent and absent from a fresh test run. It surfaces as a database
    error rather than as an authorisation result, so it reads like an outage. And it
    inverts the intended failure direction: the whole design of these policies is that
    forgetting to scope a session shows nothing, safely — an exception is a louder
    failure, but it is also a denial of service on any endpoint that ever reads
    unscoped.

    `nullif(..., '')` maps both the unset and the emptied case to NULL, and
    `business_id = NULL` is NULL, which the policy treats as false. Zero rows, no error,
    which is what was always meant.
    """
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (business_id = nullif(current_setting('app.current_business_id', true), '')::uuid) "
            "WITH CHECK (business_id = nullif(current_setting('app.current_business_id', true), '')::uuid)"
        )


def downgrade() -> None:
    """Restore the unsafe cast. Kept faithful rather than convenient: a downgrade that
    quietly leaves an improvement in place makes the migration history a lie."""
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (business_id = current_setting('app.current_business_id', true)::uuid) "
            "WITH CHECK (business_id = current_setting('app.current_business_id', true)::uuid)"
        )
