"""Authentication: turning a credential into a principal.

Kept out of the API layer so that the same rules hold for the CLI and for tests,
and out of the repository layer so that the repository stays a thin, auditable
mapping to SQL.

The shape worth noticing is that :func:`authenticate` returns an
:class:`AuthOutcome` rather than raising. A failed login is an ordinary,
*expected* result in a system whose users type passwords, and it has to be
recorded (a failure counter, an audit row) before a response is produced. Raising
would put that bookkeeping in an exception handler, where it gets skipped.

**Every failure returns the same message to the caller.** "No such user" and
"wrong password" are distinguished internally, for the audit trail, and
deliberately not distinguished on the wire: an API that says which is which hands
over a list of valid usernames.

The identity *providers* are a seam, not a branch: :class:`AuthSource` names
``local``, ``ldap`` and ``oidc``, and only ``local`` is implemented here. An
account whose source is LDAP has no password hash (a check constraint says so),
so it cannot be authenticated by this module at all — it fails closed rather than
falling back to a local credential that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from mrip.auth import passwords
from mrip.auth.principal import Principal
from mrip.auth.tokens import issue_access_token
from mrip.config import Settings, get_settings
from mrip.db.repositories.users import LOCKOUT_MINUTES, UserRecord
from mrip.db.store import Store
from mrip.schemas import AuthSource

__all__ = [
    "GENERIC_FAILURE",
    "AuthFailure",
    "AuthOutcome",
    "authenticate",
    "change_password",
    "principal_for",
]

#: What every failed login says, whatever actually happened.
GENERIC_FAILURE = "Incorrect username or password."


class AuthFailure:
    """Why a login failed, for the audit trail. Never sent to the client."""

    NO_SUCH_USER = "no_such_user"
    WRONG_PASSWORD = "wrong_password"  # noqa: S105 — a reason code, not a secret
    INACTIVE = "inactive"
    LOCKED = "locked"
    NO_LOCAL_CREDENTIAL = "no_local_credential"


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """The result of a login attempt."""

    ok: bool
    #: Present on success.
    principal: Principal | None = None
    token: str | None = None
    expires_at: datetime | None = None
    #: Present on failure: the internal reason code, and the message to return.
    reason: str | None = None
    message: str = GENERIC_FAILURE
    #: Set when the account is locked, so the client can say how long for.
    locked_until: datetime | None = None

    @property
    def is_locked(self) -> bool:
        return self.reason == AuthFailure.LOCKED


def principal_for(
    store: Store, user: UserRecord, *, token_id: str | None = None
) -> Principal:
    """Build a principal from a user row, reading scope from the grants table.

    Called on **every** authenticated request, not only at login. That is the
    cost of keeping role and scope out of the token: one small query, in exchange
    for revocation that takes effect immediately.
    """
    return Principal(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        scope=store.users.scope_for(user),
        auth_source=user.auth_source,
        must_change_password=user.must_change_password,
        token_id=token_id,
    )


def authenticate(
    store: Store,
    username: str,
    password: str,
    *,
    settings: Settings | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> AuthOutcome:
    """Verify a credential, record the attempt, and mint a token on success.

    Writes in every branch — a failure counter or a login timestamp, plus an
    audit row — so the caller must run this inside a transaction it intends to
    commit even when the result is a refusal. A rolled-back failed login is an
    unbounded number of guesses.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    user = store.users.by_username(username)

    def refuse(
        reason: str,
        *,
        locked_until: datetime | None = None,
        message: str = GENERIC_FAILURE,
    ) -> AuthOutcome:
        store.audit.record(
            "auth.login_failed",
            actor_user_id=user.user_id if user else None,
            actor_username=user.username if user else username[:64],
            subject_type="user",
            subject_id=user.user_id if user else None,
            detail={"reason": reason},
            request_id=request_id,
            source_ip=source_ip,
        )
        return AuthOutcome(
            ok=False, reason=reason, message=message, locked_until=locked_until
        )

    if user is None:
        # Still hash, so that a non-existent username is not detectably faster
        # than a wrong password. Username enumeration on a ministry deployment's
        # user list is a finding in its own right.
        passwords.verify_password(None, password)
        return refuse(AuthFailure.NO_SUCH_USER)

    if not user.is_active:
        passwords.verify_password(None, password)
        return refuse(AuthFailure.INACTIVE)

    if user.is_locked(now):
        # Checked before the password so a locked account cannot be used as an
        # oracle: the answer is the same whether the guess was right or wrong.
        return refuse(
            AuthFailure.LOCKED,
            locked_until=user.locked_until,
            message=(
                f"Too many failed attempts. This account is locked for "
                f"{LOCKOUT_MINUTES} minutes. An administrator can unlock it sooner."
            ),
        )

    if user.auth_source is not AuthSource.LOCAL:
        passwords.verify_password(None, password)
        return refuse(AuthFailure.NO_LOCAL_CREDENTIAL)

    if not passwords.verify_password(user.password_hash, password):
        locked_until = store.users.record_login_failure(user.user_id)
        if locked_until is not None and locked_until > now:
            return refuse(
                AuthFailure.LOCKED,
                locked_until=locked_until,
                message=(
                    f"Too many failed attempts. This account is locked for "
                    f"{LOCKOUT_MINUTES} minutes."
                ),
            )
        return refuse(AuthFailure.WRONG_PASSWORD)

    # Correct. Bring the stored hash up to current cost if policy has moved on;
    # this does not bump the session epoch, because the credential is unchanged.
    if user.password_hash and passwords.needs_rehash(user.password_hash):
        store.users.refresh_password_hash(user.user_id, passwords.hash_password(password))

    store.users.record_login_success(user.user_id)
    token, expires_at = issue_access_token(
        user.user_id, session_epoch=user.session_epoch, settings=settings
    )
    principal = principal_for(store, user)

    store.audit.record(
        "auth.login",
        actor_user_id=user.user_id,
        actor_username=user.username,
        subject_type="user",
        subject_id=user.user_id,
        entity_scope=str(principal.scope),
        detail={"role": user.role.value, "auth_source": user.auth_source.value},
        request_id=request_id,
        source_ip=source_ip,
    )

    return AuthOutcome(ok=True, principal=principal, token=token, expires_at=expires_at)


