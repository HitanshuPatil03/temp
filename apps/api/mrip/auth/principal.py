"""The authenticated caller.

A :class:`Principal` is the answer to both access questions at once, which is why
they travel together:

- **what** may this caller do — the :class:`~mrip.schemas.Role`, a rank;
- **which rows** may they see — the :class:`~mrip.auth.scope.Scope`, row-level.

Keeping them separate objects on one principal is deliberate. They are different
kinds of thing: an *approver* in SECL and an *approver* in MCL have identical
powers over entirely different data, and every real access bug in a reporting
system is one of the two silently standing in for the other.

This module is pure: it raises :class:`AccessDeniedError`, never an
``HTTPException``. The mapping to 401/403 belongs to the API layer, so the same
checks hold in a worker, in the CLI and in a test.
"""

from __future__ import annotations

from dataclasses import dataclass

from mrip.auth.scope import Scope
from mrip.schemas import AuthSource, Role

__all__ = ["AccessDeniedError", "Principal"]


class AccessDeniedError(PermissionError):
    """The caller is authenticated but not permitted to do this."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling, what they may do, and which entities they may see."""

    user_id: str
    username: str
    role: Role
    scope: Scope
    auth_source: AuthSource = AuthSource.LOCAL
    #: True until a temporary password has been replaced. Every route except the
    #: password change refuses while this is set.
    must_change_password: bool = False
    #: The token id this request arrived with, recorded on audit rows so a trail
    #: names the session rather than only the account.
    token_id: str | None = None

    # --------------------------------------------------------------- authority

    def can(self, required: Role) -> bool:
        return self.role.can(required)

    def require(self, required: Role, *, action: str | None = None) -> None:
        """Raise unless this principal holds at least ``required``.

        The message names both roles and the action, because "403 Forbidden" with
        no detail generates a support ticket that costs more than the sentence.
        """
        if not self.can(required):
            what = action or "this action"
            raise AccessDeniedError(
                f"{what} requires the {required.value} role; "
                f"{self.username} is a {self.role.value}."
            )

    def require_entity(self, entity_id: str, *, action: str | None = None) -> None:
        """Raise unless this principal's scope covers ``entity_id``.

        Used on the *write* path. Reads are filtered by the scope clause instead —
        a read of something out of scope should return nothing, not an error,
        because a 403 on a specific id confirms that id exists.
        """
        if not self.scope.allows(entity_id):
            what = action or "this action"
            raise AccessDeniedError(
                f"{what} is outside {self.username}'s access scope "
                f"({entity_id} is not covered)."
            )

    # ------------------------------------------------------------- convenience

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    def describe(self) -> str:
        """One line for a log or an audit row."""
        return f"{self.username} ({self.role.value}, {self.scope})"

    @classmethod
    def service(cls, name: str, *, reason: str) -> Principal:
        """A non-human principal for internal work, with its reason recorded.

        Workers act with no user behind them. Rather than passing ``None`` around
        and letting each call site decide what that means, they carry an explicit
        service principal whose scope states why it is unrestricted — which is
        what appears in the audit log when a sweep changes a fact's status.
        """
        return cls(
            user_id=f"svc:{name}",
            username=f"service:{name}",
            role=Role.ADMIN,
            scope=Scope.unrestricted(reason),
            auth_source=AuthSource.LOCAL,
        )
