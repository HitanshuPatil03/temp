"""Database layer: schema, engine, repositories and the store facade.

Re-exported here so callers write ``from mrip.db import Store`` exactly as they
did against the DuckDB implementation this package replaces.
"""

from mrip.db.engine import (
    advisory_lock,
    dispose_engine,
    get_engine,
    ping,
    read_only,
    transaction,
)
from mrip.db.store import Store, new_id, read_only_store, store_session
from mrip.db.tables import METADATA

__all__ = [
    "METADATA",
    "Store",
    "advisory_lock",
    "dispose_engine",
    "get_engine",
    "new_id",
    "ping",
    "read_only",
    "read_only_store",
    "store_session",
    "transaction",
]
