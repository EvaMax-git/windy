from __future__ import annotations

from enum import Enum
from typing import Any

from mneme.schemas.common import ApiSchema


class HealthState(str, Enum):
    ok = "ok"
    degraded = "degraded"
    unavailable = "unavailable"


class HealthLiveData(ApiSchema):
    status: HealthState
    environment: str


class HealthReadyData(ApiSchema):
    status: HealthState
    database: str
    redis: str = "unknown"
    outbox_pending: int = -1


class HealthStartupData(ApiSchema):
    status: HealthState
    migrations: str | None = None


# ── Extended Health ────────────────────────────────────────────────────────────

class DiskInfo(ApiSchema):
    """Per-partition disk usage information."""
    mountpoint: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float


class MemoryInfo(ApiSchema):
    """System memory usage information (bytes)."""
    total_bytes: int
    available_bytes: int
    used_bytes: int
    usage_percent: float


class CpuInfo(ApiSchema):
    """System CPU usage information."""
    usage_percent: float
    per_cpu_percent: list[float] = []
    load_avg: list[float] | None = None
    core_count_logical: int | None = None
    core_count_physical: int | None = None


class ProcessMemoryInfo(ApiSchema):
    """Current process memory usage."""
    rss_bytes: int
    vms_bytes: int
    rss_percent: float = 0.0


class ProcessCpuInfo(ApiSchema):
    """Current process CPU and resource usage."""
    cpu_percent: float
    thread_count: int
    open_fds: int | None = None


class DbPoolInfo(ApiSchema):
    """Database connection pool statistics."""
    pool_size: int
    checked_in: int
    checked_out: int
    overflow: int
    total_connections: int


class HealthExtendedData(ApiSchema):
    """Extended health report including system resource metrics."""
    status: HealthState
    database: str
    redis: str = "unknown"
    vector_service: str = "unknown"
    vector_service_reason: str | None = None
    disk: list[DiskInfo] = []
    memory: MemoryInfo | None = None
    cpu: CpuInfo | None = None
    process_memory: ProcessMemoryInfo | None = None
    process_cpu: ProcessCpuInfo | None = None
    db_pool: DbPoolInfo | None = None
    hostname: str = ""
    python_version: str = ""

