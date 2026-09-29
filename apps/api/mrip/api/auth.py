"""Authentication and account administration endpoints.

``POST /api/auth/login`` takes JSON rather than OAuth2 form encoding. The only
client is this project's own Next.js frontend, and a JSON body keeps the error
model identical to every other endpoint here — the ``password_grant`` form
convention buys interoperability with third-party OAuth clients that a
network-isolated on-prem deployment will never have.

There is no refresh token. Sessions are short (an hour by default) and a
reviewer's working session is a browser tab; a refresh token would add a second
credential to store, leak and revoke in exchange for not re-typing a password
once a shift. When CIL's SSO lands, the IdP owns session lifetime anyway.

``POST /api/auth/logout`` exists and is honest about what it does: a stateless
token cannot be individually revoked, so logout tells the client to discard it
and writes the audit row. "Sign out everywhere" is a password change, which bumps
the session epoch.

Account administration lives here too, gated on the admin role. It is the same
surface the ``mrip-admin`` CLI drives, so bootstrapping a deployment and running
it day to day do not diverge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from mrip.api.deps import (
    PrincipalDep,
    SourceIpDep,
    StoreDep,
    require_role,
)
from mrip.auth import passwords
from mrip.auth.principal import Principal
from mrip.auth.service import authenticate, change_password
from mrip.db.repositories.users import (
    LOCKOUT_MINUTES,
    MAX_FAILED_LOGINS,
    UserRecord,
)
from mrip.schemas import AuthSource, Role

router = APIRouter(prefix="/auth", tags=["auth"])

AdminDep = Annotated[Principal, Depends(require_role(Role.ADMIN))]


# ------------------------------------------------------------------- wire models


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class SessionResponse(BaseModel):
    """What a successful login returns.

    The token's expiry is returned explicitly so the client can show "session
    ends at 18:40" and schedule a re-authentication prompt, rather than
    discovering expiry as a failed request mid-edit.
    """

    access_token: str
    token_type: str = "bearer"  # noqa: S105 — a scheme name, not a credential
    expires_at: datetime
    user: PrincipalResponse


class PrincipalResponse(BaseModel):
    """The caller's own identity. Never another account's."""

    user_id: str
    username: str
    display_name: str | None
    role: Role
    auth_source: AuthSource
    #: ``["*"]`` for an HQ-wide grant. Returned so the UI can label the header
    #: "SECL, MCL" instead of leaving a reviewer guessing why a document is
    #: missing from a list.
    entities: list[str]
    must_change_password: bool

    @classmethod
    def build(
        cls, principal: Principal, *, display_name: str | None
    ) -> PrincipalResponse:
        return cls(
            user_id=principal.user_id,
            username=principal.username,
            display_name=display_name,
            role=principal.role,
            auth_source=principal.auth_source,
            entities=sorted(principal.scope.entity_ids),
            must_change_password=principal.must_change_password,
        )


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    """An account as an administrator sees it. No hash, ever."""

    user_id: str
    username: str
    display_name: str | None
    email: str | None
    role: Role
    auth_source: AuthSource
    is_active: bool
    must_change_password: bool
    entities: list[str]
    failed_login_count: int
    locked_until: datetime | None
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def build(cls, record: UserRecord, entities: list[str]) -> UserResponse:
        return cls(
            user_id=record.user_id,
            username=record.username,
            display_name=record.display_name,
            email=record.email,
            role=record.role,
            auth_source=record.auth_source,
            is_active=record.is_active,
            must_change_password=record.must_change_password,
            entities=entities,
            failed_login_count=record.failed_login_count,
            locked_until=record.locked_until,
            created_at=record.created_at,
            last_login_at=record.last_login_at,
        )


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    role: Role = Role.VIEWER
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    #: Entity ids this account may read; ``["*"]`` for HQ-wide.
    entities: list[str] = Field(default_factory=list, max_length=64)
    #: Default true: an administrator who chose the password knows it, so the
    #: holder must replace it before the account is usable for anything else.
    must_change_password: bool = True


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=200)
    entities: list[str] | None = Field(default=None, max_length=64)
    #: Setting this resets the password and forces a change at next login.
    reset_password: str | None = Field(default=None, max_length=256)
    unlock: bool = False


# -------------------------------------------------------------------- endpoints


@router.post("/login", response_model=SessionResponse)
def login(
    body: LoginRequest,
    request: Request,
    store: StoreDep,
    caller_ip: SourceIpDep,
) -> SessionResponse:
    """Exchange a username and password for an access token.

    Returns 401 with the same message for every kind of failure — unknown user,
    wrong password, disabled account — because a response that distinguishes them
    is a username oracle. The distinction is recorded in the audit log, where it
    belongs.

    Note that this endpoint **writes on failure**: the failed-attempt counter and
    the audit row are part of the same transaction, so a rejected login still
    counts against the lockout threshold.
    """
    outcome = authenticate(
        store,
        body.username,
        body.password,
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )

    if not outcome.ok or outcome.principal is None or outcome.token is None:
        detail: dict[str, Any] = {
            "error": "locked" if outcome.is_locked else "invalid_credentials",
            "message": outcome.message,
        }
        if outcome.locked_until is not None:
            detail["locked_until"] = outcome.locked_until.isoformat()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )

    record = store.users.get(outcome.principal.user_id)
    assert outcome.expires_at is not None  # set together with the token
    return SessionResponse(
        access_token=outcome.token,
        expires_at=outcome.expires_at,
        user=PrincipalResponse.build(
            outcome.principal,
            display_name=record.display_name if record else None,
        ),
    )


@router.get("/me", response_model=PrincipalResponse)
def whoami(principal: PrincipalDep, store: StoreDep) -> PrincipalResponse:
    """The caller's own identity, role and entity scope.

    The frontend calls this on load to decide which actions to render. It is not
    a security boundary — every route enforces its own role — but a UI that
    offers a reviewer a button that will 403 is a UI that trains people to
    distrust it.
    """
    record = store.users.get(principal.user_id)
    return PrincipalResponse.build(
        principal, display_name=record.display_name if record else None
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    principal: PrincipalDep,
    store: StoreDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> None:
    """Record a sign-out. The client discards the token.

    Deliberately not pretending to revoke: a stateless token stays valid until it
    expires, and an endpoint that implied otherwise would be a false assurance.
    To end every session now, change the password.
    """
    store.audit.record(
        "auth.logout",
        actor_user_id=principal.user_id,
        actor_username=principal.username,
        subject_type="user",
        subject_id=principal.user_id,
        detail={"token_id": principal.token_id},
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def set_own_password(
    body: PasswordChangeRequest,
    principal: PrincipalDep,
    store: StoreDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> None:
    """Change your own password. Ends every other session for this account.

    Reachable while ``must_change_password`` is set — it is the one thing such an
    account may do — and the current password is still required, because holding
    a token is not proof of holding the credential it was issued for.
    """
    record = store.users.get(principal.user_id)
    if record is None:  # pragma: no cover - the dependency already loaded it
        raise HTTPException(status_code=404, detail="Account not found.")

    try:
        change_password(
            store,
            record,
            body.current_password,
            body.new_password,
            request_id=getattr(request.state, "request_id", None),
            source_ip=caller_ip,
        )
    except PermissionError as wrong:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "wrong_password", "message": str(wrong)},
        ) from wrong
    except passwords.WeakPasswordError as weak:
        # 422, like every other refusal in this API: the request was understood
        # and declined on its contents.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "weak_password",
                "message": "The new password does not meet policy.",
                "reasons": weak.reasons,
                "policy": {
                    "min_length": passwords.MIN_PASSWORD_LENGTH,
                    "lockout_after_failures": MAX_FAILED_LOGINS,
                    "lockout_minutes": LOCKOUT_MINUTES,
                },
            },
        ) from weak


# --------------------------------------------------------------- administration


@router.get("/users", response_model=list[UserResponse])
def list_users(admin: AdminDep, store: StoreDep) -> list[UserResponse]:
    """Every account. Admin only."""
    return [
        UserResponse.build(
            record,
            [grant.entity_id for grant in store.users.grants_for(record.user_id)],
        )
        for record in store.users.list_accounts()
    ]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    body: CreateUserRequest,
    admin: AdminDep,
    store: StoreDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> UserResponse:
    """Create an account with an initial password and entity grants.

    The password is checked against policy here rather than trusted because an
    administrator typed it: "Welcome123" chosen once for forty reviewers is the
    normal way a deployment's accounts all end up sharing a credential.
    """
    try:
        passwords.check_password_policy(body.password, username=body.username)
    except passwords.WeakPasswordError as weak:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "weak_password",
                "message": "The initial password does not meet policy.",
                "reasons": weak.reasons,
            },
        ) from weak

    if store.users.by_username(body.username) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "username_taken",
                "message": f"An account named {body.username!r} already exists.",
            },
        )

    record = store.users.create(
        body.username,
        role=body.role,
        password_hash=passwords.hash_password(body.password),
        email=body.email,
        display_name=body.display_name,
        must_change_password=body.must_change_password,
    )
    granted = store.users.replace_scopes(
        record.user_id, body.entities, granted_by=admin.user_id
    )
    store.audit.record(
        "user.created",
        actor_user_id=admin.user_id,
        actor_username=admin.username,
        subject_type="user",
        subject_id=record.user_id,
        detail={
            "username": record.username,
            "role": record.role.value,
            "entities": sorted(set(body.entities)),
            "grants": granted,
        },
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )
    return UserResponse.build(record, sorted(set(body.entities)))


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    admin: AdminDep,
    store: StoreDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> UserResponse:
    """Change a role, scope, status or password. Admin only.

    Refuses to remove the last active administrator. An on-prem deployment with
    no reachable admin account is recovered with a database console, which for a
    ministry deployment means a support ticket and a week.
    """
    record = store.users.get(user_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Account not found.")

    losing_last_admin = (
        record.role is Role.ADMIN
        and record.is_active
        and store.users.count_active_admins() == 1
        and (
            (body.role is not None and body.role is not Role.ADMIN)
            or body.is_active is False
        )
    )
    if losing_last_admin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "last_admin",
                "message": (
                    f"{record.username} is the only active administrator. "
                    "Promote another account first."
                ),
            },
        )

    changed: dict[str, Any] = {}

    if body.role is not None and body.role is not record.role:
        store.users.set_role(user_id, body.role)
        changed["role"] = body.role.value
    if body.is_active is not None and body.is_active != record.is_active:
        store.users.set_active(user_id, active=body.is_active)
        changed["is_active"] = body.is_active
    if body.display_name is not None:
        store.users.set_display_name(user_id, body.display_name)
        changed["display_name"] = body.display_name
    if body.entities is not None:
        store.users.replace_scopes(user_id, body.entities, granted_by=admin.user_id)
        changed["entities"] = sorted(set(body.entities))
    if body.unlock:
        store.users.unlock(user_id)
        changed["unlocked"] = True
    if body.reset_password is not None:
        try:
            passwords.check_password_policy(body.reset_password, username=record.username)
        except passwords.WeakPasswordError as weak:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "weak_password",
                    "message": "The replacement password does not meet policy.",
                    "reasons": weak.reasons,
                },
            ) from weak
        store.users.set_password(
            user_id,
            passwords.hash_password(body.reset_password),
            # An administrator knows this one, so it buys exactly one login.
            must_change=True,
        )
        changed["password_reset"] = True

    if not changed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "no_change",
                "message": "The request would not change anything.",
            },
        )

    store.audit.record(
        "user.updated",
        actor_user_id=admin.user_id,
        actor_username=admin.username,
        subject_type="user",
        subject_id=user_id,
        detail={"username": record.username, "changed": changed},
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )

    updated = store.users.get(user_id)
    assert updated is not None
    return UserResponse.build(
        updated, [grant.entity_id for grant in store.users.grants_for(user_id)]
    )


@router.get("/audit")
def read_audit(
    admin: AdminDep,
    store: StoreDep,
    limit: int = 100,
    action: str | None = None,
    subject_id: str | None = None,
) -> list[dict[str, Any]]:
    """The audit trail, newest first. Admin only.

    Unscoped by necessity — a trail filtered by the reader's own entity scope is
    not a trail — which is why the role check is the whole access control here.
    """
    entries = store.audit.recent(
        limit=min(limit, 1000), action=action, subject_id=subject_id
    )
    return [
        {
            "audit_id": entry.audit_id,
            "occurred_at": entry.occurred_at.isoformat(),
            "actor": entry.actor_username,
            "action": entry.action,
            "subject_type": entry.subject_type,
            "subject_id": entry.subject_id,
            "entity_scope": entry.entity_scope,
            "detail": entry.detail,
            "request_id": entry.request_id,
            "source_ip": entry.source_ip,
        }
        for entry in entries
    ]


__all__ = ["router"]
