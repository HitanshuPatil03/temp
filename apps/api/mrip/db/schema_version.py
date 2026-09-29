"""Schema version checks.

A process that serves requests against a database one migration behind will
return answers that are quietly wrong — a column the code reads as canonical
tonnes that the schema still holds as raw, and nothing in the response says so.
So the API and the workers both refuse to start out of step with the migration
history rather than discovering it at the first bad query.

This is a *check*, not an auto-migrate. Applying migrations is a deliberate,
separately-runnable step (the ``migrate`` service in docker-compose), because a
schema change on a live corpus is an operator's decision, not a side effect of a
deploy restarting a container.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from mrip.db.engine import get_engine

__all__ = ["SchemaVersion", "check_schema_version", "require_schema_version"]

#: apps/api/mrip/db/schema_version.py -> up three levels is apps/api.
_API_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class SchemaVersion:
    applied: str | None
    expected: str | None

    @property
    def is_current(self) -> bool:
        return self.applied is not None and self.applied == self.expected

    def describe(self) -> str:
        if self.applied is None:
            return (
                "The database has no Alembic version stamp — it has never been "
                "migrated. Run: alembic upgrade head"
            )
        if self.expected is None:
            return "No migration scripts were found; the history is missing."
        if self.applied != self.expected:
            return (
                f"The database is at revision {self.applied!r} but this code "
                f"expects {self.expected!r}. Run: alembic upgrade head"
            )
        return f"Schema is at {self.applied!r}."


def _script_directory() -> ScriptDirectory:
    config = Config(str(_API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_API_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def check_schema_version() -> SchemaVersion:
    """Compare the database's stamp against the migration history's head."""
    with get_engine().connect() as conn:
        applied = MigrationContext.configure(conn).get_current_revision()
    return SchemaVersion(applied=applied, expected=_script_directory().get_current_head())


def require_schema_version() -> SchemaVersion:
    """Raise unless the database is at the expected revision."""
    version = check_schema_version()
    if not version.is_current:
        raise RuntimeError(f"Refusing to start. {version.describe()}")
    return version
