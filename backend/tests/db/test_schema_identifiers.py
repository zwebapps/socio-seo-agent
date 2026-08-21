"""Every constraint and index name must fit a Postgres identifier.

Postgres truncates an identifier longer than 63 characters (`NAMEDATALEN - 1`) without
complaining. SQLAlchemy's two kinds of name take that limit very differently:

* a name produced by the metadata's naming convention is a `conv` label, which
  SQLAlchemy truncates and hashes itself, so the ORM and the database agree;
* a name given as a plain string is passed to `validate_identifier`, which raises
  `IdentifierError` instead.

`platform_connections` hit the second case. Its `UniqueConstraint` was named with the
64-character string the convention would have produced, so the database ended up with
the truncation the migration wrote through `op.f()` and the ORM metadata kept the full
name -- and `alembic check` could not get as far as comparing the two schemas. The
project's model-vs-migration drift guard was off, silently, for as long as that name
stood: a column present in the models and missing from the database would not have
shown up until it was queried in production.

So this asserts the limit over the whole metadata rather than over one table. It is a
pure-metadata test with no database: the failure it guards against is a naming mistake
made while writing a model, and it should surface on a laptop with nothing running.
"""

from sqlalchemy import Table

from backend.app.db import models  # noqa: F401  -- import registers every table
from backend.app.db.base import Base

#: Postgres's `NAMEDATALEN` is 64, and an identifier is NUL-terminated, so 63 characters
#: is the most that survives a round trip intact.
MAX_IDENTIFIER_LENGTH = 63


def _named_objects(table: Table) -> list[tuple[str, str]]:
    """Every named constraint and index on `table`, as `(kind, name)`."""
    named: list[tuple[str, str]] = [
        ("constraint", str(c.name)) for c in table.constraints if c.name is not None
    ]
    named += [("index", str(i.name)) for i in table.indexes if i.name is not None]
    return named


def test_every_constraint_and_index_name_fits_a_postgres_identifier() -> None:
    """A name over 63 characters is a bug, whether Postgres or SQLAlchemy notices it.

    Reported as a complete list rather than one failure at a time: renaming a
    constraint costs a migration, and finding all of them in one run is the difference
    between one migration and several.
    """
    too_long = [
        f"{table.name}.{name} ({kind}, {len(name)} chars)"
        for table in Base.metadata.tables.values()
        for kind, name in _named_objects(table)
        if len(name) > MAX_IDENTIFIER_LENGTH
    ]

    assert not too_long, (
        f"{len(too_long)} identifier(s) exceed Postgres's {MAX_IDENTIFIER_LENGTH}-character "
        "limit. Postgres would truncate them and the ORM would not, which breaks "
        "`alembic check` and with it the drift guard:\n  " + "\n  ".join(sorted(too_long))
    )


def test_the_metadata_actually_contains_names_to_check() -> None:
    """Guard the guard.

    The assertion above passes trivially if the models fail to import or the loop finds
    nothing -- a test that could never fail is worse than no test. `platform_connections`
    is named specifically because it is the table that caused this, and its unique
    constraint is the one whose name had to shrink.
    """
    names = [name for t in Base.metadata.tables.values() for _, name in _named_objects(t)]

    assert len(names) > 50, f"expected the whole schema's names, found {len(names)}"
    assert "uq_platform_connections_business_platform_account" in names
