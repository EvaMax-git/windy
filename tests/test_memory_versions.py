"""P4-06 Memory Versions + Sources contract tests.

Tests both DB-layer functions and API endpoints:
* GET    /api/v4/memory/{memory_id}/versions        — version history
* GET    /api/v4/memory/{memory_id}/versions/{v}    — specific version
* POST   /api/v4/memory/{memory_id}/sources         — add source link
* GET    /api/v4/memory/{memory_id}/sources         — list sources
* DELETE /api/v4/memory/sources/{memory_source_id}   — remove source link

Also validates:
* Version records created automatically during memory lifecycle
* before_json / after_json auditability
* Version continuity (no gaps)
* Source multi-type references
* Source source_role CHECK constraint
* Source source_span format
"""

from __future__ import annotations

import datetime as _dt_mod
import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
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
TEST_PROJECT_CODE = "p4-06-proj"
TEST_CANDIDATE_ID = uuid4()
TEST_REVIEW_ITEM_ID = uuid4()


# ═══════════════════════════════════════════════════════════════════════
# SQLite compat
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


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(monkeypatch):
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

    with TestClient(app) as c:
        login_resp = c.post(
            "/api/v4/auth/login",
            json={"username": "test_user", "password": "test-pass"},
        )
        assert login_resp.status_code == 200, f"Login failed: {login_resp.json()}"
        yield c, engine


def _idem_headers() -> dict:
    return {"Idempotency-Key": str(uuid4())}


def _assert_ok(resp, status=200):
    assert resp.status_code == status, (
        f"Expected {status}, got {resp.status_code}: {resp.json()}"
    )
    body = resp.json()
    assert "data" in body
    assert body["request_id"] is not None
    assert body["correlation_id"] is not None
    return body["data"]


def _create_memory(client, **kw):
    """Create a memory (draft) via POST /api/v4/memory."""
    body = {
        "title": kw.get("title", "Test Memory"),
        "memory_text": kw.get("memory_text", "Default content."),
        "project_id": str(kw.get("project_id", TEST_PROJECT_ID)),
        "sensitivity_level": kw.get("sensitivity_level", "private"),
    }
    if "canonical_key" in kw:
        body["canonical_key"] = kw["canonical_key"]
    return _assert_ok(
        client.post("/api/v4/memory", json=body, headers=_idem_headers()), 201
    )


def _activate_memory(client, *, candidate_id=None, memory_text=None,
                     review_item_id=None, project_id=None):
    """Activate memory via POST /api/v4/memory/activate."""
    return _assert_ok(
        client.post(
            "/api/v4/memory/activate",
            json={
                "candidate_id": str(candidate_id or TEST_CANDIDATE_ID),
                "project_id": str(project_id or TEST_PROJECT_ID),
                "title": "Activated Memory",
                "memory_text": memory_text or "Memory from candidate.",
                "sensitivity_level": "normal",
                "review_item_id": str(review_item_id or TEST_REVIEW_ITEM_ID),
            },
            headers={"Idempotency-Key": str(uuid4())},
        ),
        201,
    )


def _patch_memory(client, memory_id, *, title=None, memory_text=None):
    """Update memory via PATCH /api/v4/memory/{id}."""
    body = {}
    if title is not None:
        body["title"] = title
    if memory_text is not None:
        body["memory_text"] = memory_text
    if not body:
        return None
    return _assert_ok(
        client.patch(
            f"/api/v4/memory/{memory_id}", json=body, headers=_idem_headers()
        )
    )


def _expire_memory(client, memory_id):
    """Expire memory via POST /api/v4/memory/{id}/expire."""
    return _assert_ok(
        client.post(
            f"/api/v4/memory/{memory_id}/expire",
            json={},
            headers=_idem_headers(),
        )
    )


def _restore_memory(client, memory_id):
    """Restore memory via POST /api/v4/memory/{id}/restore."""
    return _assert_ok(
        client.post(
            f"/api/v4/memory/{memory_id}/restore",
            json={},
            headers=_idem_headers(),
        )
    )


