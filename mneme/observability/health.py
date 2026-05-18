"""Health-check helpers for database, Redis, outbox, and system resources.

Exposes lightweight, import-safe check functions used by the
``/health/live``, ``/health/startup``, ``/health/ready``, and
``/health/extended`` endpoints.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class DependencyStatus:
    """Canonical health states for a single dependency."""

    ok: str = "ok"
    degraded: str = "degraded"
    unavailable: str = "unavailable"


@dataclass
class HealthReport:
    status: str = DependencyStatus.ok
    checks: dict[str, str] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


def check_database() -> str:
    """Return ``"ok"``, ``"degraded"``, or ``"unavailable"`` for Postgres."""
    try:
        from mneme.db.base import check_database_connection

        check_database_connection()
        return DependencyStatus.ok
    except Exception:
        logger.exception("database health check failed")
        return DependencyStatus.unavailable


def check_redis() -> str:
    """Return ``"ok"``, ``"degraded"``, or ``"unavailable"`` for Redis.

    In Phase 1 Redis is not required for core write-path correctness
    (outbox writes go to Postgres), so a missing Redis is reported as
    ``"degraded"`` rather than ``"unavailable"``.
    """
    try:
        import redis

        from mneme.config import get_settings

        settings = get_settings()
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        client.ping()
        client.close()
        return DependencyStatus.ok
    except Exception:
        logger.warning("redis health check failed", exc_info=True)
        return DependencyStatus.degraded


def check_outbox_pending() -> int:
    """Return the number of pending outbox events (``events`` table).

    Returns -1 when the database is unreachable so callers can distinguish
    "zero pending" from "unknown".
    """
    try:
        from mneme.db.base import SessionLocal

        db = SessionLocal()
        try:
            from sqlalchemy import text

            result = db.execute(
                text(
                    "SELECT COUNT(*) FROM events WHERE publish_state = 'pending'"
                )
            ).scalar()
            return int(result or 0)
        finally:
            db.close()
    except Exception:
        logger.exception("outbox pending check failed")
        return -1


# ── Extended health checks ──────────────────────────────────────────────────────


def check_disk() -> list[dict[str, Any]]:
    """Return disk usage information for all mounted partitions.

    Returns a list of dicts with keys: mountpoint, total_bytes, used_bytes,
    free_bytes, usage_percent.
    """
    partitions: list[dict[str, Any]] = []
    try:
        for part in shutil.disk_partitions():
            try:
                usage = shutil.disk_usage(part.mountpoint)
                partitions.append({
                    "mountpoint": part.mountpoint,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "usage_percent": round(usage.used / usage.total * 100, 2),
                })
            except (PermissionError, OSError):
                # Skip partitions we can't read
                continue
    except Exception:
        logger.exception("disk health check failed")

    return partitions


def check_memory() -> dict[str, Any] | None:
    """Return system memory information.

    Returns a dict with keys: total_bytes, available_bytes, used_bytes,
    usage_percent. Returns None when the information is unavailable.
    """
    try:
        import psutil
        mem = psutil.virtual_memory()
        return {
            "total_bytes": mem.total,
            "available_bytes": mem.available,
            "used_bytes": mem.used,
            "usage_percent": round(mem.percent, 2),
        }
    except ImportError:
        # Fallback: try /proc/meminfo on Linux
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            meminfo: dict[str, int] = {}
            for line in lines:
                parts = line.split(":")
                if len(parts) >= 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    try:
                        meminfo[key] = int(val) * 1024  # kB → bytes
                    except (ValueError, IndexError):
                        pass

            total = meminfo.get("MemTotal", 0)
            available = meminfo.get("MemAvailable", 0)
            used = total - available if total and available else 0
            return {
                "total_bytes": total,
                "available_bytes": available,
                "used_bytes": used,
                "usage_percent": round(used / total * 100, 2) if total else 0.0,
            }
        except Exception:
            logger.exception("memory health check (proc fallback) failed")
            return None
    except Exception:
        logger.exception("memory health check failed")
        return None


def check_cpu() -> dict[str, Any] | None:
    """Return system CPU usage information.

    Returns a dict with keys:
      - usage_percent: overall CPU utilization (0-100)
      - per_cpu_percent: list of per-core utilization percentages
      - load_avg: 1/5/15 min load average (None on non-Linux)
      - core_count: physical + logical core count

    Returns None when the information is unavailable.
    """
    try:
        import psutil

        usage = psutil.cpu_percent(interval=0.1)
        per_cpu = psutil.cpu_percent(interval=0.0, percpu=True)
        core_count_logical = psutil.cpu_count(logical=True)
        core_count_physical = psutil.cpu_count(logical=False)

        load_avg: list[float] | None = None
        try:
            lavg = psutil.getloadavg()
            load_avg = [round(v, 2) for v in lavg]
        except (AttributeError, OSError):
            pass  # Not available on all platforms

        return {
            "usage_percent": round(usage, 2),
            "per_cpu_percent": [round(p, 2) for p in per_cpu] if per_cpu else [],
            "load_avg": load_avg,
            "core_count_logical": core_count_logical,
            "core_count_physical": core_count_physical,
        }
    except ImportError:
        # Fallback: /proc/stat on Linux
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
            parts = line.split()
            if parts[0] == "cpu":
                # cpu user nice system idle iowait irq softirq steal ...
                total = sum(int(x) for x in parts[1:])
                idle = int(parts[4])
                # We need two samples for real usage — single sample is cumulative
                # so we read then wait then read again
                import time
                time.sleep(0.1)
                with open("/proc/stat", "r") as f:
                    line2 = f.readline()
                parts2 = line2.split()
                total2 = sum(int(x) for x in parts2[1:])
                idle2 = int(parts2[4])
                total_delta = total2 - total
                idle_delta = idle2 - idle
                usage_pct = round((total_delta - idle_delta) / total_delta * 100, 2) if total_delta > 0 else 0.0

                # Try get loadavg
                load_avg = None
                try:
                    with open("/proc/loadavg", "r") as f:
                        la = f.readline().split()
                        load_avg = [round(float(x), 2) for x in la[:3]]
                except Exception:
                    pass

                import os as _os
                return {
                    "usage_percent": usage_pct,
                    "per_cpu_percent": [],
                    "load_avg": load_avg,
                    "core_count_logical": _os.cpu_count() or 0,
                    "core_count_physical": None,
                }
        except Exception:
            logger.exception("cpu health check (proc fallback) failed")
            return None
    except Exception:
        logger.exception("cpu health check failed")
        return None


def check_process_memory() -> dict[str, Any] | None:
    """Return the current process memory usage (RSS, VMS).

    Returns a dict with keys:
      - rss_bytes: resident set size (actual physical memory)
      - vms_bytes: virtual memory size
      - rss_percent: RSS as percentage of total system memory

    Returns None when unavailable.
    """
    try:
        import psutil
        proc = psutil.Process()
        mem_info = proc.memory_info()
        rss = mem_info.rss
        vms = mem_info.vms

        rss_pct = 0.0
        try:
            total = psutil.virtual_memory().total
            if total > 0:
                rss_pct = round(rss / total * 100, 2)
        except Exception:
            pass

        return {
            "rss_bytes": rss,
            "vms_bytes": vms,
            "rss_percent": rss_pct,
        }
    except ImportError:
        # Fallback: /proc/self/status on Linux
        try:
            with open("/proc/self/status", "r") as f:
                status = f.read()
            rss = 0
            vms = 0
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024  # kB → bytes
                elif line.startswith("VmSize:"):
                    vms = int(line.split()[1]) * 1024
            return {
                "rss_bytes": rss,
                "vms_bytes": vms,
                "rss_percent": 0.0,
            }
        except Exception:
            logger.exception("process memory health check failed")
            return None
    except Exception:
        logger.exception("process memory health check failed")
        return None


def check_process_cpu() -> dict[str, Any] | None:
    """Return the current process CPU usage.

    Returns a dict with keys:
      - cpu_percent: process CPU utilization (0-100)
      - thread_count: number of threads
      - open_fds: number of open file descriptors (None on non-Linux)

    Returns None when unavailable.
    """
    try:
        import psutil
        proc = psutil.Process()
        cpu_pct = proc.cpu_percent(interval=0.1)
        threads = proc.num_threads()

        open_fds = None
        try:
            open_fds = proc.num_fds()
        except (AttributeError, Exception):
            pass

        return {
            "cpu_percent": round(cpu_pct, 2),
            "thread_count": threads,
            "open_fds": open_fds,
        }
    except ImportError:
        return None
    except Exception:
        logger.exception("process cpu health check failed")
        return None


def check_db_pool() -> dict[str, Any] | None:
    """Return database connection pool statistics from SQLAlchemy engine.

    Returns a dict with keys: pool_size, checked_in, checked_out, overflow,
    total_connections. Returns None when unavailable.
    """
    try:
        from mneme.db.base import engine

        pool = engine.pool

        checked_in = getattr(pool, "checkedin_connections", None)
        checked_in = checked_in() if callable(checked_in) else -1

        size_fn = getattr(pool, "size", None)
        pool_size = size_fn() if callable(size_fn) else -1

        overflow_fn = getattr(pool, "overflow", None)
        overflow = overflow_fn() if callable(overflow_fn) else -1

        checked_out = getattr(pool, "_checked_out", -1)

        return {
            "pool_size": pool_size,
            "checked_in": checked_in,
            "checked_out": checked_out,
            "overflow": overflow,
            "total_connections": checked_in + max(checked_out, 0),
        }
    except Exception:
        logger.exception("db pool health check failed")
        return None


def get_hostname() -> str:
    """Return the hostname of the machine."""
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


# ── Vector / Embedding service health check ──────────────────────────────────────

_vector_service_state: str = "unknown"
_vector_service_reason: str | None = None


def check_vector_service() -> str:
    """Check if the embedding Gateway service is responsive.

    Performs a lightweight probe by attempting to resolve a capability binding
    for ``embedding.create``.  Returns ``"ok"``, ``"degraded"``, or
    ``"unavailable"`` and caches the result for the current process lifetime.

    The cached state can be read by search functions to decide whether to
    attempt vector-based ranking or degrade to FTS-only mode.
    """
    global _vector_service_state, _vector_service_reason
    try:
        from mneme.db.gateway import resolve_capability_binding

        # Resolve a capability binding to check Gateway availability
        binding = resolve_capability_binding(capability_code="embedding.create")
        if binding is None:
            _vector_service_state = "unavailable"
            _vector_service_reason = "no embedding capability binding configured"
            return _vector_service_state

        # Lightweight probe — just verify the binding resolves
        _vector_service_state = "ok"
        _vector_service_reason = None
        return _vector_service_state
    except Exception as exc:
        logger.warning("vector service health check failed: %s", exc)
        _vector_service_state = "unavailable"
        _vector_service_reason = str(exc)
        return _vector_service_state


def get_vector_service_status() -> tuple[str, str | None]:
    """Return the cached vector service (state, reason) tuple.

    Returns ``("unknown", None)`` if :func:`check_vector_service` has never
    been called.
    """
    return _vector_service_state, _vector_service_reason


def mark_vector_service_degraded(reason: str | None = None) -> None:
    """Explicitly mark the vector service as degraded from outside.

    Call this from search functions that encounter a Gateway error so that
    subsequent requests can skip vector ranking immediately.
    """
    global _vector_service_state, _vector_service_reason
    _vector_service_state = "degraded"
    _vector_service_reason = reason or "vector service unreachable"


def reset_vector_service_state() -> None:
    """Reset the cached vector service state so it will be re-probed."""
    global _vector_service_state, _vector_service_reason
    _vector_service_state = "unknown"
    _vector_service_reason = None
