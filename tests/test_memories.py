"""P4-05 Memories CRUD contract tests.

Tests all 9 API endpoints:
* POST   /api/v4/memory                      — create (manual, draft)
* POST   /api/v4/memory/activate             — activate from candidate
* GET    /api/v4/memory                      — list (paginated, filterable)
* GET    /api/v4/memory/{memory_id}          — get detail
* PATCH  /api/v4/memory/{memory_id}          — update content
* POST   /api/v4/memory/{memory_id}/merge    — merge
* POST   /api/v4/memory/{memory_id}/expire   — expire
* POST   /api/v4/memory/{memory_id}/restore  — restore
* DELETE /api/v4/memory/{memory_id}          — soft-delete

Also validates:
- State machine transitions
- canonical_key uniqueness
- Version recording
- Merge relations
- CHECK constraint (active ⇒ activated_by_review_item_id)
"""

from __future__ import annotations

import datetime as _dt_mod
import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import bindparam, create_engine, event, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.config import get_settings
from mneme.db.base import get_db
from mneme.main import create_app
from mneme.security import hash_password


TEST_USER_ID = uuid4()
TEST_PROJECT_ID = uuid4()
TEST_PROJECT_CODE = "test-proj"
TEST_CANDIDATE_ID = uuid4()
TEST_REVIEW_ITEM_ID = uuid4()


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


def _register_sqlite_compat(engine):
    """Register ``now()`` and ``gen_random_uuid()`` SQL functions on the engine."""
    from uuid import uuid4 as _uuid4

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _conn_record):
        try:
            dbapi_conn.create_function(
                "now", 0, lambda: _dt_mod.datetime.now(_dt_mod.timezone.utc).isoformat()
            )
        except Exception:
            pass
        try:
            dbapi_conn.create_function(
                "gen_random_uuid", 0, lambda: _uuid4().hex
            )
        except Exception:
            pass