def _merge_memory(client, memory_id, target_memory_id):
    """Merge via POST /api/v4/memory/{id}/merge."""
    return _assert_ok(
        client.post(
            f"/api/v4/memory/{memory_id}/merge",
            json={"target_memory_id": str(target_memory_id), "reason": "test merge"},
            headers=_idem_headers(),
        )
    )


def _delete_memory(client, memory_id):
    """Soft-delete via DELETE /api/v4/memory/{id}."""
    return _assert_ok(
        client.delete(
            f"/api/v4/memory/{memory_id}",
            headers=_idem_headers(),
        )
    )


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
        # Memory index entries
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_index_entries (
                memory_index_entry_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL, memory_version INTEGER NOT NULL,
                project_id TEXT, index_profile TEXT NOT NULL DEFAULT 'default',
                embedding_model_id TEXT, content_hash TEXT NOT NULL,
                index_text TEXT NOT NULL,
                fts_state TEXT NOT NULL DEFAULT 'pending',
                vector_state TEXT NOT NULL DEFAULT 'pending',
                ready_at TEXT, stale_at TEXT, last_error TEXT,
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
            "pid": TEST_PROJECT_ID.hex,
            "pcode": TEST_PROJECT_CODE,
            "pname": "P4-06 Test Project",
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
                    :chash, 'approved')
        """), {
            "cid": TEST_CANDIDATE_ID.hex, "pid": TEST_PROJECT_ID.hex,
            "chash": "dummy-candidate-hash-for-activation",
        })
        db.commit()


# ═══════════════════════════════════════════════════════════════════════
# P4-06 — Memory Versions
# ═══════════════════════════════════════════════════════════════════════


class TestListMemoryVersions:
    """GET /api/v4/memory/{memory_id}/versions — version history listing."""

    def test_list_versions_empty_for_draft(self, client):
        """A newly created draft memory has exactly 1 version (create)."""
        c, _engine = client
        mem = _create_memory(c, title="V1 Draft", memory_text="Draft content.")

        resp = c.get(f"/api/v4/memory/{mem['memory_id']}/versions")
        data = _assert_ok(resp)
        items = data["items"]
        assert len(items) == 1
        assert items[0]["version"] == 1
        assert items[0]["action"] == "create"
        assert items[0]["after_json"]["status"] == "draft"

    def test_list_versions_after_multiple_operations(self, client):
        """After multiple lifecycle ops, all versions are listed."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Initial content.")

        mid = mem["memory_id"]
        _patch_memory(c, mid, title="Updated Title")
        _patch_memory(c, mid, memory_text="Updated content v2.")
        _expire_memory(c, mid)

        resp = c.get(f"/api/v4/memory/{mid}/versions")
        data = _assert_ok(resp)
        items = data["items"]

        # create(v1) + update(v2) + update(v3) + expire(v4) = 4 versions
        assert len(items) == 4
        actions = [item["action"] for item in items]
        assert "create" in actions
        assert "update" in actions
        assert "expire" in actions

    def test_list_versions_pagination(self, client):
        """Versions support pagination."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Base content.")
        mid = mem["memory_id"]

        # Perform several updates to get multiple versions
        for i in range(5):
            _patch_memory(c, mid, memory_text=f"Update iteration {i}.")

        # Page 1, size 3
        resp = c.get(
            f"/api/v4/memory/{mid}/versions?page_size=3&page=1"
        )
        data = _assert_ok(resp)
        assert len(data["items"]) == 3
        assert data["page_info"]["total_items"] >= 6  # 1 create + 5 updates
        assert data["page_info"]["has_next"] is True

        # Page 2, size 3
        resp2 = c.get(
            f"/api/v4/memory/{mid}/versions?page_size=3&page=2"
        )
        data2 = _assert_ok(resp2)
        assert len(data2["items"]) >= 3

    def test_list_versions_filter_by_action(self, client):
        """Filter version history by action type."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Action filter test.")
        mid = mem["memory_id"]

        _patch_memory(c, mid, title="Modified")
        _expire_memory(c, mid)

        # Filter to "create" only
        resp = c.get(
            f"/api/v4/memory/{mid}/versions?action=create"
        )
        data = _assert_ok(resp)
        assert len(data["items"]) == 1
        assert data["items"][0]["action"] == "create"

        # Filter to "expire" only
        resp2 = c.get(
            f"/api/v4/memory/{mid}/versions?action=expire"
        )
        data2 = _assert_ok(resp2)
        assert len(data2["items"]) == 1
        assert data2["items"][0]["action"] == "expire"

    def test_list_versions_ordered_newest_first(self, client):
        """Versions are listed newest first (DESC)."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Order test.")
        mid = mem["memory_id"]

        _patch_memory(c, mid, title="V2")
        _patch_memory(c, mid, title="V3")

        resp = c.get(f"/api/v4/memory/{mid}/versions")
        data = _assert_ok(resp)
        items = data["items"]
        # First item should be highest version
        assert items[0]["version"] >= items[1]["version"]

    def test_list_versions_memory_not_found(self, client):
        """Requesting versions for non-existent memory returns 404."""
        c, _engine = client
        fake_id = str(uuid4())
        resp = c.get(f"/api/v4/memory/{fake_id}/versions")
        assert resp.status_code == 404

    def test_list_versions_continuity_no_gaps(self, client):
        """Version numbers are contiguous with no gaps."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Continuity test.")
        mid = mem["memory_id"]

        _patch_memory(c, mid, title="V2")
        _patch_memory(c, mid, title="V3")
        _expire_memory(c, mid)

        resp = c.get(f"/api/v4/memory/{mid}/versions")
        data = _assert_ok(resp)
        items = data["items"]

        versions = sorted([item["version"] for item in items])
        assert versions == [1, 2, 3, 4]
        # No gaps: max version equals count
        assert max(versions) == len(versions)


