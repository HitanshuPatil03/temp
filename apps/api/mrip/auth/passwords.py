"""Local password credentials.

Argon2id, at the OWASP-recommended parameters, through ``argon2-cffi`` — which
also settles the questions that get answered wrongly by hand: the salt is
generated per hash, the parameters are encoded *in* the hash string so they can
be raised later without invalidating anyone, and verification is constant-time.

Two decisions are about the failure path rather than the happy one.

**A login against a username that does not exist still does the work.**
:func:`verify_password` is called against :data:`DUMMY_HASH`, so the response
time does not disclose which usernames are real. An enumeration oracle on a
government deployment's user list is a finding on its own.

**Parameters are re-applied on successful login.** When the cost is raised, the
next successful login re-hashes at the new cost (:func:`needs_rehash`), so the
corpus of stored hashes converges without a password reset for everybody.

Local accounts are the fallback, not the target: CIL runs Active Directory, and
``AuthSource.LDAP``/``OIDC`` principals keep their credentials there and have no
row in this table's ``password_hash`` column (enforced by a check constraint).
"""

from __future__ import annotations

import contextlib
import functools

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "WeakPasswordError",
    "check_password_policy",
    "hash_password",
    "needs_rehash",
    "verify_password",
]

#: OWASP's second recommended Argon2id profile: 19 MiB, 2 iterations, 1 lane.
#: Memory-bound rather than time-bound, which is what makes GPU cracking
#: expensive. Sized to stay comfortable on the modest on-prem hardware a CMPDI
#: deployment is likely to get.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

MIN_PASSWORD_LENGTH = 12

#: Rejected outright. Short, because a denylist is a courtesy, not a control —
#: the length floor and the lockout are the controls. These are the ones a
#: deployment of *this* system will actually be handed.
_OBVIOUS = frozenset(
    {
        "password",
        "password123",
        "passw0rd",
        "administrator",
        "coalindia",
        "coalindia123",
        "mriplogin",
        "mripadmin",
        "letmein",
        "welcome1",
        "qwerty123456",
        "changeme",
        "changeme123",
    }
)


class WeakPasswordError(ValueError):
    """The password does not meet policy. Carries every reason, not the first."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(" ".join(reasons))


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash of a value nobody has, for timing-equalised failures.

    Computed once and cached: doing the work per request is the same cost as a
    genuine verification, which is the point, but computing it at import time
    would slow every CLI invocation for no reason.
    """
    return _HASHER.hash("argon2id timing equaliser, not a credential")


def hash_password(password: str) -> str:
    """Hash a password for storage. Never logs or returns the plaintext."""
    return _HASHER.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Check a password against a stored hash.

    ``None`` — an account with no local credential, i.e. an LDAP or OIDC
    principal — still performs a verification against the dummy hash before
    returning ``False``, so "this account exists but not here" is not
    distinguishable by timing from "this password is wrong".
    """
    if not stored_hash:
        _safe_dummy_verify(password)
        return False
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _safe_dummy_verify(password: str) -> None:
    """Burn the same CPU a real verification would, and swallow the result."""
    with contextlib.suppress(VerifyMismatchError, VerificationError, InvalidHashError):
        _HASHER.verify(_dummy_hash(), password)


def needs_rehash(stored_hash: str) -> bool:
    """Whether this hash was made with weaker parameters than current policy."""
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        # Unparseable: treat as needing replacement rather than crashing a login.
        return True


def check_password_policy(password: str, *, username: str | None = None) -> None:
    """Raise :class:`WeakPasswordError` listing everything wrong with a password.

    Length first, and length is the only hard floor: composition rules push
    people towards ``Coalindia@2025``, which satisfies four character classes and
    is the first thing anybody would try. The rest of the rules exist to catch
    the specific passwords this deployment will otherwise be given.
    """
    reasons: list[str] = []
    stripped = password.strip()

    if len(password) < MIN_PASSWORD_LENGTH:
        reasons.append(
            f"Must be at least {MIN_PASSWORD_LENGTH} characters (got {len(password)})."
        )
    if not stripped:
        reasons.append("Must not be blank.")
    if password != stripped:
        reasons.append("Must not start or end with whitespace.")

    lowered = password.lower()
    if lowered in _OBVIOUS:
        reasons.append("This password is on the refused list.")
    if username and len(username) > 2 and username.lower() in lowered:
        reasons.append("Must not contain the username.")
    if len(set(password)) < 5:
        reasons.append("Must use at least five distinct characters.")

    if reasons:
        raise WeakPasswordError(reasons)