@pytest.fixture
def mem_client(monkeypatch):
    """Fixture: TestClient wired to SQLite :memory: with required tables."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MNEME_SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _register_sqlite_compat(engine)
    _create_tables(engine)
    _seed_data(engine)

    app = create_app()

    def override_get_db():
        db = Session(engine, expire_on_commit=False)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        login_resp = client.post(
            "/api/v4/auth/login",
            json={"username": "test_user", "password": "test-pass"},
        )
        assert login_resp.status_code == 200, f"Login failed: {login_resp.json()}"
        yield client, engine


def _idem_headers() -> dict:
    return {"Idempotency-Key": str(uuid4())}


def _assert_ok(resp, status=200):
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}: {resp.json()}"
    body = resp.json()
    assert "data" in body
    assert body["request_id"] is not None
    assert body["correlation_id"] is not None
    return body["data"]


def _create_memory(client, *, title="Test Memory", memory_text="Content",
                   canonical_key=None, project_id=None, sensitivity_level="private"):
    body = {
        "title": title,
        "memory_text": memory_text,
        "project_id": str(project_id or TEST_PROJECT_ID),
        "sensitivity_level": sensitivity_level,
    }
    if canonical_key:
        body["canonical_key"] = canonical_key
    return _assert_ok(client.post("/api/v4/memory", json=body, headers=_idem_headers()), 201)


# ═══════════════════════════════════════════════════════════════════════
# Table setup
# ═══════════════════════════════════════════════════════════════════════


def _create_tables(engine) -> None:
    with engine.begin() as conn:
        # Users + sessions
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                email TEXT UNIQUE, display_name TEXT NOT NULL,
                role_code TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_bootstrap',
                password_hash TEXT NOT NULL, mfa_mode TEXT NOT NULL DEFAULT 'none',
                locale TEXT NOT NULL DEFAULT 'zh-CN', timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                last_login_at TEXT, disabled_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                session_token_hash TEXT NOT NULL UNIQUE, session_token_prefix TEXT NOT NULL,
                auth_method TEXT NOT NULL DEFAULT 'password', device_label TEXT,
                device_fingerprint TEXT, ip_hash TEXT, user_agent TEXT,
                step_up_verified_at TEXT, last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL, revoked_at TEXT, revoke_reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        # Audit + events
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_events (
                audit_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
                actor_type TEXT NOT NULL, actor_id TEXT,
                auth_context_type TEXT, auth_context_id TEXT,
                action TEXT NOT NULL, object_type TEXT, object_id TEXT,
                project_id TEXT, result TEXT NOT NULL DEFAULT 'success',
                reason_code TEXT, sensitivity_level TEXT NOT NULL DEFAULT 'normal',
                correlation_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '', review_item_id TEXT,
                diff_summary TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                event_type TEXT NOT NULL, aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL, aggregate_version INTEGER NOT NULL DEFAULT 1,
                correlation_id TEXT, causation_id TEXT,
                idempotency_key TEXT UNIQUE, producer TEXT NOT NULL DEFAULT 'mneme-api',
                payload_json TEXT NOT NULL DEFAULT '{}', visibility TEXT NOT NULL DEFAULT 'internal',
                publish_state TEXT NOT NULL DEFAULT 'pending',
                occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
                committed_at TEXT NOT NULL DEFAULT (datetime('now')),
                published_at TEXT, last_error TEXT
            )
        """))
        # Projects
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY, project_code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, description TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                sensitivity_default TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')), archived_at TEXT
            )
        """))
        # Review items
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS review_items (
                review_item_id TEXT PRIMARY KEY,
                project_id TEXT, review_type TEXT NOT NULL,
                target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                target_version INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 100,
                requester_actor_type TEXT NOT NULL DEFAULT 'system',
                requester_actor_id TEXT, reviewer_id TEXT,
                decision TEXT, reason TEXT,
                decision_payload TEXT NOT NULL DEFAULT '{}',
                due_at TEXT, decided_at TEXT, expires_at TEXT,
                correlation_id TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        # Memory candidates
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_candidates (
                candidate_id TEXT PRIMARY KEY, project_id TEXT,
                source_type TEXT NOT NULL, source_id TEXT,
                submitted_by_actor_type TEXT NOT NULL, submitted_by_actor_id TEXT,
                title TEXT, candidate_text TEXT NOT NULL,
                candidate_hash TEXT NOT NULL,
                sensitivity_level TEXT NOT NULL DEFAULT 'private',
                candidate_status TEXT NOT NULL DEFAULT 'pending_review',
                confidence_score REAL, review_required INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (project_id, candidate_hash)
            )
        """))
        # Memories
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY, project_id TEXT,
                canonical_key TEXT NOT NULL,
                title TEXT, memory_text TEXT NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 1,
                sensitivity_level TEXT NOT NULL DEFAULT 'private',
                status TEXT NOT NULL DEFAULT 'draft',
                activated_from_candidate_id TEXT, activated_by_review_item_id TEXT,
                activated_at TEXT, expired_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (project_id, canonical_key)
            )
        """))
        # Memory versions
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_versions (
                memory_version_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL, version INTEGER NOT NULL,
                action TEXT NOT NULL,
                before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}',
                actor_type TEXT NOT NULL, actor_id TEXT,
                review_item_id TEXT, candidate_id TEXT, event_id TEXT,
                reason TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (memory_id, version)
            )
        """))
        # Memory sources
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_sources (
                memory_source_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL, memory_version INTEGER NOT NULL,
                candidate_id TEXT, raw_event_id TEXT, asset_id TEXT,
                document_id TEXT, block_id TEXT, message_id TEXT,
                source_span TEXT NOT NULL DEFAULT '{}', confidence REAL,
                source_role TEXT NOT NULL DEFAULT 'evidence',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        # Memory relations
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_relations (
                memory_relation_id TEXT PRIMARY KEY, project_id TEXT,
                from_memory_id TEXT NOT NULL, from_memory_version INTEGER,
                to_memory_id TEXT NOT NULL, to_memory_version INTEGER,
                relation_type TEXT NOT NULL,
                relation_status TEXT NOT NULL DEFAULT 'active',
                created_by_review_item_id TEXT, reason TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (from_memory_id, to_memory_id, relation_type)
            )
        """))


def _seed_data(engine) -> None:
    with Session(engine) as db:
        db.execute(text("""
            INSERT INTO users (user_id, username, email, display_name, role_code,
                               status, password_hash, mfa_mode)
            VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa)
        """), {
            "uid": TEST_USER_ID.hex, "uname": "test_user", "email": "test@test.local",
            "dname": "Test User", "role": "owner", "status": "active",
            "phash": hash_password("test-pass"), "mfa": "none",
        })
        db.execute(text("""
            INSERT INTO projects (project_id, project_code, name, status, sensitivity_default)
            VALUES (:pid, :pcode, :pname, 'active', 'normal')
        """), {
            "pid": TEST_PROJECT_ID.hex, "pcode": TEST_PROJECT_CODE, "pname": "Test Project",
        })
        db.execute(text("""
            INSERT INTO review_items (review_item_id, project_id, review_type, target_type,
                                      target_id, status, idempotency_key)
            VALUES (:rid, :pid, 'memory_candidate', 'memory_candidate', :cid, 'approved', :ikey)
        """), {
            "rid": TEST_REVIEW_ITEM_ID.hex, "pid": TEST_PROJECT_ID.hex,
            "cid": TEST_CANDIDATE_ID.hex, "ikey": str(uuid4()),
        })
        db.execute(text("""
            INSERT INTO memory_candidates (candidate_id, project_id, source_type,
                                           submitted_by_actor_type, title, candidate_text,
                                           candidate_hash, candidate_status)
            VALUES (:cid, :pid, 'manual', 'user', 'Test Candidate', 'Candidate text',
                    'dummy-hash', 'approved')
        """), {
            "cid": TEST_CANDIDATE_ID.hex, "pid": TEST_PROJECT_ID.hex,
        })
        db.commit()


# ═══════════════════════════════════════════════════════════════════════
# Tests — Create
# ═══════════════════════════════════════════════════════════════════════


def test_create_memory_manual(mem_client):
    """Manual creation yields draft status with auto canonical_key."""
    client, _engine = mem_client
    data = _create_memory(client, title="Hello", memory_text="World")
    assert data["title"] == "Hello"
    assert data["memory_text"] == "World"
    assert data["status"] == "draft"
    assert data["current_version"] == 1
    assert data["canonical_key"] == f"{TEST_PROJECT_CODE}-mem-1"
    assert data["activated_from_candidate_id"] is None
    assert data["activated_by_review_item_id"] is None


def test_create_memory_custom_canonical_key(mem_client):
    """Custom canonical_key can be supplied."""
    client, _engine = mem_client
    data = _create_memory(client, canonical_key="my-custom-key")
    assert data["canonical_key"] == "my-custom-key"


def test_create_memory_duplicate_canonical_key(mem_client):
    """Duplicate canonical_key in same project returns 409."""
    client, _engine = mem_client
    _create_memory(client, canonical_key="unique-key")
    resp = client.post("/api/v4/memory", json={
        "title": "Dup", "memory_text": "dup",
        "project_id": str(TEST_PROJECT_ID),
        "canonical_key": "unique-key",
    }, headers=_idem_headers())
    assert resp.status_code == 409


def test_create_memory_missing_idempotency(mem_client):
    """Missing Idempotency-Key returns 400."""
    client, _engine = mem_client
    resp = client.post("/api/v4/memory", json={
        "title": "NoKey", "memory_text": "text",
        "project_id": str(TEST_PROJECT_ID),
    })
    assert resp.status_code == 400


def test_create_memory_empty_text(mem_client):
    """Empty memory_text fails validation (400)."""
    client, _engine = mem_client
    resp = client.post("/api/v4/memory", json={
        "title": "Empty", "memory_text": "",
        "project_id": str(TEST_PROJECT_ID),
    }, headers=_idem_headers())
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# Tests — Activate
# ═══════════════════════════════════════════════════════════════════════


def test_activate_memory_from_candidate(mem_client):
    """Activate creates active memory with version + source."""
    client, _engine = mem_client
    resp = client.post("/api/v4/memory/activate", json={
        "candidate_id": str(TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "Activated Memory",
        "memory_text": "From candidate",
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem_headers())
    data = _assert_ok(resp, 201)
    assert data["status"] == "active"
    assert data["current_version"] == 1
    assert data["activated_from_candidate_id"] == str(TEST_CANDIDATE_ID)
    assert data["activated_by_review_item_id"] == str(TEST_REVIEW_ITEM_ID)
    assert data["canonical_key"] == f"{TEST_PROJECT_CODE}-mem-1"


def test_activate_memory_idempotent(mem_client):
    """Same idempotency key returns existing memory."""
    client, _engine = mem_client
    ikey = str(uuid4())
    headers = {"Idempotency-Key": ikey}
    resp1 = client.post("/api/v4/memory/activate", json={
        "candidate_id": str(TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "Idem Memory",
        "memory_text": "Idem text",
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=headers)
    assert resp1.status_code == 201
    data1 = resp1.json()["data"]

    resp2 = client.post("/api/v4/memory/activate", json={
        "candidate_id": str(TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "Idem Memory",
        "memory_text": "Idem text",
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=headers)
    assert resp2.status_code == 201
    data2 = resp2.json()["data"]
    assert data1["memory_id"] == data2["memory_id"]


# ═══════════════════════════════════════════════════════════════════════
# Tests — Read
# ═══════════════════════════════════════════════════════════════════════


def test_get_memory(mem_client):
    client, _engine = mem_client
    created = _create_memory(client)
    resp = client.get(f"/api/v4/memory/{created['memory_id']}")
    data = _assert_ok(resp)
    assert data["memory_id"] == created["memory_id"]
    assert data["title"] == "Test Memory"


def test_get_memory_not_found(mem_client):
    client, _engine = mem_client
    resp = client.get(f"/api/v4/memory/{uuid4()}")
    assert resp.status_code == 404


def test_list_memories_empty(mem_client):
    client, _engine = mem_client
    resp = client.get("/api/v4/memory")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] == 0
    assert data["items"] == []


def test_list_memories_paginated(mem_client):
    client, _engine = mem_client
    for i in range(5):
        _create_memory(client, title=f"Memory-{i}", memory_text=f"Content-{i}")
    resp = client.get("/api/v4/memory?page=1&page_size=3")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] == 5
    assert data["page_info"]["total_pages"] == 2
    assert len(data["items"]) == 3
    assert data["page_info"]["has_next"] is True


def test_list_memories_filter_by_status(mem_client):
    client, _engine = mem_client
    _create_memory(client, title="A")
    resp = client.get("/api/v4/memory?status=draft")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] >= 1
    for item in data["items"]:
        assert item["status"] == "draft"


def test_list_memories_filter_by_project(mem_client):
    client, _engine = mem_client
    _create_memory(client, title="A")
    resp = client.get(f"/api/v4/memory?project_id={uuid4()}")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] == 0


def test_list_memories_search(mem_client):
    client, _engine = mem_client
    _create_memory(client, title="Alpha", memory_text="Bravo content")
    _create_memory(client, title="Charlie", memory_text="Delta")
    resp = client.get("/api/v4/memory?search=Bravo")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] == 1
    assert data["items"][0]["title"] == "Alpha"


# ═══════════════════════════════════════════════════════════════════════
# Tests — Update
# ═══════════════════════════════════════════════════════════════════════


def test_update_memory(mem_client):
    client, _engine = mem_client
    created = _create_memory(client, title="Old Title", memory_text="Old text")
    mid = created["memory_id"]
    assert created["current_version"] == 1

    resp = client.patch(f"/api/v4/memory/{mid}", json={
        "title": "New Title",
        "memory_text": "New text",
    }, headers=_idem_headers())
    data = _assert_ok(resp)
    assert data["title"] == "New Title"
    assert data["memory_text"] == "New text"
    assert data["current_version"] == 2


def test_update_memory_partial(mem_client):
    client, _engine = mem_client
    created = _create_memory(client, title="Old", memory_text="text")
    mid = created["memory_id"]

    resp = client.patch(f"/api/v4/memory/{mid}", json={
        "title": "New Title Only",
    }, headers=_idem_headers())
    data = _assert_ok(resp)
    assert data["title"] == "New Title Only"
    assert data["memory_text"] == "text"  # Unchanged
    assert data["current_version"] == 2


def test_update_memory_not_found(mem_client):
    client, _engine = mem_client
    resp = client.patch(f"/api/v4/memory/{uuid4()}", json={
        "title": "Ghost",
    }, headers=_idem_headers())
    assert resp.status_code == 404


def test_update_memory_missing_idempotency(mem_client):
    client, _engine = mem_client
    created = _create_memory(client)
    resp = client.patch(f"/api/v4/memory/{created['memory_id']}", json={
        "title": "NoKey",
    })
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# Tests — State machine: expire / restore / delete
# ═══════════════════════════════════════════════════════════════════════


def test_expire_memory(mem_client):
    """Expire transitions active → expired."""
    client, _engine = mem_client
    # Activate to get an active memory
    resp = client.post("/api/v4/memory/activate", json={
        "candidate_id": str(TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "To Expire",
        "memory_text": "Content",
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem_headers())
    mid = _assert_ok(resp, 201)["memory_id"]

    # Expire
    resp = client.post(f"/api/v4/memory/{mid}/expire", headers=_idem_headers())
    data = _assert_ok(resp)
    assert data["status"] == "expired"
    assert data["expired_at"] is not None


def test_expire_draft_memory_fails(mem_client):
    """Cannot expire a draft memory."""
    client, _engine = mem_client
    created = _create_memory(client)
    mid = created["memory_id"]

    resp = client.post(f"/api/v4/memory/{mid}/expire", headers=_idem_headers())
    assert resp.status_code == 409


def test_restore_expired_memory(mem_client):
    """Restore expired → active."""
    client, _engine = mem_client
    resp = client.post("/api/v4/memory/activate", json={
        "candidate_id": str(TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "To Restore",
        "memory_text": "Content",
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem_headers())
    mid = _assert_ok(resp, 201)["memory_id"]

    client.post(f"/api/v4/memory/{mid}/expire", headers=_idem_headers())
    resp = client.post(f"/api/v4/memory/{mid}/restore", headers=_idem_headers())
    data = _assert_ok(resp)
    assert data["status"] == "active"
    assert data["expired_at"] is None


def test_restore_active_memory_fails(mem_client):
    """Cannot restore an already active memory."""
    client, _engine = mem_client
    resp = client.post("/api/v4/memory/activate", json={
        "candidate_id": str(TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "Active",
        "memory_text": "Content",
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem_headers())
    mid = _assert_ok(resp, 201)["memory_id"]

    resp = client.post(f"/api/v4/memory/{mid}/restore", headers=_idem_headers())
    assert resp.status_code == 409


def test_delete_memory(mem_client):
    """Soft-delete transitions any status → deleted."""
    client, _engine = mem_client
    created = _create_memory(client)
    mid = created["memory_id"]

    resp = client.delete(f"/api/v4/memory/{mid}", headers=_idem_headers())
    data = _assert_ok(resp)
    assert data["deleted"] is True

    # Verify via GET
    get_resp = client.get(f"/api/v4/memory/{mid}")
    get_data = _assert_ok(get_resp)
    assert get_data["status"] == "deleted"


def test_delete_already_deleted(mem_client):
    """Deleting an already deleted memory returns 409."""
    client, _engine = mem_client
    created = _create_memory(client)
    mid = created["memory_id"]
    client.delete(f"/api/v4/memory/{mid}", headers=_idem_headers())

    resp = client.delete(f"/api/v4/memory/{mid}", headers=_idem_headers())
    assert resp.status_code == 409


def test_restore_deleted_memory(mem_client):
    """Restore deleted → active."""
    client, _engine = mem_client
    resp = client.post("/api/v4/memory/activate", json={
        "candidate_id": str(TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "RestoreFromDel",
        "memory_text": "Content",
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem_headers())
    mid = _assert_ok(resp, 201)["memory_id"]

    client.delete(f"/api/v4/memory/{mid}", headers=_idem_headers())
    resp = client.post(f"/api/v4/memory/{mid}/restore", headers=_idem_headers())
    data = _assert_ok(resp)
    assert data["status"] == "active"


# ═══════════════════════════════════════════════════════════════════════
# Tests — Merge
# ═══════════════════════════════════════════════════════════════════════


def test_merge_memory(mem_client):
    """Merge survivor absorbs consumed text."""
    client, engine = mem_client
    mem_a = _create_memory(client, title="Survivor", memory_text="Text A")
    mem_b = _create_memory(client, title="Consumed", memory_text="Text B")

    resp = client.post(f"/api/v4/memory/{mem_a['memory_id']}/merge", json={
        "target_memory_id": mem_b["memory_id"],
        "reason": "Dedup",
    }, headers=_idem_headers())
    data = _assert_ok(resp)
    assert "Text A" in data["memory_text"]
    assert "Text B" in data["memory_text"]
    assert data["status"] in ("draft", "active")

    # Consumed (B) is now merged
    get_b = client.get(f"/api/v4/memory/{mem_b['memory_id']}")
    b_data = _assert_ok(get_b)
    assert b_data["status"] == "merged"


def test_merge_self_fails(mem_client):
    """Cannot merge a memory into itself."""
    client, _engine = mem_client
    created = _create_memory(client)
    mid = created["memory_id"]

    resp = client.post(f"/api/v4/memory/{mid}/merge", json={
        "target_memory_id": mid,
    }, headers=_idem_headers())
    assert resp.status_code in (400, 409)


def test_merge_not_found(mem_client):
    client, _engine = mem_client
    created = _create_memory(client)
    resp = client.post(f"/api/v4/memory/{created['memory_id']}/merge", json={
        "target_memory_id": str(uuid4()),
    }, headers=_idem_headers())
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# Tests — Version recording
# ═══════════════════════════════════════════════════════════════════════


def test_create_records_version(mem_client):
    """Manual create records version 1."""
    client, engine = mem_client
    _create_memory(client, title="VTest", memory_text="Version test")

    with Session(engine) as db:
        row = db.execute(text(
            "SELECT version, action FROM memory_versions ORDER BY created_at DESC LIMIT 1"
        )).first()
        assert row is not None
        assert row[0] == 1
        assert row[1] == "create"


def test_update_records_version(mem_client):
    """Update records version 2."""
    client, engine = mem_client
    created = _create_memory(client, title="V1", memory_text="v1 text")
    mid = created["memory_id"]

    client.patch(f"/api/v4/memory/{mid}", json={
        "title": "V2",
    }, headers=_idem_headers())

    # Convert to UUID so the parameter binds correctly against PG_UUID hex format
    mid_uuid = UUID(mid)
    with Session(engine) as db:
        rows = db.execute(text(
            "SELECT version, action FROM memory_versions WHERE memory_id = :mid ORDER BY version"
        ).bindparams(bindparam("mid", type_=PG_UUID(as_uuid=True))), {"mid": mid_uuid}).all()
        assert len(rows) == 2
        assert rows[0].version == 1 and rows[0].action == "create"
        assert rows[1].version == 2 and rows[1].action == "update"


# ═══════════════════════════════════════════════════════════════════════
# Tests — canonical_key auto-increment
# ═══════════════════════════════════════════════════════════════════════


def test_canonical_key_auto_increment(mem_client):
    """Each new memory gets the next sequential canonical_key."""
    client, _engine = mem_client
    m1 = _create_memory(client, title="First")
    m2 = _create_memory(client, title="Second")
    m3 = _create_memory(client, title="Third")

    assert m1["canonical_key"] == f"{TEST_PROJECT_CODE}-mem-1"
    assert m2["canonical_key"] == f"{TEST_PROJECT_CODE}-mem-2"
    assert m3["canonical_key"] == f"{TEST_PROJECT_CODE}-mem-3"


# ═══════════════════════════════════════════════════════════════════════
# Tests — Sensitivity level
# ═══════════════════════════════════════════════════════════════════════


def test_create_with_sensitivity(mem_client):
    client, _engine = mem_client
    data = _create_memory(client, sensitivity_level="sensitive")
    assert data["sensitivity_level"] == "sensitive"


def test_list_filter_by_sensitivity(mem_client):
    client, _engine = mem_client
    _create_memory(client, title="Normal", sensitivity_level="normal")
    _create_memory(client, title="Private", sensitivity_level="private")
    resp = client.get("/api/v4/memory?sensitivity_level=private")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] >= 1
    for item in data["items"]:
        assert item["sensitivity_level"] == "private"
