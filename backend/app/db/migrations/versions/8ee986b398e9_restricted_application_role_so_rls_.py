"""restricted application role so RLS actually applies

Revision ID: 8ee986b398e9
Revises: 3b8336ae2975
Create Date: 2026-08-18 21:27:00.636248

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ee986b398e9"
down_revision: str | Sequence[str] | None = "3b8336ae2975"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the restricted runtime role and grant it DML only.

    Why this migration exists, and it is not cosmetic:

    The role that owns these tables is a superuser in local and CI environments
    (the postgres image makes POSTGRES_USER a superuser). **A superuser bypasses
    row-level security entirely, and BYPASSRLS ignores FORCE.** So with only the
    owner role, the nine tenant_isolation policies from the previous migration
    have no effect whatsoever -- and an isolation test written against that role
    would pass while proving nothing at all.

    The application therefore connects as ``sma_app``: no superuser, no
    BYPASSRLS, not the table owner. Migrations continue to run as the owner,
    which is what lets DDL and data fixes work.

    In production this role is provisioned by infrastructure with a real
    password; the guarded CREATE ROLE below exists so a local or CI database
    bootstraps itself.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sma_app') THEN
                CREATE ROLE sma_app LOGIN PASSWORD 'sma_app';
            END IF;
        END
        $$;
        """
    )

    op.execute("GRANT USAGE ON SCHEMA public TO sma_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sma_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sma_app")

    # Tables created by later migrations must be reachable without another grant
    # step, or a new table silently 403s for the running application.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sma_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO sma_app"
    )

    # Deliberately NOT granted: CREATE on the schema, ownership of any table,
    # and any DDL. The runtime cannot alter the shape of the database, so it
    # cannot drop a policy that constrains it.


def downgrade() -> None:
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM sma_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM sma_app")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sma_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM sma_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM sma_app")
    # The role itself is left in place: it may own objects in other databases on
    # the same cluster, and dropping it here would be a surprise.
