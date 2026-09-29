"""User, scope-grant and audit repositories.

These three sit together because they are the same concern seen three ways: who
exists, what they may see, and what they did.

**Login is a write.** :meth:`UserRepository.record_login_failure` increments a
counter in the database and locks the account after
:data:`MAX_FAILED_LOGINS`. Counting in process memory would give every API
replica its own fresh allowance of guesses, and the whole point of a lockout is
that the total is bounded.

**Scope grants are rows, not a column.** An officer may cover two subsidiaries;
HQ covers all of them via the :data:`~mrip.auth.scope.SCOPE_ALL` sentinel stored
as an ordinary row, so revoking HQ-wide access is the same operation as revoking
any single grant.

**The audit log is append-only in the database**, not by convention — a trigger
rejects ``UPDATE``, ``DELETE`` and ``TRUNCATE``, and it binds the table owner
too. So :meth:`AuditRepository.record` is the only write method here, and there
is deliberately no way to correct a row: a mistaken entry is followed by a
corrective entry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db.tables import audit_log, user_scopes, users
from mrip.schemas import AuthSource, Role

__all__ = [
    "LOCKOUT_MINUTES",
    "MAX_FAILED_LOGINS",
    "AuditEntry",
    "AuditRepository",
    "ScopeGrant",
    "UserRecord",
    "UserRepository",
    "normalize_username",
]

#: Consecutive failures before an account is locked. Five is the usual
#: compromise: high enough that a fat-fingered password does not lock a reviewer
#: out mid-shift, low enough that online guessing is hopeless.
MAX_FAILED_LOGINS = 5

#: How long a lockout lasts. A timed lockout rather than a permanent one on
#: purpose: a permanent lock turns a guessing attempt against a known username
#: into a denial of service against that person, and an on-prem deployment's
#: administrator is not on call at 2am.
LOCKOUT_MINUTES = 15


def normalize_username(username: str) -> str:
    """Usernames are case-insensitive and stored lowercased.

    ``S.Kumar`` and ``s.kumar`` must not be two accounts with two different
    scopes, and Active Directory treats them as one principal.
    """
    return username.strip().lower()


@dataclass(frozen=True, slots=True)
class UserRecord:
    """A user row, including the password hash.

    Carries the hash because the login path needs it. It is a deliberately
    internal type: the API returns :class:`~mrip.auth.principal.Principal` or a
    response model, never this.
    """

    user_id: str
    username: str
    role: Role
    auth_source: AuthSource
    is_active: bool
    must_change_password: bool
    session_epoch: int
    failed_login_count: int
    locked_until: datetime | None
    created_at: datetime
    last_login_at: datetime | None
    email: str | None = None
    display_name: str | None = None
    password_hash: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> UserRecord:
        return cls(
            user_id=row["user_id"],
            username=row["username"],
            role=Role(row["role"]),
            auth_source=AuthSource(row["auth_source"]),
            is_active=row["is_active"],
            must_change_password=row["must_change_password"],
            session_epoch=row["session_epoch"],
            failed_login_count=row["failed_login_count"],
            locked_until=row["locked_until"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
            email=row["email"],
            display_name=row["display_name"],
            password_hash=row["password_hash"],
        )

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now


@dataclass(frozen=True, slots=True)
class ScopeGrant:
    entity_id: str
    granted_at: datetime
    granted_by: str | None


class UserRepository:
    """Accounts and their entity grants.

    Every method here is either a write or a lookup of *one* account by its own
    id or username. None of them take a :class:`~mrip.auth.scope.Scope`, and the
    reason is structural rather than an oversight: this is the table that
    *defines* scope, so scoping its reads would be circular. Access to it is
    gated by role at the route instead — only an admin may list or modify
    accounts — and that registration is asserted in ``tests/test_scope.py``.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # -------------------------------------------------------------- lookups

    def get(self, user_id: str) -> UserRecord | None:
        row = (
            self._conn.execute(sa.select(users).where(users.c.user_id == user_id))
            .mappings()
            .first()
        )
        return UserRecord.from_row(dict(row)) if row else None

    def by_username(self, username: str) -> UserRecord | None:
        row = (
            self._conn.execute(
                sa.select(users).where(users.c.username == normalize_username(username))
            )
            .mappings()
            .first()
        )
        return UserRecord.from_row(dict(row)) if row else None

    # Named `list_accounts` rather than `list`: inside a class body, a method
    # called `list` shadows the builtin for every *annotation* in the same
    # scope, so `-> list[ScopeGrant]` a few methods later silently means
    # "returns this method". mypy caught it here; the name change is the fix.
    def list_accounts(
        self, *, limit: int = 200, include_inactive: bool = True
    ) -> list[UserRecord]:
        query = sa.select(users).order_by(users.c.username).limit(limit)
        if not include_inactive:
            query = query.where(users.c.is_active)
        rows = self._conn.execute(query).mappings().all()
        return [UserRecord.from_row(dict(row)) for row in rows]

    def count_active_admins(self) -> int:
        """Guards the "you cannot lock yourself out" rules.

        An on-prem deployment with no reachable admin account needs a database
        console to recover, which for a ministry deployment means a support
        ticket and a week.
        """
        return (
            self._conn.execute(
                sa.select(sa.func.count())
                .select_from(users)
                .where(users.c.role == Role.ADMIN.value, users.c.is_active)
            ).scalar()
            or 0
        )

    # --------------------------------------------------------------- writes

    def create(
        self,
        username: str,
        *,
        role: Role = Role.VIEWER,
        password_hash: str | None = None,
        auth_source: AuthSource = AuthSource.LOCAL,
        email: str | None = None,
        display_name: str | None = None,
        must_change_password: bool = False,
    ) -> UserRecord:
        """Create an account. Raises on a duplicate username (unique constraint).

        A local account with no hash is refused by a check constraint rather than
        by this method: the same rule has to hold for a row inserted by the CLI
        or by a migration.
        """
        from mrip.db.repositories.documents import new_id

        row = (
            self._conn.execute(
                sa.insert(users)
                .values(
                    user_id=new_id("usr"),
                    username=normalize_username(username),
                    role=role.value,
                    password_hash=password_hash,
                    auth_source=auth_source.value,
                    email=email,
                    display_name=display_name,
                    must_change_password=must_change_password,
                )
                .returning(users)
            )
            .mappings()
            .one()
        )
        return UserRecord.from_row(dict(row))

    def set_password(
        self, user_id: str, password_hash: str, *, must_change: bool = False
    ) -> None:
        """Replace the stored hash and **invalidate existing sessions**.

        The epoch bump is the security-relevant half: without it, a password
        changed because it leaked would leave every token minted under the old
        one valid until it expired.
        """
        self._conn.execute(
            sa.update(users)
            .where(users.c.user_id == user_id)
            .values(
                password_hash=password_hash,
                must_change_password=must_change,
                session_epoch=users.c.session_epoch + 1,
                failed_login_count=0,
                locked_until=None,
            )
        )

    def refresh_password_hash(self, user_id: str, password_hash: str) -> None:
        """Re-store the *same* password at current Argon2 parameters.

        Distinct from :meth:`set_password` in one respect that matters: the
        session epoch is **not** bumped. The credential has not changed, so
        there is nothing to revoke, and bumping it would log the user out during
        the very request in which they successfully signed in.
        """
        self._conn.execute(
            sa.update(users)
            .where(users.c.user_id == user_id)
            .values(password_hash=password_hash)
        )

    def set_role(self, user_id: str, role: Role) -> None:
        self._conn.execute(
            sa.update(users).where(users.c.user_id == user_id).values(role=role.value)
        )

    def set_display_name(self, user_id: str, display_name: str | None) -> None:
        self._conn.execute(
            sa.update(users)
            .where(users.c.user_id == user_id)
            .values(display_name=display_name)
        )

    def set_active(self, user_id: str, *, active: bool) -> None:
        """Enable or disable an account.

        Disabling bumps the epoch, so a suspended account's open sessions stop
        working immediately rather than at token expiry. Re-enabling does not,
        because there is nothing outstanding to invalidate.
        """
        values: dict[str, Any] = {"is_active": active}
        if not active:
            values["session_epoch"] = users.c.session_epoch + 1
        self._conn.execute(
            sa.update(users).where(users.c.user_id == user_id).values(**values)
        )

    def record_login_success(self, user_id: str) -> None:
        self._conn.execute(
            sa.update(users)
            .where(users.c.user_id == user_id)
            .values(
                last_login_at=sa.func.now(),
                failed_login_count=0,
                locked_until=None,
            )
        )

    def record_login_failure(self, user_id: str) -> datetime | None:
        """Count a failure, locking the account at the threshold.

        Returns the lockout expiry if this failure caused (or extended) one. The
        counter is reset by a successful login, so the threshold means
        *consecutive* failures.
        """
        row = (
            self._conn.execute(
                sa.update(users)
                .where(users.c.user_id == user_id)
                .values(
                    failed_login_count=users.c.failed_login_count + 1,
                    locked_until=sa.case(
                        (
                            users.c.failed_login_count + 1 >= MAX_FAILED_LOGINS,
                            sa.func.now()
                            + sa.text(f"interval '{LOCKOUT_MINUTES} minutes'"),
                        ),
                        else_=users.c.locked_until,
                    ),
                )
                .returning(users.c.locked_until, users.c.failed_login_count)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        locked: datetime | None = row["locked_until"]
        return locked

    def unlock(self, user_id: str) -> None:
        """Clear a lockout. An administrator's answer to "I am locked out"."""
        self._conn.execute(
            sa.update(users)
            .where(users.c.user_id == user_id)
            .values(failed_login_count=0, locked_until=None)
        )

    # ---------------------------------------------------------------- scope

    def grants_for(self, user_id: str) -> list[ScopeGrant]:
        rows = (
            self._conn.execute(
                sa.select(user_scopes)
                .where(user_scopes.c.user_id == user_id)
                .order_by(user_scopes.c.entity_id)
            )
            .mappings()
            .all()
        )
        return [
            ScopeGrant(
                entity_id=row["entity_id"],
                granted_at=row["granted_at"],
                granted_by=row["granted_by"],
            )
            for row in rows
        ]

    def scope_for(self, user: UserRecord) -> Scope:
        """The row-level scope this account reads under.

        An inactive account gets :meth:`~mrip.auth.scope.Scope.nothing`, not its
        grants. Belt and braces — authentication already refuses it — but the two
        checks are in different layers and this one is the one the queries see.
        """
        if not user.is_active:
            return Scope.nothing()
        return Scope.from_grants(
            (grant.entity_id for grant in self.grants_for(user.user_id)),
            granted_to=user.username,
        )

    def grant_scope(
        self, user_id: str, entity_id: str, *, granted_by: str | None = None
    ) -> bool:
        """Grant access to one entity (or :data:`SCOPE_ALL`). Idempotent."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        result = self._conn.execute(
            pg_insert(user_scopes)
            .values(user_id=user_id, entity_id=entity_id, granted_by=granted_by)
            .on_conflict_do_nothing(index_elements=["user_id", "entity_id"])
        )
        return (result.rowcount or 0) == 1

    def revoke_scope(self, user_id: str, entity_id: str) -> bool:
        result = self._conn.execute(
            sa.delete(user_scopes).where(
                user_scopes.c.user_id == user_id,
                user_scopes.c.entity_id == entity_id,
            )
        )
        return (result.rowcount or 0) == 1

    def replace_scopes(
        self, user_id: str, entity_ids: Sequence[str], *, granted_by: str | None = None
    ) -> int:
        """Set a user's grants to exactly this set, in one transaction."""
        self._conn.execute(sa.delete(user_scopes).where(user_scopes.c.user_id == user_id))
        unique = sorted(set(entity_ids))
        if not unique:
            return 0
        self._conn.execute(
            sa.insert(user_scopes),
            [
                {"user_id": user_id, "entity_id": entity_id, "granted_by": granted_by}
                for entity_id in unique
            ],
        )
        return len(unique)

    def users_with_entity(self, entity_id: str) -> list[str]:
        """Usernames that can see an entity, HQ-wide grants included.

        Answers "who can see this document" — asked during an access review, and
        a question an access-control layer should be able to answer directly
        rather than by reasoning about the grant table.
        """
        rows = self._conn.execute(
            sa.select(users.c.username)
            .join(user_scopes, user_scopes.c.user_id == users.c.user_id)
            .where(
                users.c.is_active,
                user_scopes.c.entity_id.in_([entity_id, SCOPE_ALL]),
            )
            .order_by(users.c.username)
        ).all()
        return [username for (username,) in rows]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    audit_id: int
    occurred_at: datetime
    actor_user_id: str | None
    actor_username: str | None
    action: str
    subject_type: str | None
    subject_id: str | None
    entity_scope: str | None
    detail: dict[str, Any]
    request_id: str | None
    source_ip: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AuditEntry:
        return cls(
            audit_id=row["audit_id"],
            occurred_at=row["occurred_at"],
            actor_user_id=row["actor_user_id"],
            actor_username=row["actor_username"],
            action=row["action"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            entity_scope=row["entity_scope"],
            detail=row["detail"] or {},
            request_id=row["request_id"],
            source_ip=row["source_ip"],
        )


class AuditRepository:
    """Append-only trail. One write method, no update, no delete."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def record(
        self,
        action: str,
        *,
        actor_user_id: str | None = None,
        actor_username: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        entity_scope: str | None = None,
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
    ) -> int:
        """Append an entry and return its id.

        ``actor_username`` is stored alongside the id on purpose: the foreign key
        is ``ON DELETE SET NULL``, so without the denormalized copy a trail would
        become anonymous the moment an account was removed — which is precisely
        when it matters.
        """
        audit_id: int = self._conn.execute(
            sa.insert(audit_log)
            .values(
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                entity_scope=entity_scope,
                detail=detail or {},
                request_id=request_id,
                source_ip=source_ip,
            )
            .returning(audit_log.c.audit_id)
        ).scalar_one()
        return int(audit_id)

    def recent(
        self,
        *,
        limit: int = 100,
        action: str | None = None,
        actor_user_id: str | None = None,
        subject_id: str | None = None,
    ) -> list[AuditEntry]:
        """Read the trail, newest first.

        Unscoped, and it has to be: an audit trail filtered by the reader's own
        entity scope is not an audit trail. Reading it requires the admin role,
        enforced at the route.
        """
        query = sa.select(audit_log).order_by(audit_log.c.audit_id.desc()).limit(limit)
        if action is not None:
            query = query.where(audit_log.c.action == action)
        if actor_user_id is not None:
            query = query.where(audit_log.c.actor_user_id == actor_user_id)
        if subject_id is not None:
            query = query.where(audit_log.c.subject_id == subject_id)
        rows = self._conn.execute(query).mappings().all()
        return [AuditEntry.from_row(dict(row)) for row in rows]

    def count(self) -> int:
        return (
            self._conn.execute(sa.select(sa.func.count()).select_from(audit_log)).scalar()
            or 0
        )
