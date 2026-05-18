"""In-process metrics with Prometheus exposition and system resource gauges.

Tracks:

* **request_count** – total HTTP requests, labelled by route, method, status_code
* **request_duration_seconds** – per-request wall-clock duration histogram
* **db_ready** – 1 when the last database health-check succeeded, else 0
* **redis_ready** – 1 when the last Redis health-check succeeded, else 0
* **outbox_pending** – number of pending outbox events (-1 = unknown)
* **system_memory_*** – system memory usage gauges (total, available, used, percent)
* **system_cpu_*** – system CPU utilization gauges
* **process_memory_*** – current process memory gauges (RSS, VMS)
* **process_cpu_*** – current process CPU utilization gauges

Two endpoints:

* ``GET /metrics`` – Prometheus text exposition format
* ``GET /api/v4/metrics`` – JSON snapshot (backward compatible)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = logging.getLogger(__name__)

# ── Global Prometheus registry ────────────────────────────────────────────────
_registry = CollectorRegistry(auto_describe=True)

# ── HTTP metrics ──────────────────────────────────────────────────────────────
request_count = Counter(
    "mneme_http_requests_total",
    "Total HTTP requests processed",
    labelnames=["method", "route", "status_code"],
    registry=_registry,
)

request_duration = Histogram(
    "mneme_http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=_registry,
)

error_count = Counter(
    "mneme_http_errors_total",
    "Total HTTP 5xx errors",
    labelnames=["method", "route"],
    registry=_registry,
)

# ── Dependency gauges ─────────────────────────────────────────────────────────
db_ready_gauge = Gauge(
    "mneme_db_ready",
    "Whether the database is reachable (1 = healthy, 0 = down)",
    registry=_registry,
)
db_ready_gauge.set(1)

redis_ready_gauge = Gauge(
    "mneme_redis_ready",
    "Whether Redis is reachable (1 = healthy, 0 = down)",
    registry=_registry,
)
redis_ready_gauge.set(1)

outbox_pending_gauge = Gauge(
    "mneme_outbox_pending",
    "Number of pending outbox events (-1 = unknown)",
    registry=_registry,
)
outbox_pending_gauge.set(-1)

# ── System memory gauges ──────────────────────────────────────────────────────
system_memory_total = Gauge(
    "mneme_system_memory_total_bytes",
    "Total system memory in bytes",
    registry=_registry,
)
system_memory_available = Gauge(
    "mneme_system_memory_available_bytes",
    "Available system memory in bytes",
    registry=_registry,
)
system_memory_used = Gauge(
    "mneme_system_memory_used_bytes",
    "Used system memory in bytes",
    registry=_registry,
)
system_memory_usage_percent = Gauge(
    "mneme_system_memory_usage_percent",
    "System memory usage percentage (0-100)",
    registry=_registry,
)

# ── System CPU gauges ─────────────────────────────────────────────────────────
system_cpu_usage_percent = Gauge(
    "mneme_system_cpu_usage_percent",
    "System CPU utilization percentage (0-100)",
    registry=_registry,
)
system_cpu_load_1m = Gauge(
    "mneme_system_cpu_load_1m",
    "System load average (1 min)",
    registry=_registry,
)
system_cpu_load_5m = Gauge(
    "mneme_system_cpu_load_5m",
    "System load average (5 min)",
    registry=_registry,
)
system_cpu_load_15m = Gauge(
    "mneme_system_cpu_load_15m",
    "System load average (15 min)",
    registry=_registry,
)

# ── Process memory gauges ─────────────────────────────────────────────────────
process_memory_rss = Gauge(
    "mneme_process_memory_rss_bytes",
    "Process RSS (resident set size) in bytes",
    registry=_registry,
)
process_memory_vms = Gauge(
    "mneme_process_memory_vms_bytes",
    "Process VMS (virtual memory size) in bytes",
    registry=_registry,
)
process_memory_rss_percent = Gauge(
    "mneme_process_memory_rss_percent",
    "Process RSS as percentage of total system memory",
    registry=_registry,
)

# ── Process CPU gauges ─────────────────────────────────────────────────────────
process_cpu_percent = Gauge(
    "mneme_process_cpu_percent",
    "Process CPU utilization percentage (0-100)",
    registry=_registry,
)
process_thread_count = Gauge(
    "mneme_process_thread_count",
    "Number of threads in the current process",
    registry=_registry,
)
process_open_fds = Gauge(
    "mneme_process_open_fds",
    "Number of open file descriptors (-1 = unavailable)",
    registry=_registry,
)

# ── DB pool gauges ─────────────────────────────────────────────────────────────
db_pool_size = Gauge(
    "mneme_db_pool_size",
    "Database connection pool max size",
    registry=_registry,
)
db_pool_checked_in = Gauge(
    "mneme_db_pool_checked_in",
    "Database connections currently checked in",
    registry=_registry,
)
db_pool_checked_out = Gauge(
    "mneme_db_pool_checked_out",
    "Database connections currently checked out",
    registry=_registry,
)
db_pool_overflow = Gauge(
    "mneme_db_pool_overflow",
    "Database connection pool overflow count",
    registry=_registry,
)
db_pool_total = Gauge(
    "mneme_db_pool_total_connections",
    "Total database connections (checked_in + checked_out)",
    registry=_registry,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Backward-compatible MetricsRegistry (for existing tests and code)
# ═══════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field
from collections import defaultdict
import threading


@dataclass
class RouteMetrics:
    count: int = 0
    error_count: int = 0
    duration_ms_total: float = 0.0


@dataclass
class MetricsSnapshot:
    request_count: int = 0
    error_count: int = 0
    request_duration_ms_total: float = 0.0
    request_duration_ms_max: float = 0.0
    routes: dict[str, RouteMetrics] = field(default_factory=dict)
    db_ready: int = 1
    redis_ready: int = 1
    outbox_pending: int = -1


class MetricsRegistry:
    """Thread-safe in-memory metrics store (backward compatible).

    Records metrics in both the Prometheus registry and a local snapshot
    for the existing ``snapshot()`` / ``as_dict()`` API used in tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = MetricsSnapshot()
        self._routes: dict[str, RouteMetrics] = defaultdict(RouteMetrics)

    def record_request(
        self,
        *,
        route: str,
        method: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        key = f"{method.upper()} {route}"
        with self._lock:
            self._snapshot.request_count += 1
            self._snapshot.request_duration_ms_total += duration_ms
            if duration_ms > self._snapshot.request_duration_ms_max:
                self._snapshot.request_duration_ms_max = duration_ms

            rm = self._routes[key]
            rm.count += 1
            rm.duration_ms_total += duration_ms

            if status_code >= 500:
                self._snapshot.error_count += 1
                rm.error_count += 1

        # Also record in Prometheus
        track_request(
            route=route,
            method=method,
            status_code=status_code,
            duration_ms=duration_ms,
        )

    def set_db_ready(self, ready: bool) -> None:
        with self._lock:
            self._snapshot.db_ready = 1 if ready else 0
        db_ready_gauge.set(1 if ready else 0)

    def set_redis_ready(self, ready: bool) -> None:
        with self._lock:
            self._snapshot.redis_ready = 1 if ready else 0
        redis_ready_gauge.set(1 if ready else 0)

    def set_outbox_pending(self, count: int) -> None:
        with self._lock:
            self._snapshot.outbox_pending = count
        outbox_pending_gauge.set(count)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            snap = MetricsSnapshot(
                request_count=self._snapshot.request_count,
                error_count=self._snapshot.error_count,
                request_duration_ms_total=self._snapshot.request_duration_ms_total,
                request_duration_ms_max=self._snapshot.request_duration_ms_max,
                routes=dict(self._routes),
                db_ready=self._snapshot.db_ready,
                redis_ready=self._snapshot.redis_ready,
                outbox_pending=self._snapshot.outbox_pending,
            )
        return snap

    def as_dict(self) -> dict[str, Any]:
        snap = self.snapshot()
        avg_duration = (
            round(snap.request_duration_ms_total / snap.request_count, 2)
            if snap.request_count > 0
            else 0.0
        )
        return {
            "requests": {
                "total": snap.request_count,
                "errors": snap.error_count,
                "duration_ms_avg": avg_duration,
                "duration_ms_max": snap.request_duration_ms_max,
            },
            "routes": {
                key: {
                    "count": rm.count,
                    "error_count": rm.error_count,
                    "duration_ms_avg": (
                        round(rm.duration_ms_total / rm.count, 2) if rm.count else 0.0
                    ),
                }
                for key, rm in snap.routes.items()
            },
            "dependencies": {
                "database": "ready" if snap.db_ready else "unavailable",
                "redis": "ready" if snap.redis_ready else "unavailable",
                "outbox_pending": snap.outbox_pending,
            },
        }


# ── Recording functions ───────────────────────────────────────────────────────

# Additional gauge for tracking max request duration
request_duration_max = Gauge(
    "mneme_http_request_duration_max_ms",
    "Maximum HTTP request duration in milliseconds observed",
    registry=_registry,
)


def track_request(
    *,
    route: str,
    method: str,
    status_code: int,
    duration_ms: float,
) -> None:
    """Record an HTTP request in Prometheus metrics."""
    try:
        labels_method = method.upper()
        request_count.labels(
            method=labels_method,
            route=route,
            status_code=str(status_code),
        ).inc()
        request_duration.labels(
            method=labels_method,
            route=route,
        ).observe(duration_ms / 1000.0)
        if status_code >= 500:
            error_count.labels(
                method=labels_method,
                route=route,
            ).inc()
        # Track max duration
        current_max = _get_gauge_value(request_duration_max)
        if duration_ms > current_max:
            request_duration_max.set(duration_ms)
    except Exception:
        logger.exception("metrics recording failed")


# ── Background probe (runs in metrics middleware) ─────────────────────────────

_last_probe_ts: float = 0.0
_PROBE_INTERVAL_S = 15.0


def _maybe_probe_dependencies() -> None:
    """Periodically refresh DB / Redis / outbox / system gauges."""
    global _last_probe_ts
    now = time.monotonic()
    if now - _last_probe_ts < _PROBE_INTERVAL_S:
        return
    _last_probe_ts = now

    from mneme.observability.health import (
        check_cpu,
        check_database,
        check_db_pool,
        check_memory,
        check_outbox_pending,
        check_process_cpu,
        check_process_memory,
        check_redis,
    )

    # -- Database --
    try:
        db_status = check_database()
        db_ready_gauge.set(1 if db_status == "ok" else 0)
    except Exception:
        db_ready_gauge.set(0)

    # -- Redis --
    try:
        redis_status = check_redis()
        redis_ready_gauge.set(1 if redis_status == "ok" else 0)
    except Exception:
        redis_ready_gauge.set(0)

    # -- Outbox --
    try:
        pending = check_outbox_pending()
        outbox_pending_gauge.set(pending)
    except Exception:
        pass

    # -- System memory --
    try:
        mem = check_memory()
        if mem:
            system_memory_total.set(mem.get("total_bytes", 0))
            system_memory_available.set(mem.get("available_bytes", 0))
            system_memory_used.set(mem.get("used_bytes", 0))
            system_memory_usage_percent.set(mem.get("usage_percent", 0.0))
    except Exception:
        pass

    # -- System CPU --
    try:
        cpu = check_cpu()
        if cpu:
            system_cpu_usage_percent.set(cpu.get("usage_percent", 0.0))
            load_avg = cpu.get("load_avg")
            if load_avg and len(load_avg) >= 3:
                system_cpu_load_1m.set(load_avg[0])
                system_cpu_load_5m.set(load_avg[1])
                system_cpu_load_15m.set(load_avg[2])
    except Exception:
        pass

    # -- Process memory --
    try:
        pmem = check_process_memory()
        if pmem:
            process_memory_rss.set(pmem.get("rss_bytes", 0))
            process_memory_vms.set(pmem.get("vms_bytes", 0))
            process_memory_rss_percent.set(pmem.get("rss_percent", 0.0))
    except Exception:
        pass

    # -- Process CPU --
    try:
        pcpu = check_process_cpu()
        if pcpu:
            process_cpu_percent.set(pcpu.get("cpu_percent", 0.0))
            process_thread_count.set(pcpu.get("thread_count", 0))
            process_open_fds.set(pcpu.get("open_fds", -1))
    except Exception:
        pass

    # -- DB pool --
    try:
        pool = check_db_pool()
        if pool:
            db_pool_size.set(pool.get("pool_size", 0))
            db_pool_checked_in.set(pool.get("checked_in", 0))
            db_pool_checked_out.set(pool.get("checked_out", 0))
            db_pool_overflow.set(pool.get("overflow", 0))
            db_pool_total.set(pool.get("total_connections", 0))
    except Exception:
        pass


# ── Prometheus exposition ─────────────────────────────────────────────────────

def get_prometheus_metrics() -> bytes:
    """Return metrics in Prometheus text exposition format."""
    _maybe_probe_dependencies()
    return generate_latest(_registry)


# ── JSON format (backward compatible) ─────────────────────────────────────────

def get_json_metrics() -> dict[str, Any]:
    """Return a JSON-serialisable metrics snapshot (backward compatible)."""
    _maybe_probe_dependencies()

    # ── Collect counter totals (sum over all label combinations) ─────────
    total_requests = 0
    total_errors = 0
    duration_sum = 0.0
    duration_count = 0

    for metric in request_count.collect():
        for s in metric.samples:
            if s.name.endswith("_total"):
                total_requests += int(s.value)

    for metric in error_count.collect():
        for s in metric.samples:
            if s.name.endswith("_total"):
                total_errors += int(s.value)

    for metric in request_duration.collect():
        for s in metric.samples:
            if s.name.endswith("_sum"):
                duration_sum += s.value
            elif s.name.endswith("_count"):
                duration_count += int(s.value)

    avg_duration_ms = (
        round(duration_sum / duration_count * 1000, 2)
        if duration_count > 0
        else 0.0
    )

    # ── Per-route stats ─────────────────────────────────────────────────
    routes: dict[str, dict[str, Any]] = {}
    # Route-level request counts
    for metric in request_count.collect():
        for s in metric.samples:
            labels = getattr(s, "labels", {})
            m = labels.get("method", "")
            r = labels.get("route", "")
            if m and r:
                key = f"{m} {r}"
                if key not in routes:
                    routes[key] = {"count": 0, "error_count": 0, "duration_ms_avg": 0.0}
                routes[key]["count"] += int(s.value)

    # Route-level error counts
    for metric in error_count.collect():
        for s in metric.samples:
            labels = getattr(s, "labels", {})
            m = labels.get("method", "")
            r = labels.get("route", "")
            if m and r:
                key = f"{m} {r}"
                if key not in routes:
                    routes[key] = {"count": 0, "error_count": 0, "duration_ms_avg": 0.0}
                routes[key]["error_count"] += int(s.value)

    # Route-level duration sums
    route_duration_sums: dict[str, float] = {}
    route_duration_counts: dict[str, int] = {}
    for metric in request_duration.collect():
        for s in metric.samples:
            labels = getattr(s, "labels", {})
            m = labels.get("method", "")
            r = labels.get("route", "")
            if m and r:
                key = f"{m} {r}"
                if s.name.endswith("_sum"):
                    route_duration_sums[key] = route_duration_sums.get(key, 0.0) + s.value
                elif s.name.endswith("_count"):
                    route_duration_counts[key] = route_duration_counts.get(key, 0) + int(s.value)

    for key in routes:
        if key in route_duration_counts and route_duration_counts[key] > 0:
            routes[key]["duration_ms_avg"] = round(
                route_duration_sums.get(key, 0.0) / route_duration_counts[key] * 1000, 2
            )

    db_ready_val = _get_gauge_value(db_ready_gauge)

    return {
        "requests": {
            "total": total_requests,
            "errors": total_errors,
            "duration_ms_avg": avg_duration_ms,
            "duration_ms_max": _get_gauge_value(request_duration_max),
        },
        "routes": routes,
        "dependencies": {
            "database": "ready" if db_ready_val else "unavailable",
            "redis": "ready" if _get_gauge_value(redis_ready_gauge) else "unavailable",
            "outbox_pending": int(_get_gauge_value(outbox_pending_gauge)),
        },
        "system": {
            "memory": {
                "total_bytes": int(_get_gauge_value(system_memory_total)),
                "available_bytes": int(_get_gauge_value(system_memory_available)),
                "used_bytes": int(_get_gauge_value(system_memory_used)),
                "usage_percent": _get_gauge_value(system_memory_usage_percent),
            },
            "cpu": {
                "usage_percent": _get_gauge_value(system_cpu_usage_percent),
                "load_1m": _get_gauge_value(system_cpu_load_1m),
                "load_5m": _get_gauge_value(system_cpu_load_5m),
                "load_15m": _get_gauge_value(system_cpu_load_15m),
            },
        },
        "process": {
            "memory_rss_bytes": int(_get_gauge_value(process_memory_rss)),
            "memory_vms_bytes": int(_get_gauge_value(process_memory_vms)),
            "memory_rss_percent": _get_gauge_value(process_memory_rss_percent),
            "cpu_percent": _get_gauge_value(process_cpu_percent),
            "thread_count": int(_get_gauge_value(process_thread_count)),
            "open_fds": int(_get_gauge_value(process_open_fds)),
        },
        "db_pool": {
            "pool_size": int(_get_gauge_value(db_pool_size)),
            "checked_in": int(_get_gauge_value(db_pool_checked_in)),
            "checked_out": int(_get_gauge_value(db_pool_checked_out)),
            "overflow": int(_get_gauge_value(db_pool_overflow)),
            "total_connections": int(_get_gauge_value(db_pool_total)),
        },
    }


def _get_gauge_value(gauge: Gauge) -> float:
    """Safely extract the current value of a Prometheus Gauge."""
    try:
        # Access the internal value of the Gauge
        return float(gauge._value.get())
    except Exception:
        try:
            for metric in gauge.collect():
                for s in metric.samples:
                    if s.name == gauge._name:
                        return float(s.value)
        except Exception:
            pass
    return 0.0


# ── Backward-compatible wrappers ──────────────────────────────────────────────

get_metrics = get_json_metrics


def get_registry() -> CollectorRegistry:
    """Return the Prometheus metric registry."""
    return _registry


# ── FastAPI helpers ───────────────────────────────────────────────────────────


async def metrics_prometheus_endpoint():
    """Return Prometheus text-format metrics.

    Attach to ``GET /metrics``.
    """
    from fastapi.responses import PlainTextResponse
    _maybe_probe_dependencies()
    return PlainTextResponse(
        content=get_prometheus_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


async def metrics_endpoint() -> dict[str, Any]:
    """Return the current metrics payload as JSON.

    Attach to ``GET /api/v4/metrics``.
    """
    _maybe_probe_dependencies()
    return get_json_metrics()


def install_metrics_endpoint(app) -> None:
    """Register ``GET /metrics`` as Prometheus endpoint on *app*."""

    @app.get("/metrics", tags=["observability"])
    async def _metrics_prometheus():
        return await metrics_prometheus_endpoint()

    # Also keep JSON at a separate path for backward compatibility
    @app.get("/metrics/json", tags=["observability"])
    async def _metrics_json():
        return await metrics_endpoint()


def install_metrics_middleware(app) -> None:
    """Install a pure-ASGI middleware that records request metrics.

    Must be installed *after* the request-context middleware so that
    ``request.state.context`` is available.
    """

    @app.middleware("http")
    async def _metrics_middleware(request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 2)

        route = request.url.path
        method = request.method

        try:
            track_request(
                route=route,
                method=method,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
        except Exception:
            logger.exception("metrics recording failed")

        return response
