"""user security columns

Revision ID: 0002
Revises: 0001
Created: 2026-09-24

Adds what the identity layer needs and the baseline did not have: a forced
password change, a session epoch for revoking tokens already issued, and a
database-side lockout counter.

The counter lives here rather than in process memory because two API replicas
would otherwise each grant a fresh allowance of guesses — a lockout whose total
depends on how many containers are running is not a lockout.

The ``username = lower(username)`` constraint is added with a rewrite of any
existing rows first, so an upgrade on a deployment that already created mixed-case
accounts succeeds instead of failing the constraint check. There is no such
deployment yet; a migration that assumes its table is empty is a migration that
fails the first time it matters.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "session_epoch", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )

    # Fold any pre-existing mixed-case usernames before the constraint applies.
    # Duplicates that differ only by case would violate the unique index, which
    # is the correct failure: two accounts that should always have been one need
    # a human to decide which grants survive.
    op.execute(
        "UPDATE users SET username = lower(username) WHERE username <> lower(username)"
    )
    op.create_check_constraint(
        "usernames_are_lowercased", "users", "username = lower(username)"
    )


def downgrade() -> None:
    # The bare name, not the rendered one: Alembic applies the metadata's naming
    # convention here too, so passing "ck_users_…" would ask for
    # "ck_users_ck_users_…".
    op.drop_constraint("usernames_are_lowercased", "users", type_="check")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
    op.drop_column("users", "session_epoch")
    op.drop_column("users", "must_change_password")