class TestGetMemoryVersion:
    """GET /api/v4/memory/{memory_id}/versions/{version} — specific version."""

    def test_get_specific_version(self, client):
        """Retrieve a specific version by number."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Specific version test.")
        mid = mem["memory_id"]

        resp = c.get(f"/api/v4/memory/{mid}/versions/1")
        data = _assert_ok(resp)
        assert data["version"] == 1
        assert data["action"] == "create"
        assert data["memory_id"] == mid
        assert data["after_json"]["memory_text"] == "Specific version test."
        assert data["before_json"] == {}

    def test_get_version_with_before_after_diff(self, client):
        """Version 2+ has meaningful before_json and after_json."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Original.")
        mid = mem["memory_id"]

        _patch_memory(c, mid, memory_text="Modified content.")

        # Version 1: create
        resp1 = c.get(f"/api/v4/memory/{mid}/versions/1")
        data1 = _assert_ok(resp1)
        assert data1["before_json"] == {}
        assert data1["after_json"]["memory_text"] == "Original."

        # Version 2: update
        resp2 = c.get(f"/api/v4/memory/{mid}/versions/2")
        data2 = _assert_ok(resp2)
        assert data2["before_json"]["memory_text"] == "Original."
        assert data2["after_json"]["memory_text"] == "Modified content."

    def test_get_version_non_existent(self, client):
        """Requesting a non-existent version returns 404."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Only v1.")
        mid = mem["memory_id"]

        resp = c.get(f"/api/v4/memory/{mid}/versions/999")
        assert resp.status_code == 404

    def test_get_version_memory_not_found(self, client):
        """Requesting version for non-existent memory returns 404."""
        c, _engine = client
        fake_id = str(uuid4())
        resp = c.get(f"/api/v4/memory/{fake_id}/versions/1")
        assert resp.status_code == 404

    def test_version_preserves_lifecycle_status(self, client):
        """Each lifecycle transition is recorded with correct status in after_json."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Lifecycle tracking.")
        mid = mem["memory_id"]

        # After activation, v1 should be active
        v1 = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions/1"))
        assert v1["after_json"]["status"] == "active"

        # Expire → new version with status expired
        _expire_memory(c, mid)
        resp = c.get(f"/api/v4/memory/{mid}/versions")
        versions = _assert_ok(resp)["items"]
        latest = versions[0]  # newest first
        assert latest["action"] == "expire"
        assert latest["after_json"]["status"] == "expired"
        assert latest["before_json"]["status"] == "active"

    def test_version_has_actor_info(self, client):
        """Version records include actor_type and created_at."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Actor tracking.")
        mid = mem["memory_id"]

        v1 = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions/1"))
        assert v1["actor_type"] is not None
        assert v1["created_at"] is not None


# ═══════════════════════════════════════════════════════════════════════
# P4-06 — Memory Sources
# ═══════════════════════════════════════════════════════════════════════


class TestListMemorySources:
    """GET /api/v4/memory/{memory_id}/sources — source listing."""

    def test_list_sources_after_activation(self, client):
        """After activation, an 'origin' source is automatically created."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Source tracking.")
        mid = mem["memory_id"]

        resp = c.get(f"/api/v4/memory/{mid}/sources")
        data = _assert_ok(resp)
        items = data["items"]
        assert len(items) == 1
        assert items[0]["source_role"] == "origin"
        assert items[0]["candidate_id"] == str(TEST_CANDIDATE_ID)
        assert items[0]["memory_version"] == 1

    def test_list_sources_memory_not_found(self, client):
        """Requesting sources for non-existent memory returns 404."""
        c, _engine = client
        fake_id = str(uuid4())
        resp = c.get(f"/api/v4/memory/{fake_id}/sources")
        assert resp.status_code == 404


