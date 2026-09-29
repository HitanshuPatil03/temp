"""Structured logging.

One configuration for three processes (api, worker, scheduler) and for every
library that logs through the standard library — uvicorn, alembic, SQLAlchemy —
so a deployment reads one stream in one shape rather than three.

Three decisions are load-bearing.

**Context travels in contextvars, not in arguments.** A request binds
``request_id`` and ``user_id`` once; a job binds ``job_id`` and ``kind`` once.
Every line emitted underneath carries them, including lines from library code
that has never heard of MRIP. Threading a logger through six call frames is how
context gets dropped exactly where an incident needs it.

**Secrets are redacted by a processor, not by discipline.** A key called
``password``, ``token`` or ``*_secret`` is replaced before it reaches a handler.
Relying on every future call site to remember is relying on the one that forgets.

**``console`` in development, ``json`` in production**, and
:meth:`~mrip.config.Settings.check_production_ready` refuses a production profile
that is still on ``console`` — an on-prem deployment whose logs cannot be parsed
is a deployment whose incidents are read by hand.

Logs go to **stdout only**. Nothing in this platform ships telemetry anywhere
(ARCHITECTURE §3); the operator's log collector decides where the stream lands.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.contextvars import bound_contextvars
from structlog.typing import EventDict, Processor, WrappedLogger

from mrip.config import Profile, Settings, get_settings

__all__ = [
    "bind",
    "bound_contextvars",
    "clear",
    "configure_logging",
    "get_logger",
]

#: Keys whose values never belong in a log line. Matched case-insensitively,
#: plus the suffix rules below, so ``MRIP_JWT_SECRET`` and ``refresh_token`` are
#: caught without being enumerated.
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "credential",
        "credentials",
        "jwt",
        "password",
        "passphrase",
        "password_hash",
        "secret",
        "token",
    }
)
_SENSITIVE_SUFFIXES = ("_password", "_secret", "_token", "_key", "_hash")
_REDACTED = "[redacted]"

#: Libraries that are informative at their own default level and deafening at
#: ours. SQLAlchemy's engine logger in particular echoes statements — including
#: fact values — which is the same leak ``MRIP_DB_ECHO`` is refused for in prod.
_LIBRARY_LEVELS: dict[str, int] = {
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "alembic.runtime.migration": logging.INFO,
    "uvicorn.access": logging.WARNING,  # replaced by our own access log
    "multipart": logging.WARNING,
    "pdfminer": logging.WARNING,
    "PIL": logging.WARNING,
}

_configured = False


def _redact(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> MutableMapping[str, Any]:
    """Blank out sensitive values before any handler sees them."""
    for key in list(event_dict):
        lowered = key.lower()
        if lowered in _SENSITIVE_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES):
            event_dict[key] = _REDACTED
    return event_dict


def _shared_processors() -> list[Processor]:
    """Processors applied to MRIP's own events *and* to foreign stdlib records.

    Shared deliberately: a line from uvicorn should carry the same timestamp
    format and the same request id as a line from our own code, or correlating
    them during an incident becomes manual work.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact,
    ]


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Install the logging configuration for this process. Idempotent.

    Called from each entry point (`mrip.main`, `mrip-worker`, `mrip-scheduler`)
    rather than at import time, so importing a module never mutates global
    logging state — that is how a library ends up fighting an application's
    configuration.

    Under the ``test`` profile this is a no-op unless forced: pytest installs its
    own root handler to capture logs, and clearing it would break every
    assertion about log output.
    """
    global _configured
    settings = settings or get_settings()
    if settings.profile is Profile.TEST and not force:
        return
    if _configured and not force:
        return

    shared = _shared_processors()

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            # Hands the event dict to the stdlib handler below, which is what
            # lets one handler render both our events and foreign records.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace rather than append: uvicorn and alembic both install handlers of
    # their own, and keeping them would print every line twice in two shapes.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name, level in _LIBRARY_LEVELS.items():
        logging.getLogger(name).setLevel(level)
    if settings.db_echo:
        # An explicit opt-in outranks the library default above.
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A bound logger. Safe to call at import time, before configuration."""
    return structlog.stdlib.get_logger(name)


def bind(**values: Any) -> None:
    """Attach values to every subsequent log line in this context."""
    structlog.contextvars.bind_contextvars(**values)


def clear() -> None:
    """Drop the bound context. Called at the end of a request or job."""
    structlog.contextvars.clear_contextvars()
