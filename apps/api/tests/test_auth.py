"""Tests for identity: passwords, tokens, login, roles and the audit trail.

The claims under test are the ones an access-control layer is worth having for:

1. a failed login is **indistinguishable** on the wire from an unknown username;
2. failures are **counted in the database** and lock the account;
3. role and scope are read from the database on **every request**, so revoking
   access takes effect immediately rather than at token expiry;
4. a password change **invalidates tokens already issued**;
5. an authenticated caller with no grants sees an **empty corpus**, not the
   whole one;
6. every one of those events lands in the append-only audit log.

Each is written so that the plausible shortcut fails it — a token carrying the
role, a lockout counter in process memory, a scope defaulting to "all" when no
grants exist.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_store
from mrip.auth import passwords
from mrip.auth.principal import AccessDeniedError, Principal
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.auth.service import AuthFailure, authenticate, change_password
from mrip.auth.tokens import (
    ExpiredTokenError,
    InvalidTokenError,
    decode_access_token,
    issue_access_token,
)
from mrip.config import get_settings
from mrip.db.repositories.users import MAX_FAILED_LOGINS, normalize_username
from mrip.main import create_app
from mrip.schemas import AuthSource, Role
from tests.conftest import TEST_PASSWORD

GOOD_PASSWORD = "sunflower-ledger-98-tonne"


@pytest.fixture
def app_client(store):
    """A client with no credentials attached, for the auth tests themselves."""
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------- passwords


def test_a_hash_verifies_and_a_wrong_password_does_not():
    stored = passwords.hash_password(GOOD_PASSWORD)

    assert passwords.verify_password(stored, GOOD_PASSWORD) is True
    assert passwords.verify_password(stored, GOOD_PASSWORD + "x") is False


def test_two_hashes_of_one_password_differ():
    """Per-hash salt. Identical hashes would let an attacker spot shared
    passwords across accounts from a database dump alone."""
    assert passwords.hash_password(GOOD_PASSWORD) != passwords.hash_password(
        GOOD_PASSWORD
    )


def test_an_account_with_no_local_credential_never_verifies():
    """LDAP and OIDC principals have no hash, and must fail closed rather than
    falling through to some default."""
    assert passwords.verify_password(None, GOOD_PASSWORD) is False
    assert passwords.verify_password("", GOOD_PASSWORD) is False


def test_a_corrupt_hash_is_a_failed_login_not_a_crash():
    assert passwords.verify_password("not-an-argon2-hash", GOOD_PASSWORD) is False


def test_policy_reports_every_reason_at_once():
    """One reason at a time turns a password change into a guessing game."""
    with pytest.raises(passwords.WeakPasswordError) as raised:
        passwords.check_password_policy("aaa", username="aaa")

    reasons = " ".join(raised.value.reasons)
    assert "at least 12" in reasons
    assert "distinct characters" in reasons


def test_policy_refuses_a_password_containing_the_username():
    with pytest.raises(passwords.WeakPasswordError, match="username"):
        passwords.check_password_policy("s.kumar-secl-2025", username="s.kumar")


def test_policy_refuses_the_obvious_ones():
    with pytest.raises(passwords.WeakPasswordError, match="refused list"):
        passwords.check_password_policy("CoalIndia123")


def test_a_long_distinct_password_passes():
    passwords.check_password_policy(GOOD_PASSWORD, username="r.reviewer")


# ---------------------------------------------------------------------- tokens


def test_a_token_round_trips_its_claims():
    token, expires_at = issue_access_token("usr_1", session_epoch=3)
    claims = decode_access_token(token)

    assert claims.user_id == "usr_1"
    assert claims.session_epoch == 3
    assert claims.expires_at == expires_at.replace(microsecond=0)
    assert claims.token_id


def test_a_token_carries_no_role_or_scope():
    """The decision behind reading them per request: a token that carried a role
    would keep asserting it after a demotion."""
    token, _ = issue_access_token("usr_1", session_epoch=1)
    payload = jwt.decode(token, options={"verify_signature": False})

    assert "role" not in payload
    assert "scope" not in payload
    assert "entities" not in payload


def test_a_token_signed_with_another_key_is_rejected():
    forged = jwt.encode(
        {
            "iss": "mrip",
            "sub": "usr_1",
            "typ": "access",
            "jti": "x",
            "ep": 1,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        # A full-length key: PyJWT warns about short HMAC keys, and the point of
        # this test is the *wrong* key, not a weak one.
        "attacker-key-that-is-long-enough-to-not-warn-0123456789",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(forged)


def test_an_unsigned_token_is_rejected():
    """The classic JWT hole: `alg: none`, accepted by a decoder that takes the
    algorithm from the token instead of from configuration.

    Assembled by hand rather than with ``jwt.encode``, because the library
    refuses to mint one — which is exactly the attacker's position: they craft
    the bytes themselves and hope the *verifier* is lenient.
    """

    def segment(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    unsigned = "{}.{}.".format(
        segment({"alg": "none", "typ": "JWT"}),
        segment(
            {
                "iss": "mrip",
                "sub": "usr_1",
                "typ": "access",
                "jti": "x",
                "ep": 1,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            }
        ),
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned)


def test_an_expired_token_is_distinguishable_from_an_invalid_one():
    """The client's response differs: one means "sign in again", the other means
    "something is wrong with this request"."""
    settings = get_settings()
    payload = {
        "iss": "mrip",
        "sub": "usr_1",
        "typ": "access",
        "jti": "x",
        "ep": 1,
        "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
        "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
    }
    stale = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    with pytest.raises(ExpiredTokenError):
        decode_access_token(stale)


def test_a_token_without_an_expiry_is_rejected():
    """A session that never ends is not a session."""
    settings = get_settings()
    forever = jwt.encode(
        {"iss": "mrip", "sub": "usr_1", "typ": "access", "jti": "x", "ep": 1, "iat": 0},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError, match="exp"):
        decode_access_token(forever)


# ------------------------------------------------------------------ principals


def test_role_rank_is_a_hierarchy():
    assert Role.ADMIN.can(Role.VIEWER) is True
    assert Role.VIEWER.can(Role.REVIEWER) is False
    assert Role.APPROVER.can(Role.REVIEWER) is True


def test_require_names_both_roles_in_its_refusal():
    principal = Principal(
        user_id="usr_1",
        username="v.viewer",
        role=Role.VIEWER,
        scope=Scope.of("secl"),
    )
    with pytest.raises(AccessDeniedError, match="requires the reviewer role"):
        principal.require(Role.REVIEWER, action="Resolving a conflict")


def test_require_entity_guards_the_write_path():
    principal = Principal(
        user_id="usr_1",
        username="o.officer",
        role=Role.OFFICER,
        scope=Scope.of("secl"),
    )
    principal.require_entity("secl")
    with pytest.raises(AccessDeniedError, match="outside"):
        principal.require_entity("mcl")


# ----------------------------------------------------------------------- login


def test_a_correct_password_yields_a_token_and_a_principal(store, make_user):
    user = make_user("s.kumar", role=Role.OFFICER, entities=("secl", "mcl"))

    outcome = authenticate(store, "s.kumar", TEST_PASSWORD)

    assert outcome.ok is True
    assert outcome.token
    assert outcome.principal is not None
    assert outcome.principal.role is Role.OFFICER
    assert outcome.principal.scope.entity_ids == {"secl", "mcl"}
    assert store.users.get(user.user_id).last_login_at is not None


def test_login_is_case_insensitive_on_the_username(store, make_user):
    make_user("s.kumar")
    assert authenticate(store, "S.Kumar", TEST_PASSWORD).ok is True
    assert normalize_username("  S.Kumar  ") == "s.kumar"


def test_every_failure_returns_the_same_message(store, make_user):
    """An API that says "no such user" hands over a list of valid usernames."""
    make_user("s.kumar")

    unknown = authenticate(store, "nobody.here", TEST_PASSWORD)
    wrong = authenticate(store, "s.kumar", "the-wrong-password")

    assert unknown.ok is wrong.ok is False
    assert unknown.message == wrong.message
    # Distinguished internally, for the trail — and only there.
    assert unknown.reason == AuthFailure.NO_SUCH_USER
    assert wrong.reason == AuthFailure.WRONG_PASSWORD


def test_a_disabled_account_cannot_sign_in(store, make_user):
    user = make_user("s.kumar")
    store.users.set_active(user.user_id, active=False)

    outcome = authenticate(store, "s.kumar", TEST_PASSWORD)
    assert outcome.ok is False
    assert outcome.reason == AuthFailure.INACTIVE


def test_an_ldap_account_is_not_authenticated_locally(store, make_user):
    """No password hash exists for it, so it must fail closed rather than
    falling back to a local credential."""
    make_user("ad.user", auth_source=AuthSource.LDAP, password_hash=None)

    outcome = authenticate(store, "ad.user", TEST_PASSWORD)
    assert outcome.ok is False
    assert outcome.reason == AuthFailure.NO_LOCAL_CREDENTIAL


def test_consecutive_failures_lock_the_account(store, make_user):
    """Counted in the database: two API replicas must not each grant a fresh
    allowance of guesses."""
    make_user("s.kumar")

    for _ in range(MAX_FAILED_LOGINS - 1):
        assert (
            authenticate(store, "s.kumar", "wrong").reason == AuthFailure.WRONG_PASSWORD
        )

    locking = authenticate(store, "s.kumar", "wrong")
    assert locking.reason == AuthFailure.LOCKED
    assert locking.locked_until is not None

    # And the correct password does not help while the lock holds — otherwise the
    # lockout would be an oracle telling an attacker when they guessed right.
    blocked = authenticate(store, "s.kumar", TEST_PASSWORD)
    assert blocked.ok is False
    assert blocked.reason == AuthFailure.LOCKED


def test_a_successful_login_resets_the_failure_count(store, make_user):
    user = make_user("s.kumar")
    authenticate(store, "s.kumar", "wrong")
    authenticate(store, "s.kumar", "wrong")
    assert store.users.get(user.user_id).failed_login_count == 2

    authenticate(store, "s.kumar", TEST_PASSWORD)
    assert store.users.get(user.user_id).failed_login_count == 0


def test_an_administrator_can_unlock(store, make_user):
    user = make_user("s.kumar")
    for _ in range(MAX_FAILED_LOGINS):
        authenticate(store, "s.kumar", "wrong")

    store.users.unlock(user.user_id)
    assert authenticate(store, "s.kumar", TEST_PASSWORD).ok is True


# ------------------------------------------------------------ password changes


def test_changing_a_password_invalidates_existing_tokens(store, make_user):
    """The revocation lever: a leaked password must not leave live sessions."""
    user = make_user("s.kumar")
    before = store.users.get(user.user_id).session_epoch

    change_password(store, store.users.get(user.user_id), TEST_PASSWORD, GOOD_PASSWORD)

    after = store.users.get(user.user_id).session_epoch
    assert after > before, "the session epoch must move, or old tokens stay valid"
    assert authenticate(store, "s.kumar", GOOD_PASSWORD).ok is True


def test_changing_a_password_requires_the_current_one(store, make_user):
    """Holding a token is not proof of holding the credential it was issued for."""
    user = make_user("s.kumar")

    with pytest.raises(PermissionError, match="current password"):
        change_password(store, user, "not-the-current-one", GOOD_PASSWORD)


def test_a_new_password_must_meet_policy_and_differ(store, make_user):
    user = make_user("s.kumar")

    with pytest.raises(passwords.WeakPasswordError):
        change_password(store, user, TEST_PASSWORD, "short")
    with pytest.raises(passwords.WeakPasswordError, match="differ"):
        change_password(store, user, TEST_PASSWORD, TEST_PASSWORD)


# -------------------------------------------------------------- scope from db


def test_an_account_with_no_grants_reads_an_empty_corpus(store, make_user, make_fact):
    """The failure this whole layer exists to prevent is the opposite default."""
    store.insert_facts([make_fact()])
    user = make_user("new.joiner", entities=())

    scope = store.users.scope_for(user)
    assert scope.is_empty
    assert store.query_facts(scope) == []


def test_a_grant_restricts_rather_than_filters_nothing(store, make_user, make_fact):
    store.insert_facts([make_fact(entity_id="secl"), make_fact(entity_id="mcl")])
    user = make_user("secl.officer", role=Role.OFFICER, entities=("secl",))

    facts = store.query_facts(store.users.scope_for(user))
    assert {fact.entity_id for fact in facts} == {"secl"}


def test_an_hq_grant_is_unrestricted_and_says_whose(store, make_user):
    user = make_user("hq.analyst", entities=(SCOPE_ALL,))

    scope = store.users.scope_for(user)
    assert scope.is_unrestricted
    assert "hq.analyst" in (scope.unrestricted_reason or "")


def test_a_disabled_account_has_no_scope_even_with_grants(store, make_user):
    user = make_user("s.kumar", entities=(SCOPE_ALL,))
    store.users.set_active(user.user_id, active=False)

    assert store.users.scope_for(store.users.get(user.user_id)).is_empty


def test_users_with_entity_includes_hq_grants(store, make_user):
    make_user("secl.officer", entities=("secl",))
    make_user("hq.analyst", entities=(SCOPE_ALL,))
    make_user("mcl.officer", entities=("mcl",))

    assert store.users.users_with_entity("secl") == ["hq.analyst", "secl.officer"]


# ----------------------------------------------------------------- over HTTP


def test_an_unauthenticated_request_is_401_with_a_reason(app_client):
    response = app_client.get("/api/facts")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["detail"]["error"] == "not_authenticated"


def test_the_liveness_probe_stays_open(app_client):
    """An orchestrator cannot hold a credential, and "is the process alive" is
    not a disclosure."""
    assert app_client.get("/api/health").status_code == 200


def test_pipeline_health_is_not_open(app_client):
    """Corpus size and queue depth are operational facts about unpublished data."""
    assert app_client.get("/api/health/pipeline").status_code == 401


def test_login_over_http_returns_a_usable_session(app_client, make_user):
    make_user("s.kumar", role=Role.OFFICER, entities=("secl",))

    response = app_client.post(
        "/api/auth/login", json={"username": "s.kumar", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["role"] == "officer"
    assert body["user"]["entities"] == ["secl"]

    me = app_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.json()["username"] == "s.kumar"


def test_a_bad_login_over_http_is_401_and_says_nothing_useful(app_client, make_user):
    make_user("s.kumar")

    response = app_client.post(
        "/api/auth/login", json={"username": "s.kumar", "password": "wrong"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["message"] == "Incorrect username or password."


def test_a_revoked_session_is_refused_with_a_specific_error(
    app_client, store, make_user, bearer
):
    """The epoch check: a password change signs other sessions out."""
    user = make_user("s.kumar")
    headers = bearer(user)
    assert app_client.get("/api/auth/me", headers=headers).status_code == 200

    change_password(store, store.users.get(user.user_id), TEST_PASSWORD, GOOD_PASSWORD)

    refused = app_client.get("/api/auth/me", headers=headers)
    assert refused.status_code == 401
    assert refused.json()["detail"]["error"] == "session_revoked"


def test_a_disabled_account_loses_its_live_session(app_client, store, make_user, bearer):
    """Suspension has to take effect now, not at token expiry."""
    user = make_user("s.kumar")
    headers = bearer(user)
    assert app_client.get("/api/auth/me", headers=headers).status_code == 200

    store.users.set_active(user.user_id, active=False)

    refused = app_client.get("/api/auth/me", headers=headers)
    assert refused.status_code == 401
    assert refused.json()["detail"]["error"] in {"account_disabled", "session_revoked"}


def test_a_demotion_takes_effect_on_the_next_request(
    app_client, store, make_user, bearer, make_fact
):
    """Role is read per request, which is the whole reason it is not in the token."""
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=191.0e6)])
    conflict = store.detect_conflicts(Scope.unrestricted("test"))[0]
    reviewer = make_user("r.reviewer", role=Role.REVIEWER, entities=(SCOPE_ALL,))
    headers = bearer(reviewer)

    store.users.set_role(reviewer.user_id, Role.VIEWER)

    response = app_client.post(
        f"/api/conflicts/{conflict.conflict_id}/resolve",
        json={"winning_fact_id": conflict.facts[0].fact_id},
        headers=headers,
    )
    assert response.status_code == 403, "the same token must now be insufficient"


def test_a_revoked_grant_takes_effect_on_the_next_request(
    app_client, store, make_user, bearer, make_fact
):
    store.insert_facts([make_fact(entity_id="secl")])
    officer = make_user("secl.officer", role=Role.OFFICER, entities=("secl",))
    headers = bearer(officer)

    assert len(app_client.get("/api/facts", headers=headers).json()) == 1

    store.users.revoke_scope(officer.user_id, "secl")

    assert app_client.get("/api/facts", headers=headers).json() == []


def test_a_viewer_cannot_resolve_a_conflict(
    app_client, store, make_user, bearer, make_fact
):
    """Role is enforced at the route, not in the UI."""
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=191.0e6)])
    conflict = store.detect_conflicts(Scope.unrestricted("test"))[0]
    viewer = make_user("v.viewer", role=Role.VIEWER, entities=(SCOPE_ALL,))

    response = app_client.post(
        f"/api/conflicts/{conflict.conflict_id}/resolve",
        json={"winning_fact_id": conflict.facts[0].fact_id},
        headers=bearer(viewer),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "insufficient_role"


def test_a_reviewer_can_resolve_and_is_named_in_the_trail(
    app_client, store, make_user, bearer, make_fact
):
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=191.0e6)])
    conflict = store.detect_conflicts(Scope.unrestricted("test"))[0]
    reviewer = make_user("r.reviewer", role=Role.REVIEWER, entities=(SCOPE_ALL,))

    response = app_client.post(
        f"/api/conflicts/{conflict.conflict_id}/resolve",
        json={"winning_fact_id": conflict.facts[0].fact_id, "note": "audited figure"},
        headers=bearer(reviewer),
    )

    assert response.status_code == 200
    trail = store.audit.recent(action="conflict.resolved")
    assert trail[0].actor_username == "r.reviewer"
    assert trail[0].subject_id == conflict.conflict_id


def test_a_temporary_password_buys_only_a_password_change(app_client, make_user, bearer):
    """An administrator who chose the password knows it, so it is not yet the
    holder's credential."""
    user = make_user("new.joiner", entities=(SCOPE_ALL,), must_change_password=True)
    headers = bearer(user)

    blocked = app_client.get("/api/facts", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "password_change_required"

    # The exempt endpoints still work, or the account could never recover.
    assert app_client.get("/api/auth/me", headers=headers).status_code == 200
    changed = app_client.post(
        "/api/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": GOOD_PASSWORD},
        headers=headers,
    )
    assert changed.status_code == 204


