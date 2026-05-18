"""Tests for P1-11: Health endpoints, structured logging, minimal metrics.

These are verification tests written by ``test_agent`` to assess whether
the P1-11 deliverables satisfy the Phase 1 completion standards.
"""

from __future__ import annotations

# ═══ env vars MUST be set before any mneme imports ══════════════════════════
import os
# Use setdefault to avoid overwriting DATABASE_URL set by conftest / shell
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import json
import logging
import sys
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mneme.config import get_settings


def _new_test_app():
    """Return a fresh FastAPI app."""
    get_settings.cache_clear()
    from mneme.main import create_app
    return create_app()


# Module-level client – a single shared app instance.
_app = _new_test_app()
client = TestClient(_app)


# Helper: monkeypatch the health functions *as seen by main.py*
# (main.py imports them at module level, so we must patch main.py's namespace)
def _patch_health_check(monkeypatch, target: str, replacement):
    """Monkeypatch a health check function in mneme.main."""
    monkeypatch.setattr(f"mneme.main.{target}", replacement)
    # Also patch the original for unit tests
    monkeypatch.setattr(f"mneme.observability.health.{target}", replacement)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Health Check Helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckDatabase:
    def test_returns_ok_when_db_is_reachable(self):
        from mneme.observability.health import check_database, DependencyStatus
        result = check_database()
        assert result == DependencyStatus.ok, f"got {result!r}"

    def test_returns_unavailable_when_db_fails(self):
        from mneme.observability.health import check_database, DependencyStatus
        import mneme.db.base as db_base
        orig = db_base.check_database_connection
        try:
            db_base.check_database_connection = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            result = check_database()
            assert result == DependencyStatus.unavailable
        finally:
            db_base.check_database_connection = orig


class TestCheckRedis:
    def test_redis_ok_or_degraded_never_unavailable(self):
        """Redis may be available or not; result is NEVER 'unavailable'."""
        from mneme.observability.health import check_redis, DependencyStatus
        result = check_redis()
        # Phase 1: Redis is optional → ok or degraded are both valid
        assert result in (DependencyStatus.ok, DependencyStatus.degraded), (
            f"got {result!r}"
        )
        assert result != DependencyStatus.unavailable, (
            "Phase 1 requires Redis to NEVER be 'unavailable'"
        )

    def test_returns_ok_when_redis_reachable(self, monkeypatch):
        """When Redis is reachable, returns 'ok'."""
        from mneme.observability.health import check_redis, DependencyStatus
        fake = type("FakeRedis", (), {
            "ping": lambda self: True,
            "close": lambda self: None,
        })()
        monkeypatch.setattr("redis.Redis.from_url", lambda *a, **kw: fake)
        result = check_redis()
        assert result == DependencyStatus.ok

    def test_returns_degraded_when_redis_fails(self, monkeypatch):
        """When Redis is unreachable, returns 'degraded'."""
        from mneme.observability.health import check_redis, DependencyStatus
        def _fail(*a, **kw):
            raise ConnectionError("simulated Redis failure")
        monkeypatch.setattr("redis.Redis.from_url", _fail)
        result = check_redis()
        assert result == DependencyStatus.degraded


class TestCheckOutboxPending:
    def test_returns_int(self):
        from mneme.observability.health import check_outbox_pending
        assert isinstance(check_outbox_pending(), int)

    def test_returns_negative_one_when_db_fails(self):
        from mneme.observability.health import check_outbox_pending
        import mneme.db.base as db_base
        orig = db_base.SessionLocal
        try:
            db_base.SessionLocal = None
            assert check_outbox_pending() == -1
        finally:
            db_base.SessionLocal = orig


# ═══════════════════════════════════════════════════════════════════════════
# 2. JsonFormatter
# ═══════════════════════════════════════════════════════════════════════════

