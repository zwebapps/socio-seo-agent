"""sampling policies and node tool policies

Revision ID: c7a41d92b6f0
Revises: 4d2b7f9c1e83
Create Date: 2026-08-19 18:05:00.000000

Two platform-configuration tables for the `/developer` console.

``sampling_policies`` is deliberately NOT two extra columns on ``model_routes``, even
though both are keyed on task class. ``RouteConfigWriter.set_route`` DELETES the route
row when the chain is empty -- "use the default chain" and "use nothing" are different
intentions -- so sampling settings living on that row would be silently discarded the
moment an operator reverted a route to its default. Two tables, two lifecycles.

``node_tool_policies`` has a ``revoked`` column and NO ``granted`` column, and that
absence is a security property rather than an omission: ``agents/tools.NODE_TOOLS`` is a
prompt-injection barrier (docs/AGENT_RUNTIME.md section 3), and the effective set is that
allowlist MINUS whatever is stored here. Because the only operation expressible is a set
difference, nothing written to this table can grant a capability the code did not already
grant. Adding a ``granted`` column later would make widening reachable from a browser and
would need its own decision.

NO row-level security on either table, the same as ``model_routes`` and
``provider_settings``: neither carries a ``business_id``, because both are the operator's
cost-and-safety decisions rather than customer data. The tenant-isolation suite derives
its table list from the ORM, so it will not expect a policy here. The restricted
``sma_app`` role reaches both through the ``ALTER DEFAULT PRIVILEGES`` grant installed by
``8ee986b398e9``, so no explicit GRANT is needed.

Both tables are seeded EMPTY on purpose, and that is what makes this safe to deploy ahead
of the screens: an empty ``sampling_policies`` means nothing is sent and the provider
default applies -- exactly what every call site does today -- and an empty
``node_tool_policies`` means the code allowlist is used unchanged.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c7a41d92b6f0"
down_revision: Union[str, Sequence[str], None] = "4d2b7f9c1e83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sampling_policies",
        sa.Column("task_class", sa.String(length=32), nullable=False),
        sa.Column("temperature", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # The bounds are enforced in Python by `llm/sampling.py` AND here. The
        # duplication is deliberate: Python refuses a bad request, and this refuses a bad
        # ROW -- including one written by a fixture, a psql session or a later migration.
        sa.CheckConstraint(
            "temperature is null or (temperature >= 0 and temperature <= 1)",
            name=op.f("ck_sampling_policies_temperature_in_range"),
        ),
        sa.CheckConstraint(
            "max_output_tokens is null or "
            "(max_output_tokens >= 1024 and max_output_tokens <= 8192)",
            name=op.f("ck_sampling_policies_max_output_tokens_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_sampling_policies_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sampling_policies")),
        sa.UniqueConstraint("task_class", name=op.f("uq_sampling_policies_task_class")),
    )
    op.create_table(
        "node_tool_policies",
        sa.Column("node", sa.String(length=32), nullable=False),
        sa.Column(
            "revoked",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_node_tool_policies_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_tool_policies")),
        sa.UniqueConstraint("node", name=op.f("uq_node_tool_policies_node")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("node_tool_policies")
    op.drop_table("sampling_policies")
