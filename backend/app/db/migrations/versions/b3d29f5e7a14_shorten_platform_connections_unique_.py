"""Rename the platform_connections unique constraint to a name Postgres can hold

`d4f18a6c93b7` asked for
`uq_platform_connections_business_id_platform_external_account_id` -- 64 characters,
one over Postgres's 63-character identifier limit. Because that migration wrapped the
name in `op.f()`, SQLAlchemy treated it as a `conv` label and quietly truncated-and-
hashed it, so the database holds
`uq_platform_connections_business_id_platform_external_a_118f` while the ORM asked for
the full 64. The model's plain-string name took the other path -- validation, not
truncation -- so `alembic check` raised `IdentifierError` before it could compare
anything at all. The project therefore had no model-vs-migration drift guard: a missing
column would not have surfaced until runtime.

This renames the constraint that actually exists to the short name the model now
declares. The rename is by name rather than a drop-and-recreate because recreating a
UNIQUE constraint rebuilds its index and takes an ACCESS EXCLUSIVE lock for the
duration; `RENAME CONSTRAINT` is a catalogue update.

Revision ID: b3d29f5e7a14
Revises: a1c9e4f27b31
Create Date: 2026-08-21 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b3d29f5e7a14"
down_revision: Union[str, Sequence[str], None] = "a1c9e4f27b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: What `\d platform_connections` reports today: SQLAlchemy's truncation of the 64-
#: character name, being the first 56 characters plus four hex digits of its hash. It is
#: spelled out rather than discovered from `pg_constraint` so that a database in some
#: other shape fails loudly here instead of renaming whichever unique constraint a
#: lookup happened to return.
TRUNCATED_NAME = "uq_platform_connections_business_id_platform_external_a_118f"
SHORT_NAME = "uq_platform_connections_business_platform_account"


def upgrade() -> None:
    """Rename the truncated name to the short one the model declares."""
    op.execute(
        f"ALTER TABLE platform_connections RENAME CONSTRAINT {TRUNCATED_NAME} TO {SHORT_NAME}"
    )


def downgrade() -> None:
    """Rename it back.

    Restores the truncation, not the 64-character name that never existed in any
    database -- going back to `d4f18a6c93b7` has to leave behind what that migration
    actually created, or the next upgrade finds nothing to rename.
    """
    op.execute(
        f"ALTER TABLE platform_connections RENAME CONSTRAINT {SHORT_NAME} TO {TRUNCATED_NAME}"
    )
