"""Structured logging.

Every log line carries `request_id`, `session_id` (when known) and a `component`
so an operator can grep a single failing conversation across the model,
retrieval, database and artifact layers. JSON in prod, pretty console in dev.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)


def _add_context(_logger, _method, event_dict):  # type: ignore[no-untyped-def]
    rid = request_id_var.get()
    sid = session_id_var.get()
    if rid:
        event_dict.setdefault("request_id", rid)
    if sid:
        event_dict.setdefault("session_id", sid)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    processors = [
        structlog.contextvars.merge_contextvars,
        _add_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    # Route stdlib logging (uvicorn, sqlalchemy) through the same renderer.
    logging.basicConfig(level=level.upper(), stream=sys.stderr, format="%(message)s")
    for noisy in ("uvicorn.access", "httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger().bind(component=component)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, times the request and emits one access log line."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        log = get_logger("http")
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request_failed", path=request.url.path, method=request.method)
            raise
        finally:
            request_id_var.reset(token)
            session_id_var.set(None)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        if not request.url.path.startswith("/health"):
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        response.headers["x-request-id"] = rid
        return response