class TestJsonFormatter:
    @staticmethod
    def _record(name="test", level=logging.INFO, msg="hello",
                exc_info=None, **extras):
        r = logging.LogRecord(name, level, "", 0, msg, (), exc_info)
        for k, v in extras.items():
            setattr(r, k, v)
        return r

    def test_output_is_valid_json(self):
        from mneme.observability.logging import JsonFormatter
        fmt = JsonFormatter()
        assert isinstance(json.loads(fmt.format(self._record())), dict)

    def test_required_fields_present(self):
        from mneme.observability.logging import JsonFormatter
        fmt = JsonFormatter()
        parsed = json.loads(fmt.format(self._record(level=logging.WARNING)))
        for f in ("timestamp", "level", "logger", "message",
                  "request_id", "correlation_id", "actor_type"):
            assert f in parsed, f"missing {f}"

    def test_timestamp_iso8601_with_tz(self):
        from mneme.observability.logging import JsonFormatter
        parsed = json.loads(JsonFormatter().format(self._record()))
        ts = parsed["timestamp"]
        assert "+" in ts or "Z" in ts, f"no tz in {ts}"

    def test_level_uppercase(self):
        from mneme.observability.logging import JsonFormatter
        fmt = JsonFormatter()
        for lv, nm in [(logging.DEBUG, "DEBUG"), (logging.INFO, "INFO"),
                       (logging.WARNING, "WARNING"), (logging.ERROR, "ERROR")]:
            assert json.loads(fmt.format(self._record(level=lv)))["level"] == nm

    def test_exception_type_message_only_no_traceback(self):
        from mneme.observability.logging import JsonFormatter
        fmt = JsonFormatter()
        try:
            raise ValueError("test error message")
        except ValueError:
            r = self._record(name="err", level=logging.ERROR,
                             msg="boom", exc_info=sys.exc_info())
        output = fmt.format(r)
        output_lower = output.lower()
        assert "traceback" not in output_lower, "traceback leaked into log"
        parsed = json.loads(output)
        assert parsed["exception"]["type"] == "ValueError"
        assert parsed["exception"]["message"] == "test error message"

    def test_no_context_fallback(self, monkeypatch):
        from mneme.observability.logging import JsonFormatter
        import mneme.api.context as ctx
        monkeypatch.setattr(ctx, "peek_request_context", lambda: None)
        fmt = JsonFormatter()
        parsed = json.loads(fmt.format(self._record()))
        assert parsed["request_id"] == "-"
        assert parsed["correlation_id"] == "-"
        assert parsed["actor_type"] == "-"

    def test_access_log_extras(self):
        from mneme.observability.logging import JsonFormatter
        fmt = JsonFormatter()
        r = self._record(
            name="mneme.access", msg="GET /x -> 200 1.23ms",
            route="/health/live", method="GET",
            status_code=200, duration_ms=1.23,
        )
        parsed = json.loads(fmt.format(r))
        assert parsed["route"] == "/health/live"
        assert parsed["method"] == "GET"
        assert parsed["status_code"] == 200
        assert parsed["duration_ms"] == 1.23

    def test_no_password_in_output(self):
        from mneme.observability.logging import JsonFormatter
        fmt = JsonFormatter()
        r = self._record(name="mneme.auth", msg="login attempt for user test")
        assert "password" not in fmt.format(r).lower()


