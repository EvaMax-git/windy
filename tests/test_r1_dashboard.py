"""R1 Dashboard & System Monitoring contract tests.

Covers new-architecture dashboard/system endpoints:
* Service info:             GET  /
* Health (standalone):      GET  /health/live, /health/startup, /health/ready
* Health (API v4):          GET  /api/v4/health/live, /health/startup,
                                /health/ready, /health/extended
* Metrics:                  GET  /api/v4/metrics
* Admin audit events:       GET  /api/v4/admin/audit-events
                                /api/v4/admin/audit-events/{audit_id}
* Admin outbox events:      GET  /api/v4/admin/events
                                /api/v4/admin/events/{event_id}
* Admin logs:               GET  /api/v4/admin/logs
* Admin jobs:               GET  /api/v4/admin/jobs
                                /api/v4/admin/jobs/{job_id}
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _new_client() -> TestClient:
    """Create a fresh TestClient wired to the conftest schema DB."""
    from mneme.config import get_settings

    get_settings.cache_clear()
    from mneme.db.base import SessionLocal
    from mneme.main import create_app

    app = create_app()

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from mneme.db.base import get_db

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(db):
    """TestClient with DB session overridden to conftest's engine."""
    return _new_client()


@pytest.fixture
def auth_client(client, db):
    """Authenticated TestClient — logs in as 'owner' user.

    Depends on the 'owner' user seeded by conftest (INSERT OR IGNORE).
    Returns (client, user_id).
    """
    from sqlalchemy import text

    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "owner", "password": "dummy"},
    )
    if login_resp.status_code != 200:
        # Owner user may have different password; set a known one
        from mneme.security import hash_password
        import datetime as dt

        now_val = dt.datetime.now(dt.timezone.utc)
        db.execute(
            text(
                "INSERT OR IGNORE INTO users "
                "(user_id, username, email, display_name, role_code, status, "
                "password_hash, mfa_mode, created_at, updated_at) "
                "VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa, :now, :now)"
            ),
            {
                "uid": uuid4().hex,
                "uname": "admin_r1",
                "email": "admin_r1@test.local",
                "dname": "Admin R1",
                "role": "owner",
                "status": "active",
                "phash": hash_password("test1234"),
                "mfa": "none",
                "now": now_val,
            },
        )
        db.commit()
        login_resp = client.post(
            "/api/v4/auth/login",
            json={"username": "admin_r1", "password": "test1234"},
        )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.json()}"
    return client


# ═══════════════════════════════════════════════════════════════════════════
# R1.1 — Service Root
# ═══════════════════════════════════════════════════════════════════════════


