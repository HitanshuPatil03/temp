"""Access tokens.

Signed JWTs, HS256 by default, carrying **identity and nothing authoritative**.

That is the decision worth stating. A token here holds the user id, a token id, an
issue and expiry time, and the session epoch it was minted under. It does *not*
hold the role, and it does not hold the entity scope. Both are read from the
database on every single request, so:

- revoking an SECL officer's access to MCL takes effect on their next request
  rather than when their token happens to expire;
- demoting a reviewer to viewer is immediate;
- a token that outlives its account cannot be presented as proof of a role that
  no longer exists.

The cost is a lookup per request, which on a warehouse whose queries scan
evidence tables is not measurable. The benefit is that "revoke access" means what
an administrator thinks it means.

Stateless tokens cannot be individually revoked, so :data:`users.session_epoch`
is the revocation lever: changing a password or disabling an account bumps it, and
every token issued under the old epoch stops verifying. It is coarse — all of one
user's sessions, not one of them — and that is the right granularity for the
thing an operator actually asks for ("log them out").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from mrip.config import Settings, get_settings

__all__ = [
    "ISSUER",
    "TOKEN_TYPE",
    "ExpiredTokenError",
    "InvalidTokenError",
    "TokenClaims",
    "TokenError",
    "decode_access_token",
    "issue_access_token",
]

ISSUER = "mrip"
TOKEN_TYPE = "access"  # noqa: S105 — a claim value, not a credential

#: Clock skew tolerated between replicas. Small: these processes share a host or
#: a cluster, and a generous leeway is a generous window for a replayed token.
_LEEWAY_SECONDS = 10


class TokenError(Exception):
    """Base class, so a caller can catch every token problem at once."""


class InvalidTokenError(TokenError):
    """Malformed, wrongly signed, or missing a required claim."""


class ExpiredTokenError(TokenError):
    """Well-formed and correctly signed, but past its expiry.

    Distinct from :class:`InvalidTokenError` because the client's response
    differs: an expired token means "log in again", a bad one means "something is
    wrong with this request".
    """


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: str
    token_id: str
    session_epoch: int
    issued_at: datetime
    expires_at: datetime


def issue_access_token(
    user_id: str, *, session_epoch: int, settings: Settings | None = None
) -> tuple[str, datetime]:
    """Mint a token for a user. Returns ``(token, expiry)``.

    The expiry is returned rather than left for the caller to recompute, because
    the client shows "session expires at" and a second calculation is a second
    chance to disagree with the token.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.access_token_ttl_minutes)
    payload = {
        "iss": ISSUER,
        "sub": user_id,
        "typ": TOKEN_TYPE,
        # A token id, so an audit row can name the session that acted without
        # storing the token itself.
        "jti": uuid.uuid4().hex,
        "ep": session_epoch,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str, settings: Settings | None = None) -> TokenClaims:
    """Verify a token and return its claims.

    Every claim this system relies on is **required**, not defaulted: PyJWT will
    happily accept a token with no ``exp`` unless told otherwise, and a token
    that never expires is not a session.
    """
    settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            # Only the configured algorithm is accepted. Passing the full list
            # here is the classic JWT vulnerability: it lets a caller choose.
            algorithms=[settings.jwt_algorithm],
            issuer=ISSUER,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "iss", "jti"]},
        )
    except jwt.ExpiredSignatureError as expired:
        raise ExpiredTokenError("The session has expired. Sign in again.") from expired
    except jwt.InvalidTokenError as invalid:
        raise InvalidTokenError(f"Token rejected: {invalid}") from invalid

    if payload.get("typ") != TOKEN_TYPE:
        raise InvalidTokenError(
            f"Expected a {TOKEN_TYPE!r} token, got {payload.get('typ')!r}."
        )
    epoch = payload.get("ep")
    if not isinstance(epoch, int):
        raise InvalidTokenError("Token carries no session epoch.")

    return TokenClaims(
        user_id=str(payload["sub"]),
        token_id=str(payload["jti"]),
        session_epoch=epoch,
        issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )
