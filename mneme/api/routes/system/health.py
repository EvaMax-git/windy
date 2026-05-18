"""Health endpoints: /health/live, /health/startup, /health/ready, /health/extended.

- ``live`` – no dependency checks; always returns "ok".
- ``startup`` – reports whether application startup completed.
- ``ready`` – checks database and Redis; reports "degraded" when Redis is
  unavailable (Phase 1: outbox writes go to Postgres).
- ``extended`` – full system health: DB, Redis, disk, memory, DB pool.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.config import get_settings
from mneme.observability.health import (
    DependencyStatus,
    check_cpu,
    check_database,
    check_db_pool,
    check_disk,
    check_memory,
    check_outbox_pending,
    check_process_cpu,
    check_process_memory,
    check_redis,
    check_vector_service,
    get_vector_service_status,
    get_hostname,
)
from mneme.schemas import (
    CpuInfo,
    DbPoolInfo,
    DiskInfo,
    HealthExtendedData,
    HealthLiveData,
    HealthReadyData,
    HealthStartupData,
    HealthState,
    MemoryInfo,
    ProcessCpuInfo,
    ProcessMemoryInfo,
    ResponseEnvelope,
)

router = APIRouter(prefix="/health", tags=["health"])

# ---------------------------------------------------------------------------
# The lifespan function in main.py sets this flag after successful startup.
# ---------------------------------------------------------------------------
_startup_complete: bool = False


def mark_startup_complete() -> None:
    """Called by the application lifespan after successful initialisation."""
    global _startup_complete
    _startup_complete = True


def is_startup_complete() -> bool:
    """Return ``True`` once :func:`mark_startup_complete` has been called."""
    return _startup_complete


# ---------------------------------------------------------------------------
# /health/live
# ---------------------------------------------------------------------------


@router.get("/live", response_model=ResponseEnvelope[HealthLiveData])
def live(context: RequestContext = Depends(get_request_context)) -> dict:
    """Liveness probe – no dependency checks.

    Returns HTTP 200 as long as the process is alive.
    """
    settings = get_settings()
    return envelope(
        {"status": HealthState.ok, "environment": settings.environment},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ---------------------------------------------------------------------------
# /health/startup
# ---------------------------------------------------------------------------


@router.get("/startup", response_model=ResponseEnvelope[HealthStartupData])
def startup(context: RequestContext = Depends(get_request_context)) -> dict:
    """Startup probe – reports whether application initialisation is complete.

    Returns 503 when startup is still in progress.
    """
    if _startup_complete:
        data = HealthStartupData(status=HealthState.ok, migrations="applied")
        return envelope(
            data.model_dump(),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    data = HealthStartupData(status=HealthState.unavailable, migrations="pending")
    raise ApiError(
        503,
        "dependency_unavailable",
        "应用程序启动尚未完成",
        details=data.model_dump(),
    )


# ---------------------------------------------------------------------------
# /health/ready
# ---------------------------------------------------------------------------


@router.get("/ready", response_model=ResponseEnvelope[HealthReadyData])
def ready(context: RequestContext = Depends(get_request_context)) -> dict:
    """Readiness probe – checks database and Redis.

    - Database unreachable → 503 "unavailable".
    - Redis unreachable → 200 "degraded" (Phase 1: outbox writes use PG).
    """
    db_status = check_database()
    if db_status == DependencyStatus.unavailable:
        raise ApiError(
            503,
            "dependency_unavailable",
            "数据库不可用",
        )

    redis_status = check_redis()
    outbox_pending = check_outbox_pending()

    overall = (
        HealthState.degraded
        if redis_status == DependencyStatus.degraded
        else HealthState.ok
    )

    data = {
        "status": overall,
        "database": db_status,
        "redis": redis_status,
        "outbox_pending": outbox_pending,
    }

    # Add extra context for debugging – keep the response envelope stable.
    return envelope(
        data,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        meta={"outbox_pending": outbox_pending},
    )


# ---------------------------------------------------------------------------
# /health/extended
# ---------------------------------------------------------------------------


@router.get("/extended", response_model=ResponseEnvelope[HealthExtendedData])
def extended(context: RequestContext = Depends(get_request_context)) -> dict:
    """Extended health probe – full system resource diagnostics.

    Checks database, Redis, disk usage, memory, and DB connection pool.
    Returns detailed metrics for monitoring/alerting systems.
    """
    db_status = check_database()
    redis_status = check_redis()
    vector_status = check_vector_service()
    vector_state, vector_reason = get_vector_service_status()

    # Determine overall status
    if db_status == DependencyStatus.unavailable:
        overall = HealthState.unavailable
    elif redis_status == DependencyStatus.degraded or vector_status == DependencyStatus.unavailable:
        overall = HealthState.degraded
    else:
        overall = HealthState.ok

    # Collect extended metrics
    disk_info = [
        DiskInfo.model_validate(d) for d in check_disk()
    ]

    memory_raw = check_memory()
    memory_info = MemoryInfo.model_validate(memory_raw) if memory_raw else None

    cpu_raw = check_cpu()
    cpu_info = CpuInfo.model_validate(cpu_raw) if cpu_raw else None

    process_memory_raw = check_process_memory()
    process_memory_info = ProcessMemoryInfo.model_validate(process_memory_raw) if process_memory_raw else None

    process_cpu_raw = check_process_cpu()
    process_cpu_info = ProcessCpuInfo.model_validate(process_cpu_raw) if process_cpu_raw else None

    db_pool_raw = check_db_pool()
    db_pool_info = DbPoolInfo.model_validate(db_pool_raw) if db_pool_raw else None

    data = HealthExtendedData(
        status=overall,
        database=db_status,
        redis=redis_status,
        vector_service=vector_status,
        vector_service_reason=vector_reason,
        disk=disk_info,
        memory=memory_info,
        cpu=cpu_info,
        process_memory=process_memory_info,
        process_cpu=process_cpu_info,
        db_pool=db_pool_info,
        hostname=get_hostname(),
        python_version=sys.version.split()[0],
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ---------------------------------------------------------------------------
# /health/features
# ---------------------------------------------------------------------------


@router.get("/features", tags=["feature-flags"])
def feature_flags() -> dict:
    """Return currently active feature flags for the frontend.

    The frontend can use this to conditionally enable/disable features
    at runtime without a rebuild.
    """
    settings = get_settings()
    return {
        "legacy_redirects": settings.feature_legacy_redirects,
    }