class TestAddMemorySource:
    """POST /api/v4/memory/{memory_id}/sources — add source link."""

    def test_add_source_with_candidate_id(self, client):
        """Add a source link referencing a candidate."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Source addition test.")
        mid = mem["memory_id"]

        new_candidate = uuid4()
        # Insert a new candidate for referencing
        with Session(_engine) as db:
            db.execute(text("""
                INSERT INTO memory_candidates (candidate_id, project_id, source_type,
                    submitted_by_actor_type, candidate_text, candidate_hash, candidate_status)
                VALUES (:cid, :pid, 'manual', 'user', 'Evidence candidate', :chash, 'approved')
            """), {
                "cid": new_candidate.hex, "pid": TEST_PROJECT_ID.hex,
                "chash": f"hash-{new_candidate.hex[:16]}",
            })
            db.commit()

        resp = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "candidate_id": str(new_candidate),
                "source_role": "supporting",
            },
            headers=_idem_headers(),
        )
        data = _assert_ok(resp, 201)
        assert data["candidate_id"] == str(new_candidate)
        assert data["source_role"] == "supporting"
        assert data["memory_version"] == 1

    def test_add_source_with_source_span(self, client):
        """Add a source with source_span metadata."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Span test.")
        mid = mem["memory_id"]

        span = {"span_start": 10, "span_end": 50, "text_snippet": "excerpt"}
        resp = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "candidate_id": str(TEST_CANDIDATE_ID),
                "source_role": "evidence",
                "source_span": span,
                "confidence": 0.95,
            },
            headers=_idem_headers(),
        )
        data = _assert_ok(resp, 201)
        assert data["source_span"] == span
        assert data["confidence"] == 0.95

    def test_add_source_with_confidence_boundaries(self, client):
        """Confidence score accepts 0.0 and 1.0."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Confidence bounds.")
        mid = mem["memory_id"]

        # confidence = 0.0
        resp = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "candidate_id": str(TEST_CANDIDATE_ID),
                "confidence": 0.0,
            },
            headers=_idem_headers(),
        )
        assert resp.status_code == 201

        # confidence = 1.0
        resp2 = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "candidate_id": str(TEST_CANDIDATE_ID),
                "confidence": 1.0,
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert resp2.status_code == 201

    def test_add_source_missing_reference(self, client):
        """POST without any source reference returns 400."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Missing ref test.")
        mid = mem["memory_id"]

        resp = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={"memory_version": 1, "source_role": "evidence"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 400

    def test_add_source_memory_not_found(self, client):
        """Adding source to non-existent memory returns 404."""
        c, _engine = client
        fake_id = str(uuid4())
        resp = c.post(
            f"/api/v4/memory/{fake_id}/sources",
            json={
                "memory_version": 1,
                "candidate_id": str(TEST_CANDIDATE_ID),
            },
            headers=_idem_headers(),
        )
        assert resp.status_code == 404

    def test_add_source_all_role_types(self, client):
        """All SourceRole enum values are accepted."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Role test.")
        mid = mem["memory_id"]

        roles = ["evidence", "origin", "supporting", "conflict", "supersedes"]
        for i, role in enumerate(roles):
            resp = c.post(
                f"/api/v4/memory/{mid}/sources",
                json={
                    "memory_version": 1,
                    "candidate_id": str(TEST_CANDIDATE_ID),
                    "source_role": role,
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            data = _assert_ok(resp, 201)
            assert data["source_role"] == role

    def test_add_source_multiple_source_types(self, client):
        """Sources can reference different types (message_id, raw_event_id, etc.)."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Multi-type source test.")
        mid = mem["memory_id"]

        # Add source with message_id-like UUID
        msg_id = uuid4()
        resp = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "message_id": str(msg_id),
                "source_role": "evidence",
            },
            headers=_idem_headers(),
        )
        data = _assert_ok(resp, 201)
        assert data["message_id"] == str(msg_id)

        # Add source with raw_event_id-like UUID
        evt_id = uuid4()
        resp2 = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "raw_event_id": str(evt_id),
                "source_role": "supporting",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        data2 = _assert_ok(resp2, 201)
        assert data2["raw_event_id"] == str(evt_id)


