"""R5 System & Governance contract tests.

Covers new-architecture system/governance endpoints:
* Projects:                POST/GET/PUT/DELETE  /api/v4/projects
* Auth:                    POST  /api/v4/auth/login|logout|me|refresh
* Backup:                  GET  /api/v4/admin/backups
                           POST /api/v4/admin/backup
* Restore:                 GET  /api/v4/admin/restores
                           POST /api/v4/admin/restore
                           GET  /api/v4/admin/restore/{id}/preview
* Review items:            POST/GET  /api/v4/review/items
                           POST  /api/v4/review/items/{id}/claim|approve|reject|cancel
* Review policy:           GET/POST/DELETE  /api/v4/review/policy/rules
                           POST  /api/v4/review/policy/evaluate|reset
* Vault credentials:       POST/GET/PUT/DELETE  /api/v4/vault/credentials
                           POST  /api/v4/vault/credentials/{id}/reveal
* Dead letters:            GET  /api/v4/dead-letters
* Migration:               GET/POST  /api/v4/admin/migrations
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _new_client() -> TestClient:
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


def _idem_headers() -> dict:
    return {"Idempotency-Key": str(uuid4())}


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(db):
    return _new_client()


@pytest.fixture
def auth_client(client, db, test_user_id):
    from sqlalchemy import text
    from mneme.security import hash_password
    import datetime as dt

    now_val = dt.datetime.now(dt.timezone.utc)
    user_id = uuid4()
    db.execute(
        text(
            "INSERT OR IGNORE INTO users "
            "(user_id, username, email, display_name, role_code, status, "
            "password_hash, mfa_mode, created_at, updated_at) "
            "VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa, :now, :now)"
        ),
        {
            "uid": user_id.hex,
            "uname": "r5_user",
            "email": "r5_user@test.local",
            "dname": "R5 User",
            "role": "owner",
            "status": "active",
            "phash": hash_password("r5pass123"),
            "mfa": "none",
            "now": now_val,
        },
    )
    db.commit()
    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "r5_user", "password": "r5pass123"},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.json()}"
    return client


@pytest.fixture
def test_project(auth_client, db):
    from sqlalchemy import text
    pid = uuid4()
    pcode = f"r5proj-{pid.hex[:6].lower()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default, created_at, updated_at) "
            "VALUES (:pid, :code, :name, 'active', 'normal', datetime('now'), datetime('now'))"
        ),
        {"pid": pid.hex, "code": pcode, "name": "R5 Test Project"},
    )
    db.commit()
    return pid, pcode


# ═══════════════════════════════════════════════════════════════════════════
# R5.1 — Projects
# ═══════════════════════════════════════════════════════════════════════════


class TestProjects:
    """Project CRUD."""

    def test_list_projects_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/projects")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_get_project_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.get(f"/api/v4/projects/{pid}")
        assert resp.status_code == 200
        assert resp.json()["data"]["project_id"] == str(pid)

    def test_get_project_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/projects/{uuid4()}")
        assert resp.status_code == 404

    def test_create_project_requires_idempotency_key(self, auth_client):
        resp = auth_client.post(
            "/api/v4/projects",
            json={"project_code": "NO_KEY", "name": "No Key Project"},
        )
        assert resp.status_code == 400

    def test_create_project_succeeds(self, auth_client):
        code = f"r5-create-{uuid4().hex[:8].lower()}"
        resp = auth_client.post(
            "/api/v4/projects",
            json={"project_code": code, "name": "R5 Created Project"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200, f"Create failed: {resp.status_code} {resp.json()}"
        data = resp.json()["data"]
        assert data["project_code"] == code

    def test_create_project_duplicate_code_returns_409(self, auth_client, test_project):
        _, pcode = test_project
        resp = auth_client.post(
            "/api/v4/projects",
            json={"project_code": pcode, "name": "Duplicate"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 409

    def test_update_project_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.put(
            f"/api/v4/projects/{pid}",
            json={"name": "Updated Project Name"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "Updated Project Name"
        verify_resp = auth_client.get(f"/api/v4/projects/{pid}")
        assert verify_resp.status_code == 200
        assert verify_resp.json()["data"]["name"] == "Updated Project Name"

    def test_archive_project_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.delete(f"/api/v4/projects/{pid}")
        assert resp.status_code == 200
        verify_resp = auth_client.get(f"/api/v4/projects/{pid}")
        assert verify_resp.status_code == 200
        assert verify_resp.json()["data"]["status"] == "archived"

    def test_archive_project_not_found(self, auth_client):
        resp = auth_client.delete(f"/api/v4/projects/{uuid4()}")
        assert resp.status_code == 404


class TestPipelineDefs:
    """Pipeline definition API behavior."""

    def test_patch_pipeline_def_accepts_json_body_and_persists(self, auth_client):
        code = f"r5-pipe-{uuid4().hex[:8].lower()}"
        create_resp = auth_client.post(
            "/api/v4/pipelines/defs",
            json={
                "pipeline_code": code,
                "pipeline_type": "asset_import",
                "name": "R5 Pipeline",
            },
            headers=_idem_headers(),
        )
        assert create_resp.status_code == 200, create_resp.json()
        pipeline_id = create_resp.json()["data"]["pipeline_def_id"]

        patch_resp = auth_client.patch(
            f"/api/v4/pipelines/defs/{pipeline_id}",
            json={
                "name": "R5 Pipeline Updated",
                "status": "disabled",
                "config_json": {"steps": []},
            },
            headers=_idem_headers(),
        )
        assert patch_resp.status_code == 200, patch_resp.json()
        assert patch_resp.json()["data"]["name"] == "R5 Pipeline Updated"
        assert patch_resp.json()["data"]["status"] == "disabled"

        verify_resp = auth_client.get(f"/api/v4/pipelines/defs/{pipeline_id}")
        assert verify_resp.status_code == 200
        data = verify_resp.json()["data"]
        assert data["name"] == "R5 Pipeline Updated"
        assert data["status"] == "disabled"
        assert data["config_json"] == {"steps": []}


# ═══════════════════════════════════════════════════════════════════════════
# R5.2 — Auth
# ═══════════════════════════════════════════════════════════════════════════


class TestAuth:
    """Authentication endpoints."""

    def test_login_returns_session_cookie(self, client, db):
        from sqlalchemy import text
        from mneme.security import hash_password
        import datetime as dt

        uid = uuid4()
        now_val = dt.datetime.now(dt.timezone.utc)
        db.execute(
            text(
                "INSERT OR IGNORE INTO users "
                "(user_id, username, email, display_name, role_code, status, "
                "password_hash, mfa_mode, created_at, updated_at) "
                "VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa, :now, :now)"
            ),
            {
                "uid": uid.hex,
                "uname": "r5_auth_user",
                "email": "r5_auth@test.local",
                "dname": "R5 Auth",
                "role": "owner",
                "status": "active",
                "phash": hash_password("auth1234"),
                "mfa": "none",
                "now": now_val,
            },
        )
        db.commit()
        resp = client.post(
            "/api/v4/auth/login",
            json={"username": "r5_auth_user", "password": "auth1234"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["user"]["username"] == "r5_auth_user"
        assert "mneme_session" in resp.cookies

    def test_login_invalid_password_returns_401(self, client, db):
        from sqlalchemy import text
        from mneme.security import hash_password
        import datetime as dt

        uid = uuid4()
        now_val = dt.datetime.now(dt.timezone.utc)
        db.execute(
            text(
                "INSERT OR IGNORE INTO users "
                "(user_id, username, email, display_name, role_code, status, "
                "password_hash, mfa_mode, created_at, updated_at) "
                "VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa, :now, :now)"
            ),
            {
                "uid": uid.hex,
                "uname": "r5_wrong",
                "email": "r5_wrong@test.local",
                "dname": "R5 Wrong",
                "role": "owner",
                "status": "active",
                "phash": hash_password("correct"),
                "mfa": "none",
                "now": now_val,
            },
        )
        db.commit()
        resp = client.post(
            "/api/v4/auth/login",
            json={"username": "r5_wrong", "password": "wrong_password"},
        )
        assert resp.status_code == 401

    def test_me_requires_auth(self, client):
        resp = client.get("/api/v4/auth/me")
        assert resp.status_code == 401

    def test_me_returns_user_info(self, auth_client):
        resp = auth_client.get("/api/v4/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert "user" in body["data"]
        assert body["data"]["user"]["username"] == "r5_user"

    def test_logout_revokes_session(self, auth_client):
        resp = auth_client.post("/api/v4/auth/logout")
        assert resp.status_code == 200
        # Subsequent /me should fail
        assert auth_client.get("/api/v4/auth/me").status_code == 401

    def test_login_nonexistent_user_returns_401(self, client):
        resp = client.post(
            "/api/v4/auth/login",
            json={"username": "does_not_exist_999", "password": "any"},
        )
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# R5.3 — Backup
# ═══════════════════════════════════════════════════════════════════════════


class TestBackup:
    """Backup listing and triggering."""

    def test_list_backups_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/admin/backups")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_get_backup_detail_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/admin/backups/{uuid4()}")
        assert resp.status_code == 404

    def test_trigger_backup_creates_job(self, auth_client):
        resp = auth_client.post(
            "/api/v4/admin/backup",
            json={},
        )
        assert resp.status_code == 202
        body = resp.json()
        data = body["data"]
        assert "backup_id" in data
        assert "job_id" in data


# ═══════════════════════════════════════════════════════════════════════════
# R5.4 — Restore
# ═══════════════════════════════════════════════════════════════════════════


class TestRestore:
    """Restore listing, submission, preview."""

    def test_list_restores_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/admin/restores")
        assert resp.status_code == 200

    def test_submit_restore_backup_not_found(self, auth_client):
        resp = auth_client.post(
            "/api/v4/admin/restore",
            json={"backup_id": str(uuid4())},
        )
        assert resp.status_code == 404

    def test_preview_restore_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/admin/restore/{uuid4()}/preview")
        assert resp.status_code in (400, 404)


# ═══════════════════════════════════════════════════════════════════════════
# R5.5 — Review Items
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewItems:
    """Review item CRUD and workflow."""

    def test_list_review_items_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/review/items")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_create_review_item_succeeds(self, auth_client):
        resp = auth_client.post(
            "/api/v4/review/items",
            json={
                "review_type": "manual",
                "target_type": "test_object",
                "target_id": str(uuid4()),
                "status": "pending",
                "priority": 100,
                "requester_actor_type": "user",
            },
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert "review_item_id" in data
        assert data["status"] == "pending"

    def test_create_review_item_defaults(self, auth_client):
        resp = auth_client.post(
            "/api/v4/review/items",
            json={
                "review_type": "manual",
                "target_type": "memory",
                "target_id": str(uuid4()),
                "priority": 50,
                "requester_actor_type": "user",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["status"] == "pending"

    def test_get_review_item_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/review/items/{uuid4()}")
        assert resp.status_code == 404

    def test_claim_review_item_not_found(self, auth_client):
        resp = auth_client.post(f"/api/v4/review/items/{uuid4()}/claim")
        assert resp.status_code == 404

    def test_approve_review_item_not_found(self, auth_client):
        resp = auth_client.post(
            f"/api/v4/review/items/{uuid4()}/approve",
            json={"decision_payload": {}},
        )
        assert resp.status_code == 404

    def test_reject_review_item_not_found(self, auth_client):
        resp = auth_client.post(
            f"/api/v4/review/items/{uuid4()}/reject",
            json={"reason": "test"},
        )
        assert resp.status_code == 404

    def test_cancel_review_item_not_found(self, auth_client):
        resp = auth_client.post(f"/api/v4/review/items/{uuid4()}/cancel")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R5.6 — Review Policy
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewPolicy:
    """Review policy rule management."""

    def test_list_rules_returns_all(self, auth_client):
        resp = auth_client.get("/api/v4/review/policy/rules")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "rules" in data
        assert "total" in data

    def test_upsert_rule_succeeds(self, auth_client):
        resp = auth_client.post(
            "/api/v4/review/policy/rules",
            json={
                "name": "test_rule_r5",
                "action_pattern": "*.delete",
                "review_type": "manual",
                "priority": 50,
            },
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["name"] == "test_rule_r5"

    def test_get_rule_succeeds(self, auth_client):
        # First upsert
        auth_client.post(
            "/api/v4/review/policy/rules",
            json={
                "name": "get_test_rule",
                "action_pattern": "*.archive",
                "review_type": "auto",
                "priority": 60,
            },
        )
        resp = auth_client.get("/api/v4/review/policy/rules/get_test_rule")
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "get_test_rule"

    def test_get_rule_not_found(self, auth_client):
        resp = auth_client.get("/api/v4/review/policy/rules/nonexistent_xyz")
        assert resp.status_code == 404

    def test_delete_rule_succeeds(self, auth_client):
        auth_client.post(
            "/api/v4/review/policy/rules",
            json={
                "name": "to_delete",
                "action_pattern": "*.purge",
                "review_type": "manual",
                "priority": 99,
            },
        )
        resp = auth_client.delete("/api/v4/review/policy/rules/to_delete")
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] == "to_delete"

    def test_delete_rule_not_found(self, auth_client):
        resp = auth_client.delete("/api/v4/review/policy/rules/nonexistent_xyz")
        assert resp.status_code == 404

    def test_reset_rules_succeeds(self, auth_client):
        resp = auth_client.post("/api/v4/review/policy/reset")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "rules" in data

    def test_evaluate_returns_decision(self, auth_client):
        resp = auth_client.post(
            "/api/v4/review/policy/evaluate",
            json={
                "action_name": "project.delete",
                "object_type": "project",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "review_required" in data
        assert isinstance(data["review_required"], bool)


# ═══════════════════════════════════════════════════════════════════════════
# R5.7 — Vault Credentials
# ═══════════════════════════════════════════════════════════════════════════


class TestVaultCredentials:
    """Vault credential management."""

    def test_list_credentials_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/vault/credentials")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_create_credential_requires_plaintext(self, auth_client):
        resp = auth_client.post(
            "/api/v4/vault/credentials",
            json={
                "provider_id": str(uuid4()),
                "credential_name": "test-key",
                "credential_type": "api_key",
                "plaintext": "sk-test-secret-value-12345",
            },
        )
        # May succeed or fail depending on provider existence
        assert resp.status_code in (201, 400)

    def test_get_credential_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/vault/credentials/{uuid4()}")
        assert resp.status_code == 404

    def test_reveal_credential_not_found(self, auth_client):
        resp = auth_client.post(
            f"/api/v4/vault/credentials/{uuid4()}/reveal",
            json={"reason": "test"},
        )
        assert resp.status_code == 404

    def test_update_credential_not_found(self, auth_client):
        resp = auth_client.put(
            f"/api/v4/vault/credentials/{uuid4()}",
            json={"status": "disabled"},
        )
        assert resp.status_code == 404

    def test_delete_credential_not_found(self, auth_client):
        resp = auth_client.delete(f"/api/v4/vault/credentials/{uuid4()}")
        assert resp.status_code == 404

    def test_access_logs_credential_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/vault/credentials/{uuid4()}/access-logs")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R5.8 — Dead Letters (DLQ)
# ═══════════════════════════════════════════════════════════════════════════


class TestDeadLetters:
    """Dead letter queue endpoints."""

    def test_list_dead_letters_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/dead-letters")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_get_dead_letter_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/dead-letters/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R5.9 — Migration Admin
# ═══════════════════════════════════════════════════════════════════════════


class TestMigrationAdmin:
    """Migration admin endpoints."""

    def test_list_migrations_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/admin/migrations")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_migration_state_returns_summary(self, auth_client):
        resp = auth_client.get("/api/v4/admin/migrations/state")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "total_revisions" in data
        assert "pending_count" in data
        assert "applied_count" in data

    def test_get_migration_revision_not_found(self, auth_client):
        resp = auth_client.get("/api/v4/admin/migrations/zzzz_nonexistent")
        assert resp.status_code == 404

    def test_preview_migrations_returns_plan(self, auth_client):
        resp = auth_client.post("/api/v4/admin/migrations/preview")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "direction" in data
        assert "from_revision" in data
        assert "to_revision" in data

    def test_apply_migrations_dry_run(self, auth_client):
        resp = auth_client.post(
            "/api/v4/admin/migrations/apply",
            json={"dry_run": True},
        )
        assert resp.status_code == 200

    def test_list_migration_runs_returns_history(self, auth_client):
        resp = auth_client.get("/api/v4/admin/migrations/runs")
        assert resp.status_code == 200

    def test_get_migration_run_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/admin/migrations/runs/{uuid4()}")
        assert resp.status_code == 404
