"""Engine and connection management.

One engine per process, built from :class:`~mrip.config.Settings`. Two details
here exist because of how this system is deployed:

**A statement timeout is set on every connection.** A runaway aggregate over a
multi-year fact store should be killed by the database, not left to hold a
connection until an operator notices. Workers override it, because OCR-driven
inserts legitimately take longer than an interactive query.

**Transactions are explicit.** ``with transaction() as conn`` commits on success
and rolls back on any exception. There is no autocommit path, because the
ingestion state machine depends on a stage's output and its state transition
landing together or not at all (ARCHITECTURE §6).
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine, event
from sqlalchemy.pool import NullPool

from mrip.config import Settings, get_settings

__all__ = [
    "advisory_lock",
    "dispose_engine",
    "get_engine",
    "ping",
    "read_only",
    "transaction",
]


def _build_engine(settings: Settings, *, poolclass: Any = None) -> Engine:
    engine = sa.create_engine(
        settings.sync_database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        poolclass=poolclass,
        **(
            {}
            if poolclass is NullPool
            else {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_recycle": 1800,
            }
        ),
    )

    timeout_ms = settings.db_statement_timeout_ms
    if timeout_ms > 0:

        @event.listens_for(engine, "connect")
        def _set_timeouts(dbapi_connection: Any, _record: Any) -> None:
            # Set on the raw connection so it applies to every statement,
            # including those SQLAlchemy issues on our behalf.
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {timeout_ms}")
                # Never let a lock wait outlive the statement budget.
                cursor.execute(f"SET lock_timeout = {timeout_ms}")
                cursor.execute("SET idle_in_transaction_session_timeout = 60000")

    return engine


@functools.lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine."""
    return _build_engine(get_settings())


def dispose_engine() -> None:
    """Drop the pooled connections and forget the engine.

    Needed by tests, which point successive cases at different databases, and by
    a forking worker parent, which must not hand live sockets to its children.
    """
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


@contextmanager
def transaction() -> Iterator[Connection]:
    """A transactional connection: commit on success, roll back on exception."""
    with get_engine().begin() as conn:
        yield conn


@contextmanager
def read_only() -> Iterator[Connection]:
    """A read-only transaction.

    Marked read-only at the database rather than by convention, so a query path
    that was never meant to write cannot — this is how the exact-figure route
    proves it only reads (ARCHITECTURE §7).
    """
    with get_engine().connect() as conn:
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        try:
            yield conn
        finally:
            conn.rollback()


@contextmanager
def advisory_lock(key: str, *, blocking: bool = False) -> Iterator[bool]:
    """Hold a session-level advisory lock for the duration of the block.

    Used by the scheduler so a second instance becomes a no-op instead of a
    duplicate-job bug. Yields whether the lock was acquired; with
    ``blocking=False`` a losing caller gets ``False`` immediately rather than
    waiting behind the winner.
    """
    # hashtext gives a stable 32-bit key from a readable name, so the lock is
    # identifiable in pg_locks without maintaining a registry of magic integers.
    with get_engine().connect() as conn:
        if blocking:
            conn.execute(sa.text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": key})
            acquired = True
        else:
            acquired = bool(
                conn.execute(
                    sa.text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}
                ).scalar()
            )
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(
                    sa.text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key}
                )
            conn.commit()


def ping() -> bool:
    """Whether the database answers. Backs the readiness probe."""
    try:
        with get_engine().connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception:
        return False
    return True