class TestRemoveMemorySource:
    """DELETE /api/v4/memory/sources/{memory_source_id} — remove source."""

    def test_remove_source_success(self, client):
        """Remove an existing source link."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Source removal test.")
        mid = mem["memory_id"]

        # First add a source
        add_resp = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "candidate_id": str(TEST_CANDIDATE_ID),
                "source_role": "evidence",
            },
            headers=_idem_headers(),
        )
        added = _assert_ok(add_resp, 201)
        source_id = added["memory_source_id"]

        # Now remove it
        del_resp = c.delete(
            f"/api/v4/memory/sources/{source_id}",
            headers=_idem_headers(),
        )
        data = _assert_ok(del_resp)
        assert data["deleted"] is True

        # Verify it's gone from the list
        list_resp = c.get(f"/api/v4/memory/{mid}/sources")
        list_data = _assert_ok(list_resp)
        # Only the origin source from activation remains
        assert all(s["memory_source_id"] != source_id for s in list_data["items"])

    def test_remove_source_not_found(self, client):
        """Removing a non-existent source returns 404."""
        c, _engine = client
        fake_id = str(uuid4())
        resp = c.delete(
            f"/api/v4/memory/sources/{fake_id}",
            headers=_idem_headers(),
        )
        assert resp.status_code == 404

    def test_remove_source_twice(self, client):
        """Removing an already-removed source returns 404."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Double delete test.")
        mid = mem["memory_id"]

        # Add source
        add_resp = c.post(
            f"/api/v4/memory/{mid}/sources",
            json={
                "memory_version": 1,
                "candidate_id": str(TEST_CANDIDATE_ID),
                "source_role": "evidence",
            },
            headers=_idem_headers(),
        )
        source_id = _assert_ok(add_resp, 201)["memory_source_id"]

        # Remove once
        c.delete(f"/api/v4/memory/sources/{source_id}", headers=_idem_headers())

        # Remove again — should be 404
        resp2 = c.delete(
            f"/api/v4/memory/sources/{source_id}",
            headers=_idem_headers(),
        )
        assert resp2.status_code == 404