def change_password(
    store: Store,
    user: UserRecord,
    current_password: str,
    new_password: str,
    *,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> None:
    """Change a password, verifying the current one first.

    Raises :class:`~mrip.auth.passwords.WeakPasswordError` if the replacement
    fails policy, or ``PermissionError`` if the current password is wrong —
    which is checked even for a forced change, because "must change password"
    means the holder proved the temporary credential, not that anyone holding
    the token may set a new one.
    """
    if not passwords.verify_password(user.password_hash, current_password):
        store.audit.record(
            "auth.password_change_refused",
            actor_user_id=user.user_id,
            actor_username=user.username,
            subject_type="user",
            subject_id=user.user_id,
            detail={"reason": AuthFailure.WRONG_PASSWORD},
            request_id=request_id,
            source_ip=source_ip,
        )
        raise PermissionError("The current password is incorrect.")

    passwords.check_password_policy(new_password, username=user.username)
    if passwords.verify_password(user.password_hash, new_password):
        raise passwords.WeakPasswordError(
            ["The new password must differ from the current one."]
        )

    store.users.set_password(user.user_id, passwords.hash_password(new_password))
    store.audit.record(
        "auth.password_changed",
        actor_user_id=user.user_id,
        actor_username=user.username,
        subject_type="user",
        subject_id=user.user_id,
        # Recorded because the consequence is user-visible: every other session
        # this account had is now signed out.
        detail={"sessions_invalidated": True},
        request_id=request_id,
        source_ip=source_ip,
    )
