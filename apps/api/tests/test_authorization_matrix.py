"""Every role-guarded mutating route refuses the role one rank below it.

The individual route tests check a guard where it matters most (a viewer
cannot generate a report, a reviewer cannot approve). This file is the
*systematic* companion: one parametrised case per mutating route, each driven
by a caller holding exactly the rank immediately beneath the requirement.

The failure it exists to catch is a route that quietly loses its ``require_role``
dependency — a refactor drops the guard, every happy-path test still passes
because they call as an admin-equivalent, and the hole ships. Here, dropping a
guard turns a 403 into a 2xx/404/422 and this test goes red.

Roles are a linear rank (viewer < officer < reviewer < approver < admin), so the
caller one below is the strongest proof: they are *almost* allowed, and the only
thing standing between them and the action is the guard under test. Bodies are
valid enough to reach the guard; path ids point at nothing, because the role
dependency resolves before the handler ever looks a resource up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_read_only_store, provide_store
from mrip.auth.scope import SCOPE_ALL
from mrip.db import Store
from mrip.main import create_app
from mrip.schemas import Role

# (label, method, path, required role, the rank immediately below it, body)
#
# Written out rather than derived from the routes, deliberately: the test is a
# second, independent statement of what each route *should* require, so a change
# to a guard has to be made in two places that must agree.
GUARDED_ROUTES: list[tuple[str, str, str, Role, Role, dict[str, Any] | None]] = [
    (
        "generate report",
        "POST",
        "/api/reports",
        Role.OFFICER,
        Role.VIEWER,
        {"entity": "secl", "period": "FY2024-25"},
    ),
    (
        "transition report",
        "POST",
        "/api/reports/rpt_x/transition",
        Role.APPROVER,
        Role.REVIEWER,
        {"state": "in_review"},
    ),
    (
        "review fact",
        "POST",
        "/api/facts/fct_x/review",
        Role.REVIEWER,
        Role.OFFICER,
        {"decision": "validate"},
    ),
    (
        "detect conflicts",
        "POST",
        "/api/conflicts/detect",
        Role.REVIEWER,
        Role.OFFICER,
        None,
    ),
    (
        "resolve conflict",
        "POST",
        "/api/conflicts/cfl_x/resolve",
        Role.REVIEWER,
        Role.OFFICER,
        {"winning_fact_id": "fct_x"},
    ),
    ("extract topics", "POST", "/api/topics/extract", Role.OFFICER, Role.VIEWER, None),
    (
        "retry document",
        "POST",
        "/api/documents/doc_x/retry",
        Role.OFFICER,
        Role.VIEWER,
        None,
    ),
    (
        "create user",
        "POST",
        "/api/auth/users",
        Role.ADMIN,
        Role.APPROVER,
        {"username": "nobody", "password": "irrelevant", "role": "viewer"},
    ),
    (
        "update user",
        "PATCH",
        "/api/auth/users/usr_x",
        Role.ADMIN,
        Role.APPROVER,
        {"role": "officer"},
    ),
]


@pytest.fixture
def app_client(store: Store, make_user, bearer) -> Iterator[Any]:
    """A client factory that authenticates as any role, HQ-wide.

    HQ-wide scope is deliberate: it removes scope as a confound, so a 403 is
    unambiguously the *role* guard and never a scope miss masquerading as one.
    """
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store

    def _as(role: Role) -> TestClient:
        operator = make_user(f"matrix.{role.value}", role=role, entities=(SCOPE_ALL,))
        return TestClient(app, headers=bearer(operator))

    yield _as


@pytest.mark.parametrize(
    ("label", "method", "path", "required", "one_below", "body"),
    GUARDED_ROUTES,
    ids=[case[0] for case in GUARDED_ROUTES],
)
def test_the_rank_below_the_requirement_is_refused(
    app_client, label, method, path, required: Role, one_below: Role, body
) -> None:
    """A caller one rank short of a mutating route is refused with 403."""
    client = app_client(one_below)
    response = client.request(method, path, json=body)
    assert response.status_code == 403, (
        f"{label}: {one_below.value} (one below {required.value}) should be "
        f"refused, got {response.status_code} — the role guard may be missing"
    )


@pytest.mark.parametrize(
    ("label", "method", "path", "required", "one_below", "body"),
    GUARDED_ROUTES,
    ids=[case[0] for case in GUARDED_ROUTES],
)
def test_the_required_rank_passes_the_guard(
    app_client, label, method, path, required: Role, one_below: Role, body
) -> None:
    """The flip side: the required rank clears the guard, so a 403 above is the
    role check and not a route that refuses everyone.

    The call stops at a missing resource or empty corpus (404/409/422/2xx) —
    anything but 403, which is the one answer the guard owns.
    """
    client = app_client(required)
    response = client.request(method, path, json=body)
    assert response.status_code != 403, (
        f"{label}: {required.value} meets the requirement yet was refused — "
        f"the guard is stricter than its stated role"
    )
