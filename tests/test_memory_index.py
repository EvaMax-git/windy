"""P4-07 Memory Index FTS contract tests.

Tests:
* POST /api/v4/memory/index/rebuild-fts  — rebuild FTS
* GET  /api/v4/memory/index/states       — list index entries
* GET  /api/v4/memory/index/status       — aggregated summary
* Auto-creation on memory activate / create
* Auto-stale on memory update
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.config import get_settings
from mneme.db.base import get_db
from mneme.gateway.call import GatewayError
from mneme.main import create_app
from mneme.security import hash_password


TEST_USER_ID = uuid4()
TEST_PROJECT_ID = uuid4()
TEST_PROJECT_CODE = "test-idx"
TEST_REVIEW_ITEM_ID = uuid4()
TEST_CANDIDATE_ID = uuid4()


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def idx_client(monkeypatch):
    """Fixture: TestClient wired to SQLite :memory: with all necessary tables."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MNEME_SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def _idem() -> dict:
    return {"Idempotency-Key": str(uuid4())}


def _ok(resp, status=200):
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}: {resp.json()}"
    body = resp.json()
    assert "data" in body
    assert body["request_id"] is not None
    assert body["correlation_id"] is not None
    return body["data"]


# ── Table Setup ───────────────────────────────────────────────────────

def _create_tables(engine) -> None:
    with engine.begin() as conn:
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
        # P4-07: memory_index_entries (SQLite compatible — no tsvector)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_index_entries (
                memory_index_entry_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                memory_version INTEGER NOT NULL,
                project_id TEXT,
                index_profile TEXT NOT NULL DEFAULT 'default',
                embedding_model_id TEXT,
                content_hash TEXT NOT NULL,
                index_text TEXT NOT NULL,
                embedding TEXT,
                fts_state TEXT NOT NULL DEFAULT 'pending',
                vector_state TEXT NOT NULL DEFAULT 'pending',
                ready_at TEXT,
                stale_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (memory_id, memory_version, index_profile)
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


# ── Helpers ───────────────────────────────────────────────────────────

def _create_memory(client, title="Test Memory", memory_text="Content",
                   project_id=None, sensitivity_level="private"):
    body = {
        "title": title, "memory_text": memory_text,
        "project_id": str(project_id or TEST_PROJECT_ID),
        "sensitivity_level": sensitivity_level,
    }
    return _ok(client.post("/api/v4/memory", json=body, headers=_idem()), 201)


