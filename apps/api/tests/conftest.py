"""Shared test fixtures.

Tests run against a **real PostgreSQL**, not a substitute. Everything that makes
this schema trustworthy — partial unique indexes, the composite foreign key from
facts to document versions, ``LEAST`` ignoring nulls, the audit log's append-only
trigger — is behaviour a stand-in would not reproduce, so a suite that passed
against one would be testing a different system.

Isolation is a **transaction rolled back per test**, not a truncate. It is faster,
and it means a test cannot leave state behind even if it fails mid-way.

The schema is built by ``alembic upgrade head`` rather than ``create_all``, so
every run exercises the migration that production will run.

Set ``MRIP_TEST_DATABASE_URL`` to point elsewhere. With no PostgreSQL reachable,
database-backed tests skip with an actionable message and the pure-logic suites
(normalizers, schemas) still run.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection

from mrip.auth.scope import Scope
from mrip.db.repositories.documents import new_id
from mrip.schemas import (
    BBox,
    Confidence,
    Document,
    DocumentClass,
    EvidenceRef,
    ExtractionMethod,
    Fact,
)

API_ROOT = Path(__file__).resolve().parents[1]

#: Defaults to a separate database on the development server, which is already
#: running via `docker compose up -d postgres`. CI overrides this.
DEFAULT_TEST_URL = "postgresql+psycopg://mrip:mrip_dev_only@127.0.0.1:5432/mrip_test"

#: Timezone-aware on purpose. Columns are `timestamptz`, and a naive timestamp in
#: a system whose entire job is fiscal-period arithmetic is a latent bug: "1 Apr
#: 2026 00:00" falls in a different fiscal year depending on the reader's zone.
FIXED_TIME = datetime(2026, 4, 1, 9, 30, 0, tzinfo=UTC)

#: Tests run unscoped unless they are specifically about access control.
TEST_SCOPE = Scope.unrestricted("test fixture")


def _test_url() -> str:
    """Where the test database lives.

    Checked in order: the ``MRIP_TEST_DATABASE_URL`` environment variable (what
    CI sets), then the same key in ``apps/api/.env``, then the development
    default.

    The ``.env`` step is not a convenience — it is what makes a WSL2 developer
    machine work. The database's address there is assigned per boot, so the URL
    cannot be a constant; ``infra/wsl-db-env.sh`` writes the current one into
    ``.env``, and a suite that only read ``os.environ`` would ignore it and time
    out against ``127.0.0.1`` with a message about Docker.
    """
    from_environment = os.environ.get("MRIP_TEST_DATABASE_URL")
    if from_environment:
        return from_environment

    env_file = API_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "MRIP_TEST_DATABASE_URL" and value.strip():
                return value.strip()

    return DEFAULT_TEST_URL


def _ensure_database(url: str) -> None:
    """Create the test database if it does not exist.

    Connects to the server's default database to issue ``CREATE DATABASE``, which
    cannot run inside a transaction — hence the AUTOCOMMIT isolation level.
    """
    target = sa.engine.make_url(url)
    admin = target.set(database="postgres")
    engine = sa.create_engine(
        admin, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool
    )
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                # The identifier cannot be parameterized; it comes from our own
                # configuration, not from request input.
                conn.execute(sa.text(f'CREATE DATABASE "{target.database}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    """A migrated test database, or a skip with a message that says what to run."""
    url = _test_url()

    # Point the whole process at the test database before anything reads settings.
    os.environ["MRIP_DATABASE_URL"] = url
    os.environ["MRIP_PROFILE"] = "test"

    from mrip.config import get_settings

    get_settings.cache_clear()

    try:
        _ensure_database(url)
    except Exception as cause:
        pytest.skip(
            "No PostgreSQL reachable for the test suite "
            f"({type(cause).__name__}: {cause}). "
            "Start one with: docker compose up -d postgres  — or set "
            "MRIP_TEST_DATABASE_URL.",
            allow_module_level=True,
        )

    from alembic import command
    from alembic.config import Config

    from mrip.db.engine import dispose_engine

    dispose_engine()

    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    # Start from a clean schema so a half-migrated leftover from an interrupted
    # run cannot make the suite pass or fail for the wrong reason.
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    yield url

    dispose_engine()


@pytest.fixture
def connection(database_url: str) -> Iterator[Connection]:
    """A connection inside a transaction that is always rolled back."""
    from mrip.db.engine import get_engine

    with get_engine().connect() as conn:
        outer = conn.begin()
        try:
            yield conn
        finally:
            outer.rollback()


@pytest.fixture
def store(connection: Connection):
    """A store over the rolled-back transaction. Fresh per test."""
    from mrip.db import Store

    return Store(connection)


@pytest.fixture
def scope() -> Scope:
    return TEST_SCOPE


# ------------------------------------------------------------------- identity

#: The password every test account shares. Hashing is Argon2id at 19 MiB, which
#: costs ~50 ms — so it is hashed **once per session** and the string reused.
#: Hashing per test would add minutes to the suite for no extra coverage; the
#: login path's real hashing is exercised in tests/test_auth.py.
TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="session")
def test_password_hash() -> str:
    from mrip.auth.passwords import hash_password

    return hash_password(TEST_PASSWORD)


@pytest.fixture
def make_user(store, test_password_hash: str):
    """Factory for accounts. Defaults to a reviewer with an HQ-wide grant.

    The grant is explicit rather than implied: an account with no ``user_scopes``
    row reads an empty corpus by design, so a fixture that forgot to grant
    anything would make every scoped assertion trivially pass.
    """
    from mrip.auth.scope import SCOPE_ALL
    from mrip.schemas import Role

    def _make(
        username: str = "r.reviewer",
        *,
        role: Role = Role.REVIEWER,
        entities: tuple[str, ...] = (SCOPE_ALL,),
        password_hash: str | None = None,
        **overrides,
    ):
        record = store.users.create(
            username,
            role=role,
            password_hash=password_hash or test_password_hash,
            **overrides,
        )
        store.users.replace_scopes(record.user_id, list(entities))
        return record

    return _make


@pytest.fixture
def bearer():
    """Mint an ``Authorization`` header for a user record.

    Uses the real token issuer and the real dependency chain (decode → load user
    → read grants), skipping only the password verification that
    tests/test_auth.py covers directly.
    """
    from mrip.auth.tokens import issue_access_token

    def _headers(record) -> dict[str, str]:
        token, _ = issue_access_token(record.user_id, session_epoch=record.session_epoch)
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture
def make_document():
    """Factory for documents. Content hash is derived from the filename."""

    def _make(filename: str = "cil-annual-report-2024-25.pdf", **overrides) -> Document:
        defaults: dict[str, object] = {
            "document_id": new_id("doc"),
            "content_hash": hashlib.sha256(filename.encode()).hexdigest(),
            "filename": filename,
            "doc_class": DocumentClass.TEXT_PDF,
            "page_count": 180,
            "size_bytes": 4_200_000,
            "title": "Coal India Limited Annual Report FY2024-25",
            "publisher_entity_id": "cil",
            "fiscal_year": "FY2024-25",
            "ingested_at": FIXED_TIME,
        }
        return Document(**{**defaults, **overrides})

    return _make


#: The document every default fact cites. A fixed id, because several tests
#: assert on the rendered locator string.
FIXTURE_DOCUMENT_ID = "doc_fixture"


@pytest.fixture
def make_fact(store, make_document):
    """Factory for facts. Defaults describe SECL coal production in FY2024-25.

    The default source document is **registered on first use**. That is not
    convenience: ``facts`` carries a composite foreign key to
    ``documents(document_id, version)``, so a fact citing a document that was
    never registered is now rejected by the database. The old DuckDB schema had
    no such constraint and let tests create unciteable facts — exactly the state
    the project's central invariant forbids.

    ``document_id`` is a shorthand that rewrites the evidence ref, since
    re-pointing a fact at another source document is a common test need. A
    caller passing it is expected to have registered that document itself.
    """
    registered: list[str] = []

    def _make(*, document_id: str | None = None, **overrides) -> Fact:
        if document_id is None:
            if not registered:
                store.register_document(
                    make_document("fixture-source.pdf", document_id=FIXTURE_DOCUMENT_ID)
                )
                registered.append(FIXTURE_DOCUMENT_ID)
            document_id = FIXTURE_DOCUMENT_ID

        defaults: dict[str, object] = {
            "fact_id": new_id("fact"),
            "entity_id": "secl",
            "metric": "coal_production",
            "value": 193.0e6,
            "unit": "t",
            "dimension": "mass",
            "raw_value": 193.0,
            "raw_unit": "MT",
            "period_start": date(2024, 4, 1),
            "period_end": date(2025, 3, 31),
            "period_label": "FY2024-25",
            "fiscal_year": "FY2024-25",
            "evidence": EvidenceRef(
                document_id=document_id,
                page=47,
                table_id="t1",
                cell_ref="r3c2",
                bbox=BBox(x0=72.0, y0=310.5, x1=148.25, y1=324.0),
                snippet="SECL 193.00",
            ),
            "extraction_method": ExtractionMethod.TABLE_LATTICE,
            "confidence": Confidence(parse=0.97),
            "unit_ambiguous": True,
            "extracted_at": FIXED_TIME,
        }
        fact = Fact(**{**defaults, **overrides})
        return fact.model_copy(
            update={
                "evidence": fact.evidence.model_copy(update={"document_id": document_id})
            }
        )

    return _make