class TestMemoryLifecycleVersionRecording:
    """Verify that each lifecycle operation correctly records versions."""

    def test_create_records_version_1(self, client):
        """create_memory records version=1 with action='create'."""
        c, _engine = client
        mem = _create_memory(c, title="Version Check", memory_text="Draft.")

        versions = _assert_ok(c.get(f"/api/v4/memory/{mem['memory_id']}/versions"))
        items = versions["items"]
        assert len(items) == 1
        assert items[0]["version"] == 1
        assert items[0]["action"] == "create"

    def test_activate_records_version_1_create(self, client):
        """Activation records version=1 with action='create'."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Activated.")
        mid = mem["memory_id"]

        versions = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions"))
        items = versions["items"]
        assert len(items) == 1
        assert items[0]["version"] == 1
        assert items[0]["action"] == "create"

    def test_update_increments_version(self, client):
        """Each update increments the version number."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="v1 content.")
        mid = mem["memory_id"]

        assert mem["current_version"] == 1

        # First update
        mem2 = _patch_memory(c, mid, memory_text="v2 content.")
        assert mem2["current_version"] == 2

        # Second update
        mem3 = _patch_memory(c, mid, title="New title v3")
        assert mem3["current_version"] == 3

        versions = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions"))
        assert len(versions["items"]) == 3

    def test_expire_increments_version(self, client):
        """Expire creates a new version."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="To expire.")
        mid = mem["memory_id"]

        assert mem["current_version"] == 1
        mem2 = _expire_memory(c, mid)
        assert mem2["current_version"] == 2

        versions = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions"))
        actions = [v["action"] for v in versions["items"]]
        assert "expire" in actions

    def test_restore_increments_version(self, client):
        """Restore from expired creates a new version."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="To restore.")
        mid = mem["memory_id"]

        _expire_memory(c, mid)
        restored = _restore_memory(c, mid)
        assert restored["current_version"] == 3  # create + expire + restore

        versions = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions"))
        actions = [v["action"] for v in versions["items"]]
        assert "restore" in actions

    def test_delete_increments_version(self, client):
        """Soft delete creates a new version."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="To delete.")
        mid = mem["memory_id"]

        del_result = _delete_memory(c, mid)
        # After delete, get the memory to check version
        mem_detail = _assert_ok(c.get(f"/api/v4/memory/{mid}"))
        assert mem_detail["current_version"] >= 2

        versions = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions"))
        actions = [v["action"] for v in versions["items"]]
        assert "delete" in actions

    def test_merge_records_versions_for_both(self, client):
        """Merge records version for both survivor and consumed."""
        c, _engine = client
        survivor = _activate_memory(c, memory_text="Survivor content.")
        consumed = _activate_memory(
            c,
            memory_text="Consumed content.",
            candidate_id=uuid4(),
            review_item_id=uuid4(),
        )

        sv_id = survivor["memory_id"]
        co_id = consumed["memory_id"]

        _merge_memory(c, sv_id, co_id)

        # Survivor should have version 2 (create + merge)
        sv_versions = _assert_ok(c.get(f"/api/v4/memory/{sv_id}/versions"))
        sv_actions = [v["action"] for v in sv_versions["items"]]
        assert "merge" in sv_actions

        # Consumed should have version 2 (create + merge)
        co_versions = _assert_ok(c.get(f"/api/v4/memory/{co_id}/versions"))
        co_actions = [v["action"] for v in co_versions["items"]]
        assert "merge" in co_actions

    def test_full_lifecycle_version_chain(self, client):
        """Complete lifecycle creates versions: create→update→expire→restore→delete."""
        c, _engine = client
        mem = _activate_memory(c, memory_text="Full lifecycle.")
        mid = mem["memory_id"]

        _patch_memory(c, mid, title="Updated")
        _expire_memory(c, mid)
        _restore_memory(c, mid)
        _delete_memory(c, mid)

        versions = _assert_ok(c.get(f"/api/v4/memory/{mid}/versions"))
        actions = [v["action"] for v in versions["items"]]
        assert "create" in actions
        assert "update" in actions
        assert "expire" in actions
        assert "restore" in actions
        assert "delete" in actions
        assert len(actions) == 5
