"""P4-10 Standalone Memory Auto-Extract Worker – deployable on port 199.

This is a lightweight entry-point that:
1. Starts the MemoryAutoExtractSweeper as a background thread.
2. Runs a minimal HTTP server on port 199 for health/stats verification.

Usage::

    python3 -m mneme.worker.memory_worker
    # or:
    python3 mneme/worker/memory_worker.py

Then verify::

    curl http://localhost:199/health
    curl http://localhost:199/stats
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from mneme.config import get_settings
from mneme.db.base import check_database_connection
from mneme.logging import configure_logging
from mneme.worker.memory_auto_extract import (
    MemoryAutoExtractSweeper,
    get_unprocessed_stats,
    create_memory_auto_extract_sweeper,
)

logger = logging.getLogger("mneme.memory_worker")

_running = True


def _stop(signum, frame):
    global _running
    logger.info("received signal %s – shutting down", signum)
    _running = False


class StatsHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for health/stats on port 199."""

    sweeper: MemoryAutoExtractSweeper | None = None
    sweep_count: int = 0
    last_sweep_result: dict | None = None

    def log_message(self, fmt, *args):
        """Suppress access logs or redirect to our logger."""
        logger.debug("memory_worker_http: " + fmt, *args)

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self._json_response({
                "status": "ok",
                "service": "memory-auto-extract-worker",
                "running": _running,
                "sweep_count": StatsHandler.sweep_count,
            })
        elif self.path == "/stats":
            stats = get_unprocessed_stats()
            self._json_response({
                "unprocessed_messages": stats.get("unprocessed_messages", -1),
                "sweep_count": StatsHandler.sweep_count,
                "last_sweep": StatsHandler.last_sweep_result,
            })
        elif self.path == "/ready":
            try:
                check_database_connection()
                self._json_response({"status": "ready", "database": "ok"})
            except Exception as exc:
                self._json_response(
                    {"status": "not_ready", "database": str(exc)[:200]},
                    status=503,
                )
        else:
            self._json_response({"error": "not found"}, status=404)


def run_http_server(port: int = 199):
    """Run a minimal HTTP server for health probes."""
    server = HTTPServer(("0.0.0.0", port), StatsHandler)
    logger.info("memory auto-extract HTTP server listening on port %s", port)

    # Non-blocking serve with shutdown support
    server.timeout = 1.0
    while _running:
        server.handle_request()
    server.server_close()
    logger.info("HTTP server stopped")


def run_sweeper_loop(sweeper: MemoryAutoExtractSweeper, interval: float):
    """Run the sweeper in a background loop."""
    logger.info(
        "memory auto-extract sweeper starting – interval=%.1fs window=%ss mode=%s",
        interval,
        sweeper.window_seconds,
        "instant" if sweeper.is_instant_mode else "batch",
    )

    while _running:
        try:
            result = sweeper.sweep()
            StatsHandler.sweep_count += 1
            StatsHandler.last_sweep_result = {
                "conversations_scanned": result.conversations_scanned,
                "conversations_processed": result.conversations_processed,
                "messages_extracted": result.messages_extracted,
                "candidates_submitted": result.candidates_submitted,
                "candidates_deduped": result.candidates_deduped,
                "errors": result.errors,
            }

            if result.conversations_processed or result.errors:
                logger.info(
                    "memory auto-extract – scanned=%d processed=%d "
                    "messages=%d candidates=%d deduped=%d errors=%d",
                    result.conversations_scanned,
                    result.conversations_processed,
                    result.messages_extracted,
                    result.candidates_submitted,
                    result.candidates_deduped,
                    result.errors,
                )
        except Exception as exc:
            logger.error("memory auto-extract sweep error: %s", exc, exc_info=True)
            StatsHandler.last_sweep_result = {
                "error": str(exc)[:500],
            }

        # Sleep in small increments for graceful shutdown
        end = time.monotonic() + interval
        while _running and time.monotonic() < end:
            time.sleep(min(1.0, max(0.1, interval / 5)))


def main():
    """Entry point for the standalone memory auto-extract worker."""
    global _running
    _running = True

    settings = get_settings()
    configure_logging(settings.log_level)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info(
        "memory auto-extract worker starting – env=%s port=199",
        settings.environment,
    )

    # Database check
    try:
        check_database_connection()
        logger.info("database connection ok")
    except Exception as exc:
        logger.critical("database unreachable – exiting: %s", exc)
        return

    # Create sweeper
    sweeper = create_memory_auto_extract_sweeper()
    StatsHandler.sweeper = sweeper

    interval = settings.worker_memory_auto_extract_interval_seconds

    # Start sweeper in background thread
    sweeper_thread = threading.Thread(
        target=run_sweeper_loop,
        args=(sweeper, interval),
        daemon=True,
        name="memory-auto-extract-sweeper",
    )
    sweeper_thread.start()

    # Run HTTP server in foreground (blocks until _running is False)
    try:
        run_http_server(port=199)
    except KeyboardInterrupt:
        pass

    _running = False
    sweeper_thread.join(timeout=interval + 5)
    logger.info("memory auto-extract worker stopped")


if __name__ == "__main__":
    main()