def _activate_memory(client, candidate_id=None, memory_text="Activated content"):
    return _ok(client.post("/api/v4/memory/activate", json={
        "candidate_id": str(candidate_id or TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "Activated",
        "memory_text": memory_text,
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem()), 201)


# ═══════════════════════════════════════════════════════════════════════
# Tests — Auto-creation on activate / create
# ═══════════════════════════════════════════════════════════════════════

class _FakeEmbeddingGateway:
    def __init__(self, *, embedding=None, error=None):
        self.embedding = embedding or [0.25, 0.75]
        self.error = error
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return {"data": {"data": [{"embedding": self.embedding}]}}


def test_activate_memory_creates_index_entry(idx_client):
    """Activating a memory auto-creates a memory_index_entries row."""
    client, engine = idx_client
    data = _activate_memory(client, memory_text="FTS test content")

    # Check index entries exist via API
    resp = client.get(f"/api/v4/memory/index/states?memory_id={data['memory_id']}")
    entries = _ok(resp)["items"]
    assert len(entries) == 1
    e = entries[0]
    assert e["memory_id"] == data["memory_id"]
    assert e["memory_version"] == 1
    assert e["fts_state"] == "ready"
    assert e["vector_state"] == "pending"
    assert "FTS test content" in e["index_text"]


def test_create_memory_creates_index_entry(idx_client):
    """Creating a draft memory auto-creates a memory_index_entries row."""
    client, engine = idx_client
    data = _create_memory(client, title="Draft", memory_text="Draft content")

    resp = client.get(f"/api/v4/memory/index/states?memory_id={data['memory_id']}")
    entries = _ok(resp)["items"]
    assert len(entries) == 1
    e = entries[0]
    assert e["memory_id"] == data["memory_id"]
    assert e["memory_version"] == 1
    assert e["fts_state"] == "ready"
    assert "Draft content" in e["index_text"]


# ═══════════════════════════════════════════════════════════════════════
# Tests — Stale on update
# ═══════════════════════════════════════════════════════════════════════

def test_update_memory_stales_old_entry(idx_client):
    """Updating a memory marks old index entry stale, creates new one."""
    client, engine = idx_client
    data = _create_memory(client, title="Orig", memory_text="Original")

    # Update
    _ok(client.patch(
        f"/api/v4/memory/{data['memory_id']}",
        json={"title": "Updated", "memory_text": "Updated content"},
        headers=_idem(),
    ))

    # Check index entries
    resp = client.get(f"/api/v4/memory/index/states?memory_id={data['memory_id']}")
    entries = _ok(resp)["items"]
    assert len(entries) == 2  # old stale + new ready

    # Sort by version
    entries.sort(key=lambda e: e["memory_version"])

    v1_entry = entries[0]
    assert v1_entry["memory_version"] == 1
    assert v1_entry["fts_state"] == "stale"

    v2_entry = entries[1]
    assert v2_entry["memory_version"] == 2
    assert v2_entry["fts_state"] == "ready"
    assert "Updated content" in v2_entry["index_text"]


def test_update_memory_only_status_no_stale(idx_client):
    """Status-only transitions (expire/restore) do NOT create new index entries."""
    client, engine = idx_client
    data = _activate_memory(client, memory_text="Status content")

    # Expire
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/expire",
        headers=_idem(),
    ))

    resp = client.get(f"/api/v4/memory/index/states?memory_id={data['memory_id']}")
    entries = _ok(resp)["items"]
    # Should still have only the original v1 entry (status-only changes don't trigger index update)
    assert len(entries) == 1
    assert entries[0]["memory_version"] == 1


# ═══════════════════════════════════════════════════════════════════════
# Tests — Index state listing
# ═══════════════════════════════════════════════════════════════════════

def test_list_index_states_empty(idx_client):
    """Listing index states with no entries returns empty list."""
    client, engine = idx_client
    data = _ok(client.get("/api/v4/memory/index/states"))
    assert data["items"] == []
    assert data["page_info"]["total_items"] == 0


def test_list_index_states_filter_by_fts_state(idx_client):
    """Filter index states by fts_state."""
    client, engine = idx_client
    _create_memory(client, title="M1", memory_text="Content 1")
    _create_memory(client, title="M2", memory_text="Content 2")

    # Only ready entries
    resp = client.get("/api/v4/memory/index/states?fts_state=ready")
    data = _ok(resp)
    assert len(data["items"]) == 2
    for e in data["items"]:
        assert e["fts_state"] == "ready"

    # No stale entries yet
    resp = client.get("/api/v4/memory/index/states?fts_state=stale")
    data = _ok(resp)
    assert len(data["items"]) == 0


def test_list_index_states_filter_by_project(idx_client):
    """Filter index states by project_id."""
    client, engine = idx_client
    _create_memory(client, title="M1", memory_text="C1")

    resp = client.get(f"/api/v4/memory/index/states?project_id={TEST_PROJECT_ID}")
    data = _ok(resp)
    assert len(data["items"]) == 1

    # Non-existent project
    resp = client.get(f"/api/v4/memory/index/states?project_id={uuid4()}")
    data = _ok(resp)
    assert len(data["items"]) == 0


def test_list_index_states_pagination(idx_client):
    """Index states support pagination."""
    client, engine = idx_client
    for i in range(5):
        _create_memory(client, title=f"Mem {i}", memory_text=f"Content {i}")

    resp = client.get("/api/v4/memory/index/states?page=1&page_size=2")
    data = _ok(resp)
    assert len(data["items"]) == 2
    assert data["page_info"]["total_items"] == 5
    assert data["page_info"]["total_pages"] == 3
    assert data["page_info"]["has_next"] is True


# ═══════════════════════════════════════════════════════════════════════
# Tests — rebuild-fts
# ═══════════════════════════════════════════════════════════════════════

