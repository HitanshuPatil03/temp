"""FastAPI application entry point.

Run with::

    python -m uvicorn mrip.main:app --reload --port 8000

Every route lives under ``/api`` so the Next.js server can proxy the prefix
without colliding with page routes.

Startup deliberately **fails loudly**: if the database is unreachable or its
schema is behind the migration history, the process refuses to serve rather than
answering requests against a schema it does not match. A reporting system that
returns a plausible wrong number is worse than one that is visibly down.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mrip import log
from mrip.api import auth, documents, facts, normalize, query, system, uploads
from mrip.api.middleware import RequestContextMiddleware
from mrip.config import get_settings
from mrip.db import ping
from mrip.db.schema_version import require_schema_version
from mrip.jobs import handlers  # noqa: F401 — importing registers the job kinds

API_PREFIX = "/api"

logger = log.get_logger("mrip.api")

DESCRIPTION = """
Evidence-first reporting intelligence for the Indian coal sector (SIH26023).

Every figure this API returns carries an `evidence` reference naming the
document, page, table and cell it came from. Facts that disagree are reported as
conflicts for a reviewer rather than merged into a single number.

Confidence is returned as three independent stages (`ocr`, `parse`, `answer`) and
is never collapsed into one score — the weakest stage is the honest headline.
""".strip()


def _warm_llm_background() -> None:
    """Load the model so the first query does not pay cold-start. Best-effort."""
    try:
        from mrip.config import get_settings as _gs
        from mrip.llm import client_from_settings

        client_from_settings(_gs()).warm()
    except Exception:  # noqa: S110
        pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify the environment before accepting traffic, and release it after."""
    settings = get_settings()
    log.configure_logging(settings)
    settings.ensure_dirs()

    if not ping():
        raise RuntimeError(
            "Cannot reach PostgreSQL at the configured MRIP_DATABASE_URL. "
            "Start it with: docker compose up -d postgres"
        )

    version = require_schema_version()
    logger.info(
        "api starting",
        profile=settings.profile.value,
        schema=version.describe(),
        blob_dir=str(settings.blob_dir),
    )
    # Warm the local model in the background — don't block startup on Ollama.
    # A cold model is slow, not broken; the first narrative query will just
    # take longer if this hasn't finished.
    if settings.llm_enabled:
        threading.Thread(target=_warm_llm_background, daemon=True).start()
    yield
    # The engine's lifetime belongs to the process, not to the application
    # object. Disposing it here would also tear down a pool that a test — which
    # constructs and discards several apps against one engine — is still using,
    # and on a real shutdown the process is about to exit anyway. Workers call
    # `dispose_engine()` explicitly around forking, where it genuinely matters.


def create_app() -> FastAPI:
    """Build the application. A factory so tests can construct it in isolation."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.2.0",
        lifespan=lifespan,
    )
    # Outermost: every other middleware's log lines should carry the request id,
    # and middleware runs in reverse registration order.
    app.add_middleware(RequestContextMiddleware)
    # In deployment the browser talks only to Next, which proxies `/api/*`
    # same-origin, so CORS stays a development affordance.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (system, auth, documents, uploads, facts, normalize, query):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()