def test_only_an_admin_may_list_accounts(app_client, make_user, bearer):
    make_user("r.reviewer", role=Role.REVIEWER)
    admin = make_user("a.admin", role=Role.ADMIN, entities=(SCOPE_ALL,))

    assert (
        app_client.get(
            "/api/auth/users", headers=bearer(make_user("v.viewer", role=Role.VIEWER))
        ).status_code
        == 403
    )
    listed = app_client.get("/api/auth/users", headers=bearer(admin))
    assert listed.status_code == 200
    assert {row["username"] for row in listed.json()} >= {"a.admin", "r.reviewer"}
    assert all("password" not in row for row in listed.json()), "no hash, ever"


def test_creating_an_account_over_http_grants_scope_and_audits(
    app_client, store, make_user, bearer
):
    admin = make_user("a.admin", role=Role.ADMIN, entities=(SCOPE_ALL,))

    response = app_client.post(
        "/api/auth/users",
        json={
            "username": "N.Newjoiner",
            "password": GOOD_PASSWORD,
            "role": "officer",
            "entities": ["secl"],
        },
        headers=bearer(admin),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "n.newjoiner", "usernames are folded to lower case"
    assert body["entities"] == ["secl"]
    assert body["must_change_password"] is True

    trail = store.audit.recent(action="user.created")
    assert trail[0].actor_username == "a.admin"


def test_a_weak_initial_password_is_refused_with_its_reasons(
    app_client, make_user, bearer
):
    admin = make_user("a.admin", role=Role.ADMIN, entities=(SCOPE_ALL,))

    response = app_client.post(
        "/api/auth/users",
        json={"username": "n.newjoiner", "password": "Welcome1", "role": "viewer"},
        headers=bearer(admin),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reasons"]


def test_the_last_administrator_cannot_be_demoted(app_client, make_user, bearer):
    admin = make_user("a.admin", role=Role.ADMIN, entities=(SCOPE_ALL,))

    response = app_client.patch(
        f"/api/auth/users/{admin.user_id}",
        json={"role": "viewer"},
        headers=bearer(admin),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "last_admin"


# ------------------------------------------------------------------ audit log


def test_a_failed_login_is_recorded_with_its_real_reason(store, make_user):
    make_user("s.kumar")
    authenticate(store, "s.kumar", "wrong", source_ip="10.1.2.3")

    [entry] = store.audit.recent(action="auth.login_failed")
    assert entry.actor_username == "s.kumar"
    assert entry.detail["reason"] == AuthFailure.WRONG_PASSWORD
    assert entry.source_ip == "10.1.2.3"


def test_a_login_by_an_unknown_username_is_still_recorded(store):
    """Otherwise a password-spraying run against invented usernames is invisible."""
    authenticate(store, "attacker", "guess")

    [entry] = store.audit.recent(action="auth.login_failed")
    assert entry.actor_username == "attacker"
    assert entry.actor_user_id is None


def test_the_audit_log_cannot_be_rewritten(store, make_user):
    """Enforced by a trigger, so it holds for anything holding a connection."""
    import sqlalchemy as sa

    make_user("s.kumar")
    authenticate(store, "s.kumar", TEST_PASSWORD)

    with pytest.raises(Exception, match=r"append-only|audit"):
        store.connection.execute(
            sa.text("UPDATE audit_log SET action = 'auth.nothing_happened'")
        )


def test_only_an_admin_may_read_the_trail(app_client, make_user, bearer):
    reviewer = make_user("r.reviewer", role=Role.REVIEWER, entities=(SCOPE_ALL,))
    admin = make_user("a.admin", role=Role.ADMIN, entities=(SCOPE_ALL,))

    assert app_client.get("/api/auth/audit", headers=bearer(reviewer)).status_code == 403
    assert app_client.get("/api/auth/audit", headers=bearer(admin)).status_code == 200


# --------------------------------------------------- lockout persistence (real tx)


@pytest.mark.integration
def test_failed_logins_persist_the_lockout_counter_through_the_real_transaction(
    database_url: str,
) -> None:
    """A failed login must COMMIT its counter and audit row, not roll them back.

    The regression this pins: ``login`` used to ``raise HTTPException`` on a bad
    attempt, which propagates through ``provide_store``'s ``engine.begin()`` as
    an error and rolls the whole request transaction back — discarding the
    failure counter and the audit row, so the lockout never engaged and nothing
    was audited. The other auth tests cannot catch it because they override
    ``provide_store`` with a shared connection that has no commit/rollback
    boundary. This one drives the real dependency against a committed user, so a
    revert to ``raise`` fails it.
    """
    from sqlalchemy import text

    from mrip.db.engine import transaction
    from mrip.db.store import Store

    username = f"lockout_regression_{int(datetime.now(UTC).timestamp() * 1000)}"
    with transaction() as conn:
        record = Store(conn).users.create(
            username,
            role=Role.OFFICER,
            password_hash=passwords.hash_password(GOOD_PASSWORD),
        )
        Store(conn).users.replace_scopes(record.user_id, ["secl"])
        user_id = record.user_id

    try:
        # The real dependency chain: no provide_store override, so each request
        # runs in its own engine.begin() transaction, exactly like production.
        app = create_app()
        with TestClient(app) as client:
            codes = [
                client.post(
                    "/api/auth/login",
                    json={"username": username, "password": "wrong"},
                ).status_code
                for _ in range(MAX_FAILED_LOGINS + 1)
            ]

        assert codes == [401] * (MAX_FAILED_LOGINS + 1)

        with transaction() as conn:
            user = Store(conn).users.by_username(username)
            assert user is not None
            assert user.failed_login_count >= MAX_FAILED_LOGINS
            assert user.locked_until is not None  # the lockout actually engaged
            failed = [
                row
                for row in Store(conn).audit.recent(limit=100, action="auth.login_failed")
                if row.actor_username == username
            ]
            assert len(failed) >= MAX_FAILED_LOGINS  # and every failure was audited
    finally:
        # Self-cleaning: the audit log is append-only by trigger, so removing the
        # probe's rows needs the same deliberate exception Store.reset documents.
        with transaction() as conn:
            conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
            try:
                conn.execute(
                    text("DELETE FROM audit_log WHERE actor_user_id = :u"),
                    {"u": user_id},
                )
                conn.execute(
                    text("DELETE FROM user_scopes WHERE user_id = :u"), {"u": user_id}
                )
                conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user_id})
            finally:
                conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))