def test_rebuild_fts_by_entry_id(idx_client):
    """Rebuild FTS by providing memory_index_entry_id."""
    client, engine = idx_client
    data = _create_memory(client, title="Rebuild", memory_text="Original rebuild")

    # Get the entry ID
    resp = client.get(f"/api/v4/memory/index/states?memory_id={data['memory_id']}")
    entries = _ok(resp)["items"]
    entry_id = entries[0]["memory_index_entry_id"]

    # Mark as stale first (simulate)
    with Session(engine) as db:
        db.execute(
            text("UPDATE memory_index_entries SET fts_state='stale' WHERE memory_index_entry_id=:eid"),
            {"eid": entry_id.replace("-", "")},
        )
        db.commit()

    # Rebuild
    rebuild_resp = client.post(
        "/api/v4/memory/index/rebuild-fts",
        json={"memory_index_entry_id": entry_id, "index_text": "Rebuild Updated content"},
    )
    rebuild_data = _ok(rebuild_resp)
    assert rebuild_data["rebuilt"] is True
    assert rebuild_data["entry"]["fts_state"] == "ready"

    # Verify
    resp = client.get(f"/api/v4/memory/index/states?memory_id={data['memory_id']}")
    entries = _ok(resp)["items"]
    assert entries[0]["fts_state"] == "ready"


def test_rebuild_fts_by_memory_id(idx_client):
    """Rebuild FTS by providing memory_id — creates fresh entry for current version."""
    client, engine = idx_client
    data = _create_memory(client, title="MemRebuild", memory_text="Memory rebuild content")

    rebuild_resp = client.post(
        "/api/v4/memory/index/rebuild-fts",
        json={"memory_id": data["memory_id"]},
    )
    rebuild_data = _ok(rebuild_resp)
    assert rebuild_data["rebuilt"] is True
    assert rebuild_data["memory_id"] == data["memory_id"]

    # Should have new entry
    resp = client.get(f"/api/v4/memory/index/states?memory_id={data['memory_id']}")
    entries = _ok(resp)["items"]
    assert len(entries) >= 1
    # Latest entry should be ready
    latest = entries[0]
    assert latest["fts_state"] == "ready"


def test_rebuild_fts_nonexistent_entry(idx_client):
    """Rebuilding a non-existent entry returns 404."""
    client, engine = idx_client
    resp = client.post(
        "/api/v4/memory/index/rebuild-fts",
        json={"memory_index_entry_id": str(uuid4())},
    )
    assert resp.status_code == 404


def test_rebuild_fts_missing_params(idx_client):
    """Rebuilding without entry_id or memory_id returns 400."""
    client, engine = idx_client
    resp = client.post(
        "/api/v4/memory/index/rebuild-fts",
        json={},
    )
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# Tests — index status
# ═══════════════════════════════════════════════════════════════════════

def test_rebuild_vector_by_entry_id_uses_gateway(idx_client, monkeypatch):
    """Vector rebuild calls Gateway embedding.create and marks vector ready."""
    client, engine = idx_client
    data = _create_memory(client, title="Vector", memory_text="Gateway vector content")
    entries = _ok(client.get(
        f"/api/v4/memory/index/states?memory_id={data['memory_id']}"
    ))["items"]
    entry_id = entries[0]["memory_index_entry_id"]

    fake = _FakeEmbeddingGateway(embedding=[0.1, 0.2, 0.3])
    monkeypatch.setattr("mneme.memory.embedding.get_gateway", lambda: fake)

    rebuild_resp = client.post(
        "/api/v4/memory/index/rebuild-vector",
        json={"memory_index_entry_id": entry_id},
    )
    rebuild_data = _ok(rebuild_resp)
    assert rebuild_data["rebuilt"] is True
    assert rebuild_data["embedding_dimensions"] == 3
    assert rebuild_data["entry"]["vector_state"] == "ready"
    assert fake.calls[0]["kwargs"]["capability_code"] == "embedding.create"
    assert fake.calls[0]["kwargs"]["call_type"] == "embedding"
    assert "Gateway vector content" in fake.calls[0]["kwargs"]["params"]["input"]

    entries = _ok(client.get(
        f"/api/v4/memory/index/states?memory_id={data['memory_id']}"
    ))["items"]
    assert entries[0]["vector_state"] == "ready"


