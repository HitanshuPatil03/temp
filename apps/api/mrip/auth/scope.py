"""Row-level access scope.

Scope answers *which entities* a principal may see. It is separate from role,
which answers *what they may do*: a reviewer at SECL and a reviewer at MCL hold
the same role and must not see the same rows.

The important design choice is that :class:`Scope` is a **required argument** to
every repository read that returns entity-bearing rows. It is not an optional
filter with a permissive default, because a filter you can forget to pass is a
filter you will forget to pass — and the failure mode is an SECL officer reading
MCL's unpublished production figures. Revision 1 of the architecture deferred this
to a late phase; that was the mistake this module corrects.

:meth:`Scope.unrestricted` exists and is deliberately verbose at the call site. It
means "this caller has been considered and is intentionally unscoped" — a
migration, a worker processing its own job, an admin report — and it greps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import sqlalchemy as sa

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["SCOPE_ALL", "Scope"]

#: Grant sentinel meaning "every entity". Stored as an ordinary ``user_scopes``
#: row so that revoking HQ-wide access is the same operation as revoking any
#: single subsidiary.
SCOPE_ALL = "*"


@dataclass(frozen=True, slots=True)
class Scope:
    """The set of entity ids a principal may read."""

    entity_ids: frozenset[str]
    #: True when this scope was granted rather than inferred. Only used for the
    #: audit trail — enforcement does not branch on it.
    unrestricted_reason: str | None = None

    # ------------------------------------------------------------ constructors

    @classmethod
    def of(cls, *entity_ids: str) -> Self:
        """Scope to an explicit set of entities."""
        if not entity_ids:
            raise ValueError(
                "Scope.of() with no entities would match nothing. Use "
                "Scope.nothing() if that is genuinely intended."
            )
        return cls(frozenset(entity_ids))

    @classmethod
    def unrestricted(cls, reason: str) -> Self:
        """Every entity. The ``reason`` is mandatory and is recorded in the audit
        trail, so an unscoped read is always attributable to a decision."""
        if not reason.strip():
            raise ValueError("An unrestricted scope must state why.")
        return cls(frozenset({SCOPE_ALL}), unrestricted_reason=reason)

    @classmethod
    def nothing(cls) -> Self:
        """Matches no rows. What a deactivated or ungranted account gets — an
        empty result, never an unfiltered one."""
        return cls(frozenset())

    @classmethod
    def from_grants(cls, entity_ids: Iterable[str], *, granted_to: str) -> Self:
        """Build a scope from a principal's ``user_scopes`` rows.

        A grant of :data:`SCOPE_ALL` becomes an unrestricted scope whose reason
        names the account that holds it, so an audit row for an HQ-wide read says
        *whose* HQ grant permitted it rather than merely "unrestricted".

        No grants at all yields :meth:`nothing`. An account that exists but has
        been given no scope reads an empty corpus — the alternative, treating
        "unspecified" as "everything", is the failure this class exists to
        prevent.
        """
        ids = frozenset(entity_ids)
        if SCOPE_ALL in ids:
            return cls(
                frozenset({SCOPE_ALL}),
                unrestricted_reason=f"HQ-wide grant held by {granted_to}",
            )
        return cls(ids)

    # -------------------------------------------------------------- predicates

    @property
    def is_unrestricted(self) -> bool:
        return SCOPE_ALL in self.entity_ids

    @property
    def is_empty(self) -> bool:
        return not self.entity_ids

    def allows(self, entity_id: str | None) -> bool:
        """Whether a single entity is readable under this scope.

        A ``None`` entity — a document not yet attributed to a subsidiary — is
        visible only to an unrestricted scope. Treating unattributed data as
        public is the wrong default for a need-to-know system.
        """
        if self.is_unrestricted:
            return True
        return entity_id is not None and entity_id in self.entity_ids

    # ------------------------------------------------------------------ SQL

    def clause(self, column: sa.ColumnElement[str | None]) -> sa.ColumnElement[bool]:
        """A ``WHERE`` fragment restricting ``column`` to this scope.

        Returns a literal false for an empty scope rather than omitting the
        predicate: the safe degenerate case is "no rows", not "all rows".
        """
        if self.is_unrestricted:
            return sa.true()
        if self.is_empty:
            return sa.false()
        return column.in_(sorted(self.entity_ids))

    def __str__(self) -> str:
        if self.is_unrestricted:
            return f"all entities ({self.unrestricted_reason})"
        if self.is_empty:
            return "no entities"
        return ", ".join(sorted(self.entity_ids))
