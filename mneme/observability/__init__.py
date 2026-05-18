"""Mneme observability: structured JSON logging, health checks, and minimal metrics."""

from mneme.observability.health import (
    DependencyStatus,
    HealthReport,
    check_database,
    check_outbox_pending,
    check_redis,
)
from mneme.observability.logging import (
    AccessLogMiddleware,
    JsonFormatter,
    configure_logging,
)
from mneme.observability.metrics import (
    MetricsRegistry,
    get_metrics,
    get_registry,
    install_metrics_endpoint,
    install_metrics_middleware,
    track_request,
)

__all__ = [
    "AccessLogMiddleware",
    "DependencyStatus",
    "HealthReport",
    "JsonFormatter",
    "MetricsRegistry",
    "check_database",
    "check_outbox_pending",
    "check_redis",
    "configure_logging",
    "get_metrics",
    "get_registry",
    "install_metrics_endpoint",
    "install_metrics_middleware",
    "track_request",
]