def test_rebuild_vector_gateway_failure_marks_failed(idx_client, monkeypatch):
    """Gateway failures are persisted as vector_state=failed."""
    client, engine = idx_client
    data = _create_memory(client, title="VectorFail", memory_text="fails")
    entries = _ok(client.get(
        f"/api/v4/memory/index/states?memory_id={data['memory_id']}"
    ))["items"]
    entry_id = entries[0]["memory_index_entry_id"]

    fake = _FakeEmbeddingGateway(error=GatewayError(None, "gateway.test", "boom"))
    monkeypatch.setattr("mneme.memory.embedding.get_gateway", lambda: fake)

    resp = client.post(
        "/api/v4/memory/index/rebuild-vector",
        json={"memory_index_entry_id": entry_id},
    )
    assert resp.status_code == 503

    entries = _ok(client.get(
        f"/api/v4/memory/index/states?memory_id={data['memory_id']}"
    ))["items"]
    assert entries[0]["vector_state"] == "failed"
    assert "boom" in (entries[0]["last_error"] or "")


def test_hybrid_search_degrades_to_fts_when_vector_unavailable(idx_client, monkeypatch):
    """Hybrid mode returns FTS results and explicit degraded status."""
    client, engine = idx_client
    _create_memory(client, title="Search", memory_text="banana fallback text")

    fake = _FakeEmbeddingGateway(error=AssertionError("gateway should not be called"))
    monkeypatch.setattr("mneme.memory.embedding.get_gateway", lambda: fake)

    resp = client.get("/api/v4/memory/search?q=banana&mode=hybrid")
    data = _ok(resp)
    assert data["search_mode"] == "fts"
    assert data["degraded"] is True
    assert data["degradation_reason"] == "vector_unavailable"
    assert data["items"]
    assert data["items"][0]["degraded"] is True
    assert data["items"][0]["vector_state"] == "pending"
    assert fake.calls == []


def test_memory_search_exposes_stale_status(idx_client):
    """Search results carry stale markers from index state."""
    client, engine = idx_client
    data = _create_memory(client, title="StaleSearch", memory_text="stale needle text")
    entries = _ok(client.get(
        f"/api/v4/memory/index/states?memory_id={data['memory_id']}"
    ))["items"]
    entry_id = entries[0]["memory_index_entry_id"]

    with Session(engine) as db:
        db.execute(
            text("UPDATE memory_index_entries SET fts_state='stale' WHERE memory_index_entry_id=:eid"),
            {"eid": entry_id.replace("-", "")},
        )
        db.commit()

    resp = client.get("/api/v4/memory/search?q=needle&mode=fts")
    data = _ok(resp)
    assert data["stale_count"] == 1
    assert data["items"][0]["stale"] is True
    assert data["items"][0]["stale_reason"] == "fts_stale"


def test_index_status_summary(idx_client):
    """GET /memory/index/status returns aggregated counts."""
    client, engine = idx_client
    _create_memory(client, title="M1", memory_text="Content 1")
    _create_memory(client, title="M2", memory_text="Content 2")

    resp = client.get("/api/v4/memory/index/status")
    data = _ok(resp)
    assert data["total_entries"] == 2
    assert data["fts_ready"] == 2
    assert data["fts_stale"] == 0
    assert data["vector_pending"] == 2  # Phase 4: embedding always pending


def test_index_status_filtered_by_project(idx_client):
    """Index status is filterable by project_id."""
    client, engine = idx_client
    _create_memory(client, title="M1", memory_text="Content")

    resp = client.get(f"/api/v4/memory/index/status?project_id={TEST_PROJECT_ID}")
    data = _ok(resp)
    assert data["total_entries"] == 1
    assert data["fts_ready"] == 1


# ═══════════════════════════════════════════════════════════════════════
# Tests — Error cases
# ═══════════════════════════════════════════════════════════════════════

def test_index_states_bad_filter(idx_client):
    """Invalid fts_state filter returns 422 validation error."""
    client, engine = idx_client
    resp = client.get("/api/v4/memory/index/states?fts_state=invalid")
    assert resp.status_code in (400, 422)