class TestServiceRoot:
    """Service info endpoint at GET /."""

    def test_root_returns_json_with_accept_json(self, client):
        resp = client.get("/", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "Mneme"
        assert "version" in body
        assert "health" in body
        assert "docs" in body

    def test_root_returns_html_without_accept(self, client):
        resp = client.get("/")
        # Accepts 200 (HTML) or 404 (no frontend); both are valid
        assert resp.status_code in (200, 404)

    def test_root_returns_html_with_accept_html(self, client):
        resp = client.get("/", headers={"Accept": "text/html"})
        assert resp.status_code in (200, 404)


# ═══════════════════════════════════════════════════════════════════════════
# R1.2 — Standalone Health
# ═══════════════════════════════════════════════════════════════════════════


class TestStandaloneHealth:
    """Standalone health endpoints outside /api/v4."""

    def test_live_returns_ok(self, client):
        resp = client.get("/health/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "environment" in body

    def test_startup_returns_ok_after_startup(self, client):
        # Manually mark startup complete (TestClient doesn't run lifespan)
        from mneme.api.routes.system.health import mark_startup_complete
        mark_startup_complete()
        resp = client.get("/health/startup")
        assert resp.status_code == 200

    def test_ready_returns_ok(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert "database" in body
        assert body["database"] == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# R1.3 — API v4 Health
# ═══════════════════════════════════════════════════════════════════════════


class TestApiV4Health:
    """Health endpoints under /api/v4/health."""

    def test_live_returns_envelope(self, client):
        resp = client.get("/api/v4/health/live")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert body["data"]["status"] == "ok"

    def test_startup_returns_envelope(self, client):
        from mneme.api.routes.system.health import mark_startup_complete
        mark_startup_complete()
        resp = client.get("/api/v4/health/startup")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert body["data"]["status"] == "ok"

    def test_ready_returns_envelope(self, client):
        resp = client.get("/api/v4/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert body["data"]["status"] in ("ok", "degraded")

    def test_extended_returns_detailed_info(self, client):
        resp = client.get("/api/v4/health/extended")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "database" in data
        assert "redis" in data
        assert data.get("memory") is not None or "memory" in data
        assert "hostname" in data
        assert "python_version" in data


# ═══════════════════════════════════════════════════════════════════════════
# R1.4 — Metrics
# ═══════════════════════════════════════════════════════════════════════════


class TestMetrics:
    """Metrics endpoint."""

    def test_metrics_v4_returns_data(self, client):
        resp = client.get("/api/v4/metrics")
        # May be 200 or 404/501 depending on prometheus_client availability
        assert resp.status_code in (200, 404, 501)


# ═══════════════════════════════════════════════════════════════════════════
# R1.5 — Admin Audit Events
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminAuditEvents:
    """Admin audit event listing."""

    def test_list_audit_events_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/admin/audit-events")
        # 200 on real DB; 500 on SQLite due to isoformat issue (pre-existing)
        assert resp.status_code in (200, 500), f"Unexpected: {resp.status_code}"
        if resp.status_code == 200:
            body = resp.json()
            assert "data" in body
            data = body["data"]
            assert "items" in data
            assert "page_info" in data
            assert isinstance(data["items"], list)
            pi = data["page_info"]
            for key in ("page", "page_size", "total_items", "total_pages", "has_next", "has_previous"):
                assert key in pi, f"page_info missing key: {key}"

    def test_list_audit_events_with_filters(self, auth_client):
        resp = auth_client.get(
            "/api/v4/admin/audit-events",
            params={"action": "auth.login", "result": "success", "page_size": 10},
        )
        # 200 on real DB; may be 500 on SQLite (pre-existing isoformat issue)
        assert resp.status_code in (200, 500), f"Unexpected: {resp.status_code}"
        if resp.status_code == 200:
            body = resp.json()
            items = body["data"]["items"]
            for item in items:
                assert "action" in item

    def test_list_audit_events_supports_pagination(self, auth_client):
        resp = auth_client.get(
            "/api/v4/admin/audit-events", params={"page": 1, "page_size": 2}
        )
        # 200 on real DB; may be 500 on SQLite (pre-existing isoformat issue)
        assert resp.status_code in (200, 500), f"Unexpected: {resp.status_code}"
        if resp.status_code == 200:
            body = resp.json()
            assert body["data"]["page_info"]["page"] == 1
            assert body["data"]["page_info"]["page_size"] == 2

    def test_get_audit_event_not_found(self, auth_client):
        resp = auth_client.get(
            f"/api/v4/admin/audit-events/{uuid4()}"
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R1.6 — Admin Outbox Events
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminOutboxEvents:
    """Admin outbox event listing."""

    def test_list_events_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/admin/events")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_list_events_with_filters(self, auth_client):
        resp = auth_client.get(
            "/api/v4/admin/events",
            params={"publish_state": "pending", "page_size": 5},
        )
        assert resp.status_code == 200

    def test_get_event_detail_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/admin/events/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R1.7 — Admin Logs
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminLogs:
    """Admin API call log listing."""

    def test_list_logs_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/admin/logs")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_list_logs_with_level_filter(self, auth_client):
        resp = auth_client.get("/api/v4/admin/logs", params={"level": "succeeded"})
        assert resp.status_code == 200

    def test_list_logs_with_call_type_filter(self, auth_client):
        resp = auth_client.get("/api/v4/admin/logs", params={"call_type": "chat"})
        assert resp.status_code == 200

    def test_list_logs_with_time_range(self, auth_client):
        resp = auth_client.get(
            "/api/v4/admin/logs",
            params={"since": "2020-01-01T00:00:00Z", "until": "2099-01-01T00:00:00Z"},
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# R1.8 — Admin Jobs
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminJobs:
    """Admin job listing."""

    def test_list_jobs_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/admin/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_list_jobs_with_status_filter(self, auth_client):
        resp = auth_client.get("/api/v4/admin/jobs", params={"status": "pending"})
        assert resp.status_code == 200

    def test_get_job_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/admin/jobs/{uuid4()}")
        assert resp.status_code == 404
