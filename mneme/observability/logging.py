"""JSON structured logging for Mneme.

Produces one JSON object per log line with these fields (minimum):

* ``timestamp`` – ISO-8601 with timezone
* ``level`` – uppercase log level name
* ``logger`` – logger name
* ``message`` – log message
* ``request_id`` – from request context (``"-"`` when unavailable)
* ``correlation_id`` – from request context (``"-"`` when unavailable)
* ``actor_type`` – from request context (``"-"`` when unavailable)

When the :class:`AccessLogMiddleware` is installed, access-log records
additionally carry ``route``, ``method``, ``status_code``, and
``duration_ms``.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Any

from mneme.api.context import peek_request_context


class JsonFormatter(logging.Formatter):
    """Format each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        context = peek_request_context()
        base: dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": str(context.request_id) if context else "-",
            "correlation_id": str(context.correlation_id) if context else "-",
            "actor_type": context.actor.actor_type if context else "-",
        }

        # Carry access-log extras when they are attached to the record.
        for attr in ("route", "method", "status_code", "duration_ms"):
            if hasattr(record, attr):
                base[attr] = getattr(record, attr)

        # Attach exception info when present (but never log the full
        # stack-trace string – keep the record line compact).
        if record.exc_info and record.exc_info[1]:
            base["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }

        return json.dumps(base, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with JSON structured output.

    Removes any pre-existing handlers to avoid duplicate lines, then adds a
    single ``StreamHandler`` using :class:`JsonFormatter`.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Remove existing handlers but keep the new JSON handler idempotent.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # Keep third-party loggers quieter by default.
    for noisy in ("uvicorn", "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class AccessLogMiddleware:
    """Pure-ASGI middleware that logs every request as a structured JSON line.

    The emitted log record includes ``route``, ``method``, ``status_code``,
    and ``duration_ms`` in addition to the standard context fields.

    Usage::

        from mneme.observability.logging import AccessLogMiddleware
        app.add_middleware(AccessLogMiddleware)
    """

    def __init__(self, app, *, logger_name: str = "mneme.access") -> None:
        self.app = app
        self.logger = logging.getLogger(logger_name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()

        # Capture response status
        response_status: int = 0

        async def _send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            response_status = 500
            raise
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            route = scope.get("path", "")
            method = scope.get("method", "")

            self.logger.info(
                "%s %s -> %s %.2fms",
                method,
                route,
                response_status,
                duration_ms,
                extra={
                    "route": route,
                    "method": method,
                    "status_code": response_status,
                    "duration_ms": duration_ms,
                },
            )