# ═══════════════════════════════════════════════════════════════════════════
# 3. MetricsRegistry
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsRegistry:
    def test_initial_zero(self):
        from mneme.observability.metrics import MetricsRegistry
        s = MetricsRegistry().snapshot()
        assert s.request_count == s.error_count == 0
        assert s.request_duration_ms_total == 0.0
        assert s.request_duration_ms_max == 0.0

    def test_record_request_counts(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.record_request(route="/a", method="GET", status_code=200, duration_ms=5.0)
        reg.record_request(route="/a", method="GET", status_code=200, duration_ms=3.0)
        s = reg.snapshot()
        assert s.request_count == 2
        assert s.request_duration_ms_total == 8.0
        assert s.request_duration_ms_max == 5.0

    def test_error_tracking(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.record_request(route="/t", method="POST", status_code=200, duration_ms=2)
        reg.record_request(route="/t", method="POST", status_code=500, duration_ms=10)
        reg.record_request(route="/t", method="POST", status_code=502, duration_ms=7)
        s = reg.snapshot()
        assert s.request_count == 3
        assert s.error_count == 2

    def test_per_route_metrics(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.record_request(route="/a", method="GET", status_code=200, duration_ms=1)
        reg.record_request(route="/a", method="GET", status_code=500, duration_ms=2)
        reg.record_request(route="/b", method="POST", status_code=200, duration_ms=3)
        s = reg.snapshot()
        assert s.routes["GET /a"].count == 2
        assert s.routes["GET /a"].error_count == 1
        assert s.routes["POST /b"].count == 1
        assert s.routes["POST /b"].error_count == 0

    def test_set_db_ready(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.set_db_ready(True)
        assert reg.snapshot().db_ready == 1
        reg.set_db_ready(False)
        assert reg.snapshot().db_ready == 0

    def test_set_redis_ready(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.set_redis_ready(True)
        assert reg.snapshot().redis_ready == 1
        reg.set_redis_ready(False)
        assert reg.snapshot().redis_ready == 0

    def test_set_outbox_pending(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.set_outbox_pending(42)
        assert reg.snapshot().outbox_pending == 42
        reg.set_outbox_pending(-1)
        assert reg.snapshot().outbox_pending == -1

    def test_as_dict_structure(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.record_request(route="/x", method="GET", status_code=200, duration_ms=10)
        d = reg.as_dict()
        for section in ("requests", "routes", "dependencies"):
            assert section in d
        for k in ("total", "errors", "duration_ms_avg", "duration_ms_max"):
            assert k in d["requests"]
        for k in ("database", "redis", "outbox_pending"):
            assert k in d["dependencies"]

    def test_duration_avg_zero_without_requests(self):
        from mneme.observability.metrics import MetricsRegistry
        assert MetricsRegistry().as_dict()["requests"]["duration_ms_avg"] == 0.0

    def test_snapshot_independent_copy(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        reg.record_request(route="/x", method="GET", status_code=200, duration_ms=5)
        snap = reg.snapshot()
        snap.request_count = 999
        assert reg.snapshot().request_count == 1

    def test_thread_safety(self):
        from mneme.observability.metrics import MetricsRegistry
        reg = MetricsRegistry()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    reg.record_request(route="/t", method="GET",
                                       status_code=200, duration_ms=1)
                    reg.set_db_ready(True)
                    reg.set_outbox_pending(5)
                    reg.snapshot()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        assert reg.snapshot().request_count == 1000


# ═══════════════════════════════════════════════════════════════════════════
# 4. Health Endpoints (HTTP)
# ═══════════════════════════════════════════════════════════════════════════

class TestHealthLiveEndpoint:
    def test_standalone_live_200(self):
        r = client.get("/health/live")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_api_v4_live_200_envelope(self):
        r = client.get("/api/v4/health/live")
        assert r.status_code == 200
        data = r.json()
        assert "request_id" in data
        assert "correlation_id" in data
        assert data["data"]["status"] == "ok"

    def test_live_no_db_dependency(self):
        r = client.get("/health/live")
        assert r.status_code == 200


class TestHealthStartupEndpoint:
    def test_mark_and_check_helpers(self):
        from mneme.api.routes.system.health import is_startup_complete, mark_startup_complete
        import mneme.api.routes.system.health as hmod
        orig = hmod._startup_complete
        try:
            hmod._startup_complete = False
            assert not is_startup_complete()
            mark_startup_complete()
            assert is_startup_complete()
        finally:
            hmod._startup_complete = orig

    def test_startup_503_when_false(self):
        import mneme.api.routes.system.health as hmod
        orig = hmod._startup_complete
        try:
            hmod._startup_complete = False
            r = client.get("/health/startup")
            assert r.status_code == 503
            body = r.json()
            # Standalone health endpoints now use the unified error_envelope format
            assert body["error"]["code"] == "dependency_unavailable"
            assert body["error"]["details"]["migrations"] == "pending"
        finally:
            hmod._startup_complete = orig

    def test_api_v4_startup_503_when_false(self):
        import mneme.api.routes.system.health as hmod
        orig = hmod._startup_complete
        try:
            hmod._startup_complete = False
            r = client.get("/api/v4/health/startup")
            assert r.status_code == 503
            assert r.json()["error"]["code"] == "dependency_unavailable"
        finally:
            hmod._startup_complete = orig

    def test_startup_200_when_true(self):
        import mneme.api.routes.system.health as hmod
        orig = hmod._startup_complete
        try:
            hmod._startup_complete = True
            r = client.get("/health/startup")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"
        finally:
            hmod._startup_complete = orig

    def test_api_v4_startup_200_when_true(self):
        import mneme.api.routes.system.health as hmod
        orig = hmod._startup_complete
        try:
            hmod._startup_complete = True
            r = client.get("/api/v4/health/startup")
            assert r.status_code == 200
            assert r.json()["data"]["status"] == "ok"
        finally:
            hmod._startup_complete = orig


class TestHealthReadyEndpoint:
    def test_standalone_ready_200(self, monkeypatch):
        # Ensure DB check returns ok (the default)
        import mneme.main as main_mod
        monkeypatch.setattr(main_mod, "check_database",
                            lambda: "ok")
        monkeypatch.setattr(main_mod, "check_redis",
                            lambda: "degraded")
        monkeypatch.setattr(main_mod, "check_outbox_pending",
                            lambda: 0)
        r = client.get("/health/ready")
        assert r.status_code == 200
        assert r.json()["status"] in ("ok", "degraded")

    def test_api_v4_ready_200(self):
        r = client.get("/api/v4/health/ready")
        assert r.status_code == 200
        assert "data" in r.json()

    def test_ready_field_completeness(self):
        data = client.get("/health/ready").json()
        for f in ("status", "database", "redis", "outbox_pending"):
            assert f in data, f"missing {f}"

    def test_api_v4_ready_field_completeness(self):
        payload = client.get("/api/v4/health/ready").json()
        data = payload["data"]
        for f in ("status", "database", "redis", "outbox_pending"):
            assert f in data, f"missing {f}"

    def test_db_unavailable_returns_503(self, monkeypatch):
        """Simulate DB failure → /health/ready returns 503."""
        import mneme.main as main_mod
        monkeypatch.setattr(main_mod, "check_database",
                            lambda: "unavailable")
        # Also need to patch DependencyStatus
        monkeypatch.setattr(main_mod, "DependencyStatus",
                            type("D", (), {"unavailable": "unavailable", "degraded": "degraded", "ok": "ok"}))
        r = client.get("/health/ready")
        assert r.status_code == 503
        body = r.json()
        # Standalone health endpoints now use the unified error_envelope format
        assert body["error"]["code"] == "dependency_unavailable"
        assert body["error"]["details"]["database"] == "unavailable"

    def test_redis_degraded_still_200(self, monkeypatch):
        """Phase 1 strategy: Redis missing → 200 degraded, not 503."""
        import mneme.main as main_mod
        monkeypatch.setattr(main_mod, "check_database", lambda: "ok")
        monkeypatch.setattr(main_mod, "check_redis", lambda: "degraded")
        monkeypatch.setattr(main_mod, "check_outbox_pending", lambda: 0)
        monkeypatch.setattr(main_mod, "DependencyStatus",
                            type("D", (), {"ok": "ok", "degraded": "degraded", "unavailable": "unavailable"}))
        r = client.get("/health/ready")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "degraded"
        assert data["redis"] == "degraded"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Metrics Endpoints
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsEndpoints:
    def test_standalone_metrics_prometheus_200(self):
        r = client.get("/metrics")
        assert r.status_code == 200
        # Prometheus format is plain text, not JSON
        assert "text/plain" in r.headers["content-type"]
        body = r.text
        # Check for some expected Prometheus metric names
        assert "mneme_http_requests_total" in body or "mneme_db_ready" in body

    def test_standalone_metrics_json_200(self):
        r = client.get("/metrics/json")
        assert r.status_code == 200
        data = r.json()
        for section in ("requests", "routes", "dependencies"):
            assert section in data

    def test_api_v4_metrics_200(self):
        r = client.get("/api/v4/metrics")
        assert r.status_code == 200
        assert "requests" in r.json()

    def test_metrics_json_required_fields(self):
        data = client.get("/metrics/json").json()
        for f in ("total", "errors", "duration_ms_avg", "duration_ms_max"):
            assert f in data["requests"]
        for f in ("database", "redis", "outbox_pending"):
            assert f in data["dependencies"]

    def test_metrics_system_fields(self):
        """Verify JSON metrics include system/process sections."""
        data = client.get("/metrics/json").json()
        for section in ("system", "process", "db_pool"):
            assert section in data, f"missing section: {section}"

    def test_request_count_increments(self):
        client.get("/health/live")
        client.get("/health/startup")
        data = client.get("/metrics/json").json()
        assert data["requests"]["total"] > 0


# ═══════════════════════════════════════════════════════════════════════════
# 6. Logging Security
# ═══════════════════════════════════════════════════════════════════════════

class TestLoggingSecurity:
    def test_login_response_does_not_contain_password(self):
        r = client.post(
            "/api/v4/auth/login",
            json={"username": "nx", "password": "secret123"},
        )
        assert "secret123" not in r.text


# ═══════════════════════════════════════════════════════════════════════════
# 7. Configure Logging
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigureLogging:
    def test_sets_root_level(self):
        from mneme.observability.logging import configure_logging
        configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG
        configure_logging("INFO")

    def test_installs_json_handler(self):
        from mneme.observability.logging import configure_logging, JsonFormatter
        configure_logging("INFO")
        assert any(isinstance(h.formatter, JsonFormatter)
                   for h in logging.getLogger().handlers)

    def test_idempotent(self):
        from mneme.observability.logging import configure_logging
        configure_logging("INFO")
        before = len(logging.getLogger().handlers)
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == before

    def test_noisy_loggers_warning(self):
        from mneme.observability.logging import configure_logging
        configure_logging("INFO")
        for name in ("uvicorn", "uvicorn.access", "sqlalchemy.engine"):
            assert logging.getLogger(name).level == logging.WARNING


# ═══════════════════════════════════════════════════════════════════════════
# 8. DependencyStatus
# ═══════════════════════════════════════════════════════════════════════════

class TestDependencyStatus:
    def test_three_states(self):
        from mneme.observability.health import DependencyStatus
        assert DependencyStatus.ok == "ok"
        assert DependencyStatus.degraded == "degraded"
        assert DependencyStatus.unavailable == "unavailable"


# ═══════════════════════════════════════════════════════════════════════════
# 9. Middleware Installation
# ═══════════════════════════════════════════════════════════════════════════

class TestMiddlewareInstallation:
    def test_request_id_header_present(self):
        r = client.get("/health/live")
        assert "X-Request-Id" in r.headers
        assert "X-Correlation-Id" in r.headers

    def test_request_id_header_on_api_v4(self):
        r = client.get("/api/v4/health/live")
        assert "X-Request-Id" in r.headers

    def test_custom_request_id_echoed_back(self):
        rid = str(uuid4())
        r = client.get("/health/live", headers={"X-Request-Id": rid})
        assert r.headers["X-Request-Id"] == rid


# ═══════════════════════════════════════════════════════════════════════════
# 10. AccessLogMiddleware
# ═══════════════════════════════════════════════════════════════════════════

class TestAccessLogMiddleware:
    def test_access_log_written(self, caplog):
        caplog.set_level(logging.INFO, logger="mneme.access")
        client.get("/health/live")
        records = [r for r in caplog.records if r.name == "mneme.access"]
        assert len(records) >= 1

    def test_access_log_has_extras(self, caplog):
        caplog.set_level(logging.INFO, logger="mneme.access")
        client.get("/health/live")
        records = [r for r in caplog.records if r.name == "mneme.access"]
        assert len(records) >= 1
        r = records[0]
        for attr in ("route", "method", "status_code", "duration_ms"):
            assert hasattr(r, attr)

    def test_access_log_route_matches_path(self, caplog):
        caplog.set_level(logging.INFO, logger="mneme.access")
        import mneme.api.routes.system.health as hmod
        orig = hmod._startup_complete
        try:
            hmod._startup_complete = True
            client.get("/api/v4/health/startup")
        finally:
            hmod._startup_complete = orig
        records = [r for r in caplog.records if r.name == "mneme.access"]
        assert any(getattr(r, "route", "") == "/api/v4/health/startup"
                   for r in records)

    def test_method_is_correct(self, caplog):
        caplog.set_level(logging.INFO, logger="mneme.access")
        try:
            client.post("/api/v4/auth/login",
                        json={"username": "nx", "password": "np"})
        except Exception:
            pass
        records = [r for r in caplog.records if r.name == "mneme.access"]
        assert any(getattr(r, "method", "") == "POST" for r in records)

    def test_5xx_status_code_logged(self, caplog, monkeypatch):
        """Simulate DB error → 503 is captured in access log."""
        caplog.set_level(logging.INFO, logger="mneme.access")
        import mneme.main as main_mod
        monkeypatch.setattr(main_mod, "check_database",
                            lambda: "unavailable")
        monkeypatch.setattr(main_mod, "DependencyStatus",
                            type("D", (), {"unavailable": "unavailable", "degraded": "degraded", "ok": "ok"}))
        client.get("/health/ready")
        records = [r for r in caplog.records if r.name == "mneme.access"]
        assert any(getattr(r, "status_code", 0) == 503 for r in records), (
            f"No 503 status in access log. Records: {[(getattr(r,'status_code',None), getattr(r,'route',None)) for r in records]}"
        )

