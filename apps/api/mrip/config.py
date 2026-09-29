"""Runtime configuration.

Every value is overridable with an ``MRIP_``-prefixed environment variable.

The one thing this module does beyond holding values is **refuse to start a
production process with development defaults.** A deployment that silently runs on
a well-known JWT secret, or against a database URL someone forgot to set, is worse
than one that fails at boot — see :meth:`Settings.check_production_ready`.
"""

from __future__ import annotations

import functools
import secrets
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["REPO_ROOT", "Profile", "Settings", "get_settings"]

#: Repository root: apps/api/mrip/config.py -> up three levels is apps/api, four is root.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The development database password. Named as a constant so the production check
#: can reject it by identity rather than by a fuzzy "looks weak" heuristic.
DEV_DB_PASSWORD = "mrip_dev_only"  # noqa: S105 — deliberately public


class Profile(StrEnum):
    """Deployment profile. Controls which safety checks are fatal."""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class Settings(BaseSettings):
    """Application settings, overridable via environment or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="MRIP_",
        env_file=".env",
        extra="ignore",
        # A database URL is not something to get wrong because of a stray space.
        str_strip_whitespace=True,
    )

    profile: Profile = Profile.DEV

    app_name: str = "MRIP — Mining Reporting Intelligence Platform"
    problem_statement: str = "SIH26023"

    # ----------------------------------------------------------- datastore
    #: One PostgreSQL instance is the entire stateful backend: store of record,
    #: job queue, lexical search, vector search and audit log (ARCHITECTURE §4).
    database_url: PostgresDsn = Field(
        default=PostgresDsn(
            f"postgresql+psycopg://mrip:{DEV_DB_PASSWORD}@127.0.0.1:5432/mrip"
        )
    )
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)
    #: Emit every statement. Useful when writing a migration, intolerable in prod.
    db_echo: bool = False

    # --------------------------------------------------------------- paths
    data_dir: Path = REPO_ROOT / "data"
    #: Content-addressed blob store. Source documents are the evidence, so this
    #: directory is append-only and must be backed up with the database.
    blob_dir: Path | None = None
    #: Parquet snapshots for the optional DuckDB analyst sidecar (ARCHITECTURE §4).
    parquet_dir: Path | None = None

    # ------------------------------------------------------------ identity
    #: Signing key for session tokens. Generated per-process in dev so a developer
    #: never has to set one; **required** in prod, where a generated key would
    #: invalidate every session on restart and differ between API replicas.
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_ttl_minutes: int = Field(default=60, ge=5, le=1440)

    # --------------------------------------------------------------- HTTP
    #: Origins allowed to call the API directly. In deployment the browser talks
    #: only to Next, which proxies `/api/*`, so this stays a development affordance.
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    max_upload_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_upload_pages: int = Field(default=5_000, ge=1)

    # -------------------------------------------------------------- workers
    worker_concurrency: int = Field(default=2, ge=1, le=64)
    #: How long a claimed job may go without a heartbeat before another worker may
    #: reclaim it. Must exceed the longest plausible single-page OCR.
    job_lease_seconds: int = Field(default=300, ge=30)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_poll_seconds: float = Field(default=1.0, gt=0)

    # ------------------------------------------------------------ thresholds
    #: Facts whose *limiting* confidence falls below this go to the review queue
    #: rather than being treated as validated.
    review_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    #: Relative spread above which a conflict is material rather than a rounding
    #: difference between two sources.
    conflict_material_spread: float = Field(default=0.005, ge=0.0, le=1.0)

    # ----------------------------------------------------------- observability
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    # ------------------------------------------------------------------ models
    #: Base URL of the local model runtime. There is deliberately no setting for a
    #: hosted provider: MRIP performs no inference off the deployment host
    #: (ARCHITECTURE §3), and the figure path reaches no model at all (§7).
    llm_base_url: str = "http://127.0.0.1:11434"
    #: Qwen3 8B at Q4_K_M: 5.2 GB, Apache-2.0, 32K context, runs on CPU or a small
    #: GPU — which is what a CMPDI host is likely to have.
    llm_model: str = "qwen3:8b"
    #: Qwen3 reasons before answering unless told not to. The trace triples latency
    #: for prose that summarises figures already computed, so it is off by default
    #: and enabled per request on the drafting path, where structure matters more
    #: than speed.
    llm_thinking: bool = False
    #: Wall-clock ceiling for one generation. A narrative answer that takes longer
    #: than this is a worse answer than "the model is busy" — the figures were
    #: already returned by the deterministic path.
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    #: With the model runtime disabled, narrative answers are unavailable and every
    #: deterministic path must still work. CI runs the suite both ways.
    llm_enabled: bool = True

    @field_validator("data_dir", "blob_dir", "parquet_dir")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None

    @model_validator(mode="after")
    def _derive_paths(self) -> Self:
        if self.blob_dir is None:
            object.__setattr__(self, "blob_dir", self.data_dir / "blobs")
        if self.parquet_dir is None:
            object.__setattr__(self, "parquet_dir", self.data_dir / "parquet")
        return self

    # --------------------------------------------------------------- derived

    @property
    def is_production(self) -> bool:
        return self.profile is Profile.PROD

    @property
    def upload_staging_dir(self) -> Path:
        """Where an upload lands before it is hashed and moved into the blob store."""
        return self.data_dir / "staging"

    @property
    def report_dir(self) -> Path:
        """Where generated report deliverables are written."""
        return self.data_dir / "reports"

    @property
    def gold_dir(self) -> Path:
        """Where gold-corpus ground truth and benchmark queries live."""
        return self.data_dir / "gold"

    @property
    def sync_database_url(self) -> str:
        """The DSN as a plain string, for SQLAlchemy and Alembic."""
        return str(self.database_url)

    def ensure_dirs(self) -> None:
        """Create the runtime directories if they do not exist."""
        assert self.blob_dir is not None and self.parquet_dir is not None
        for path in (
            self.data_dir,
            self.blob_dir,
            self.parquet_dir,
            self.upload_staging_dir,
            self.report_dir,
            self.gold_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------ production gate

    def check_production_ready(self) -> list[str]:
        """Return the reasons this configuration must not serve production.

        Separated from validation so a developer can *read* the list without the
        process refusing to start. :func:`get_settings` raises on it when the
        profile is ``prod``.
        """
        problems: list[str] = []

        if not self.is_production:
            return problems

        dsn = str(self.database_url)
        if DEV_DB_PASSWORD in dsn:
            problems.append(
                "MRIP_DATABASE_URL still carries the development password; "
                "set a real credential."
            )
        if "@127.0.0.1" in dsn or "@localhost" in dsn:
            problems.append(
                "MRIP_DATABASE_URL points at localhost. If that is genuinely "
                "intended on this host, set it explicitly to the host's address."
            )
        # A default_factory secret differs per process, so sessions would break
        # across replicas and restarts. Detect it by absence from the environment.
        if "jwt_secret" not in self.model_fields_set:
            problems.append(
                "MRIP_JWT_SECRET is unset, so a random key was generated. Sessions "
                "would not survive a restart or span API replicas."
            )
        elif len(self.jwt_secret) < 32:
            problems.append("MRIP_JWT_SECRET is shorter than 32 characters.")
        if self.db_echo:
            problems.append("MRIP_DB_ECHO logs every statement, including fact values.")
        if self.log_format != "json":
            problems.append(
                "MRIP_LOG_FORMAT should be 'json' so logs are machine-readable."
            )
        if "*" in self.cors_origins:
            problems.append("MRIP_CORS_ORIGINS contains a wildcard.")

        return problems


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Raises ``RuntimeError`` if the profile is ``prod`` and the configuration is not
    fit to serve it. Failing at import time is the point: a misconfigured
    production process should never reach the first request.
    """
    settings = Settings()
    problems = settings.check_production_ready()
    if problems:
        listed = "\n".join(f"  - {problem}" for problem in problems)
        raise RuntimeError(
            f"MRIP_PROFILE=prod but the configuration is not production-ready:\n{listed}"
        )
    return settings
