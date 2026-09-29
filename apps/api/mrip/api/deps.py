"""Shared FastAPI dependencies.

Three things are resolved here, in this order, and the order is the design:

**A store is per-request, not per-process.** ``provide_store`` is a generator
dependency: it opens a transaction, hands the request a :class:`~mrip.db.Store`
over it, and commits when the handler returns — or rolls back if it raises. The
DuckDB implementation shared one long-lived connection across every caller
because it had to; that is what made concurrent reviewers unsafe.

**A principal is resolved from the token and the database, every request.** The
token proves *which account*; the role and the entity scope are read fresh from
``users`` and ``user_scopes``. So revoking a grant or demoting a reviewer takes
effect on their next request rather than whenever their token happens to expire
(see :mod:`mrip.auth.tokens`).

**Scope comes from the principal.** Every repository read requires a
:class:`~mrip.auth.scope.Scope`, and this module is the only place a route can
obtain one — which is what makes "did we scope this endpoint?" a question with a
mechanical answer rather than a review opinion.

Two FastAPI details are load-bearing. The providers stay zero-argument wrappers
where they can, because FastAPI treats model-typed parameters as request-body
fields, and a provider taking a ``BaseSettings`` would silently turn every POST
body into an embedded object — a bug this project has already paid for once. And
authentication is a *dependency*, not middleware, so that the OpenAPI schema
records which routes require it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mrip.auth.principal import AccessDeniedError, Principal
from mrip.auth.scope import Scope
from mrip.auth.service import principal_for
from mrip.auth.tokens import (
    ExpiredTokenError,
    InvalidTokenError,
    decode_access_token,
)
from mrip.config import Settings, get_settings
from mrip.db import Store, transaction
from mrip.schemas import Role

__all__ = [
    "PrincipalDep",
    "ScopeDep",
    "SettingsDep",
    "StoreDep",
    "current_principal",
    "provide_scope",
    "provide_settings",
    "provide_store",
    "require_role",
    "source_ip",
]

#: ``auto_error=False`` so a missing header reaches our own handler and produces
#: the message below rather than FastAPI's bare "Not authenticated".
_bearer = HTTPBearer(auto_error=False, description="Access token from /api/auth/login")

#: Paths a principal who must change their password may still reach. Anything
#: else is refused: a temporary password an administrator chose is not a
#: credential the account holder owns yet, so it buys exactly the right to
#: replace itself.
_PASSWORD_CHANGE_EXEMPT = frozenset(
    {"/api/auth/me", "/api/auth/password", "/api/auth/logout"}
)


def provide_settings() -> Settings:
    """The process-wide settings singleton."""
    return get_settings()


def provide_store() -> Iterator[Store]:
    """A store over one request-scoped transaction.

    Commits on a clean return, rolls back on an exception. A handler that raises
    after a partial write leaves nothing behind.
    """
    with transaction() as conn:
        yield Store(conn)


def source_ip(request: Request) -> str | None:
    """The caller's address for the audit trail.

    ``X-Forwarded-For`` is honoured because the only deployed path to this API is
    through the Next.js proxy, which adds it. Its *first* entry is taken and the
    rest ignored; a client can forge the whole header, so this is recorded as
    evidence of what the request claimed, not as an authenticated fact.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def current_principal(
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    """The authenticated caller, or 401.

    Every refusal here is a 401 with a ``WWW-Authenticate`` header and a reason
    the client can act on — "expired" means sign in again, "revoked" means the
    session was invalidated deliberately. A 403 would be wrong: the caller is not
    forbidden from the resource, they are not authenticated for it.
    """

    def unauthenticated(detail: str, code: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": code, "message": detail},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials is None or not credentials.credentials:
        raise unauthenticated(
            "This endpoint requires an access token. Sign in at /api/auth/login.",
            "not_authenticated",
        )

    try:
        claims = decode_access_token(credentials.credentials, settings)
    except ExpiredTokenError as expired:
        raise unauthenticated(str(expired), "token_expired") from expired
    except InvalidTokenError as invalid:
        raise unauthenticated(str(invalid), "token_invalid") from invalid

    user = store.users.get(claims.user_id)
    if user is None:
        # The account was deleted while a token was still live.
        raise unauthenticated("This account no longer exists.", "account_missing")
    if not user.is_active:
        raise unauthenticated("This account is disabled.", "account_disabled")
    if claims.session_epoch != user.session_epoch:
        raise unauthenticated(
            "This session was ended by a password change or an administrator. "
            "Sign in again.",
            "session_revoked",
        )

    principal = principal_for(store, user, token_id=claims.token_id)

    if principal.must_change_password and request.url.path not in _PASSWORD_CHANGE_EXEMPT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "password_change_required",
                "message": (
                    "This account is using a temporary password. Set a new one at "
                    "/api/auth/password before using the rest of the API."
                ),
            },
        )

    return principal


def provide_scope(principal: PrincipalDep) -> Scope:
    """The caller's row-level access scope, from their grants.

    An authenticated account with no grants yields
    :meth:`~mrip.auth.scope.Scope.nothing` — an empty corpus, not an unfiltered
    one. That is the whole reason :class:`Scope` has an explicit empty case.
    """
    return principal.scope


def require_role(minimum: Role) -> Callable[[Principal], Principal]:
    """Build a dependency that refuses callers below ``minimum``.

    Used as ``Depends(require_role(Role.REVIEWER))`` on the routes that change
    adjudicated state. Returns the principal, so a route that needs both the
    check and the identity declares one dependency rather than two.
    """

    def guard(principal: PrincipalDep) -> Principal:
        try:
            principal.require(minimum)
        except AccessDeniedError as denied:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "insufficient_role", "message": str(denied)},
            ) from denied
        return principal

    return guard


StoreDep = Annotated[Store, Depends(provide_store)]
SettingsDep = Annotated[Settings, Depends(provide_settings)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]
ScopeDep = Annotated[Scope, Depends(provide_scope)]
SourceIpDep = Annotated[str | None, Depends(source_ip)]
