"""Alembic environment.

The connection URL comes from :class:`mrip.config.Settings`, not from
``alembic.ini``. There is one place a DSN is configured, so ``alembic upgrade
head`` and the application can never migrate one database while serving another.

``compare_type`` and ``compare_server_default`` are on: a column whose type drifts
from the metadata is exactly the kind of thing that silently breaks a fact store,
and autogenerate should catch it rather than shrug.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context

from mrip.config import get_settings
from mrip.db.tables import METADATA

config = context.config
target_metadata = METADATA


def _url() -> str:
    # An explicit -x url=... wins, so CI can migrate a throwaway database without
    # exporting settings for the whole process.
    return (
        context.get_x_argument(as_dictionary=True).get("url")
        or get_settings().sync_database_url
    )


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Keep autogenerate focused on tables this project owns.

    Extension-created objects (pgvector's, pg_stat_statements') are reflected from
    the live database but are not in our metadata, so without this filter every
    autogenerate would propose dropping them.
    """
    return not (
        type_ == "table" and name is not None and name.startswith(("pg_", "sql_"))
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of applying it.

    This is how a migration is handed to a DBA in an environment where the
    application account may not hold DDL rights — a real constraint in government
    deployments.
    """
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = sa.create_engine(_url(), poolclass=sa.pool.NullPool, future=True)

    with engine.connect() as connection:
        # pgvector is needed by the retrieval tables (roadmap Phase 4.1). Created
        # here rather than in a revision so a fresh database is usable at any
        # revision, and because CREATE EXTENSION is idempotent.
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
            # Wrap the whole upgrade in one transaction: a half-applied schema
            # change is not a state this system should ever be in.
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
