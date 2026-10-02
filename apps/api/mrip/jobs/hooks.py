"""What reconciles external state when a job gives up.

A job that dead-letters has usually left something behind. The clearest case is
an ingestion stage: the *document* is what an officer is actually waiting on, and
without this the document stays in whatever state the pipeline reached. A dead
``document.digitize`` leaves its document at ``classified`` forever —
indistinguishable from one that is merely slow on a 400-page annual report. So
nobody learns the upload failed, the figures it should have produced are quietly
absent, and the officer composing a parliamentary answer has no idea a source is
missing. That silence is the bug this module exists to prevent.

Two decisions are deliberate:

**Registered per job kind, not handled in the worker.** Only the subsystem that
owns a kind knows what external state that kind manages. The worker stays generic
and the ingestion pipeline declares its own reconciliation next to the stages it
declares.

**Fired from :class:`~mrip.jobs.queue.JobQueue`.** That is the one class that can
make a job terminal — both the worker's ``fail`` and the scheduler's
``reclaim_expired`` go through it. Hooking there rather than in the worker means a
future caller cannot dead-letter a job and skip this by forgetting to.

This module imports nothing from the queue or the registry, so the hook a
pipeline registers creates no import cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection

__all__ = [
    "TerminalFailureHook",
    "on_terminal_failure",
    "run_terminal_failure_hook",
]

#: Called with the connection of the transaction that is making the job terminal,
#: the job's payload, and the error text. Runs *inside* that transaction, so the
#: job's death and the reconciliation it implies commit together or not at all.
TerminalFailureHook = Callable[[Connection, dict[str, Any], str], None]

_HOOKS: dict[str, TerminalFailureHook] = {}


def on_terminal_failure(
    kind: str,
) -> Callable[[TerminalFailureHook], TerminalFailureHook]:
    """Register what reconciles external state when a job of ``kind`` gives up.

    Terminal means ``failed`` (a handler said retrying cannot help) or ``dead``
    (the attempts ran out). A job going back to ``pending`` for another attempt is
    not terminal and must not fire this: marking a document failed while a retry
    is still coming would send an officer to re-upload something that is about to
    succeed on its own.
    """

    def register(hook: TerminalFailureHook) -> TerminalFailureHook:
        if kind in _HOOKS and _HOOKS[kind] is not hook:
            raise RuntimeError(
                f"Two terminal-failure hooks registered for job kind {kind!r}: "
                f"{_HOOKS[kind].__qualname__} and {hook.__qualname__}. Silently "
                "keeping one would reconcile whichever module imported last."
            )
        _HOOKS[kind] = hook
        return hook

    return register


def run_terminal_failure_hook(
    conn: Connection, kind: str, payload: dict[str, Any], error: str
) -> bool:
    """Run the hook for ``kind``, if one is registered.

    Returns whether a hook ran. Exceptions are **not** caught: a job recorded as
    dead whose document was left dangling is the failure mode this module exists
    to prevent, so the caller's transaction should roll back and let the job be
    reclaimed rather than commit a half-reconciled state.
    """
    hook = _HOOKS.get(kind)
    if hook is None:
        return False
    hook(conn, payload, error)
    return True


def registered_kinds() -> list[str]:
    """Job kinds with a terminal-failure hook. Test and diagnostic support."""
    return sorted(_HOOKS)
