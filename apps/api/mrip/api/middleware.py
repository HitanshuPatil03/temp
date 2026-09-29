"""HTTP middleware: request identity and the access log.

Every request gets an id, and that id appears on **every** log line produced
while serving it — including lines from library code — because it is bound into
a contextvar rather than passed around. When a reviewer reports "the conflict
page showed an error at 14:32", one grep answers what happened.

The id is also written to the ``X-Request-ID`` response header and into the audit
log, so a browser network tab, a log line and an audit row can be tied together
without timestamps-and-hope.

An inbound ``X-Request-ID`` is honoured so a trace survives the Next.js proxy
hop, but it is **sanitized first**: an id echoed into logs and headers is
attacker-controlled input, and a newline in it would let a caller forge log
lines.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from mrip import log

__all__ = ["REQUEST_ID_HEADER", "RequestContextMiddleware", "new_request_id"]

REQUEST_ID_HEADER = "X-Request-ID"

#: Conservative: anything outside this is replaced with a fresh id rather than
#: escaped, because there is no legitimate caller that needs the rest.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

#: Probes run every few seconds. Logging them at info drowns the stream, so they
#: are logged at debug unless they fail.
_QUIET_PATHS = frozenset({"/api/health", "/api/ready"})


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind request context, emit one access line, always clear up."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._logger = log.get_logger("mrip.access")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = supplied if _SAFE_REQUEST_ID.match(supplied) else new_request_id()

        # Stashed on the request too: audit rows record the request id, and a
        # route handler should not have to reach into contextvars for it.
        request.state.request_id = request_id

        log.clear()
        log.bind(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Logged here, where the request context is still bound; FastAPI's
            # own handler turns it into a 500 afterwards.
            self._logger.exception(
                "Request failed", elapsed_ms=round((time.perf_counter() - started) * 1000)
            )
            log.clear()
            raise

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        response.headers[REQUEST_ID_HEADER] = request_id

        quiet = request.url.path in _QUIET_PATHS and response.status_code < 400
        level = (
            self._logger.debug
            if quiet
            else (
                self._logger.warning if response.status_code >= 500 else self._logger.info
            )
        )
        level("request", status=response.status_code, elapsed_ms=elapsed_ms)

        log.clear()
        return response
