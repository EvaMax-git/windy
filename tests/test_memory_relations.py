"""P4-08 Memory Relations contract tests.

Tests all 6 API endpoints:
* POST   /api/v4/memory/relations                        — create relation
* GET    /api/v4/memory/{memory_id}/relations             — list relations
* GET    /api/v4/memory/relations/{memory_relation_id}    — relation detail
* PATCH  /api/v4/memory/relations/{memory_relation_id}    — update relation
* POST   /api/v4/memory/relations/{id}/resolve           — mark resolved
* POST   /api/v4/memory/relations/{id}/cancel            — cancel relation

Also validates:
- UNIQUE(from_memory_id, to_memory_id, relation_type) constraint
- CHECK(from_memory_id <> to_memory_id) constraint
- State machine: active → resolved / cancelled
- Merge auto-creates merged_into relation
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
def rel_client(monkeypatch):
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


def _create_relation(client, *, from_memory_id, to_memory_id,
                     relation_type="supports", reason=None,
                     created_by_review_item_id=None, metadata_json=None,
                     expect_status=201):
    body = {
        "from_memory_id": str(from_memory_id),
        "to_memory_id": str(to_memory_id),
        "relation_type": relation_type,
    }
    if reason:
        body["reason"] = reason
    if created_by_review_item_id:
        body["created_by_review_item_id"] = str(created_by_review_item_id)
    if metadata_json:
        body["metadata_json"] = metadata_json

    resp = client.post("/api/v4/memory/relations", json=body, headers=_idem_headers())
    if expect_status >= 400:
        return resp
    return _assert_ok(resp, expect_status)


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
                UNIQUE (from_memory_id, to_memory_id, relation_type),
                CHECK (from_memory_id <> to_memory_id)
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
# Tests — Create Relation
# ═══════════════════════════════════════════════════════════════════════


def test_create_relation_supports(rel_client):
    """Create a supports relation between two memories."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="Memory A", memory_text="Content A")
    mem_b = _create_memory(client, title="Memory B", memory_text="Content B")

    data = _create_relation(
        client,
        from_memory_id=mem_a["memory_id"],
        to_memory_id=mem_b["memory_id"],
        relation_type="supports",
        reason="A supports B",
    )
    assert data["from_memory_id"] == mem_a["memory_id"]
    assert data["to_memory_id"] == mem_b["memory_id"]
    assert data["relation_type"] == "supports"
    assert data["relation_status"] == "active"
    assert data["reason"] == "A supports B"
    assert data["memory_relation_id"] is not None
    assert data["created_at"] is not None


def test_create_relation_conflicts_with(rel_client):
    """Create a conflicts_with relation."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="Earth is round")
    mem_b = _create_memory(client, title="B", memory_text="Earth is flat")

    data = _create_relation(
        client,
        from_memory_id=mem_a["memory_id"],
        to_memory_id=mem_b["memory_id"],
        relation_type="conflicts_with",
    )
    assert data["relation_type"] == "conflicts_with"
    assert data["relation_status"] == "active"


def test_create_relation_all_types(rel_client):
    """All five relation types can be created."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    for rtype in ("conflicts_with", "supersedes", "merged_into", "duplicates", "supports"):
        mem_x = _create_memory(client, title=f"X-{rtype}", memory_text="X")
        mem_y = _create_memory(client, title=f"Y-{rtype}", memory_text="Y")
        data = _create_relation(
            client,
            from_memory_id=mem_x["memory_id"],
            to_memory_id=mem_y["memory_id"],
            relation_type=rtype,
        )
        assert data["relation_type"] == rtype


def test_create_relation_missing_idempotency(rel_client):
    """Missing Idempotency-Key returns 400."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    resp = client.post("/api/v4/memory/relations", json={
        "from_memory_id": mem_a["memory_id"],
        "to_memory_id": mem_b["memory_id"],
        "relation_type": "supports",
    })
    assert resp.status_code == 400


def test_create_relation_duplicate_rejected(rel_client):
    """UNIQUE(from_memory_id, to_memory_id, relation_type) rejects duplicates."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    _create_relation(
        client,
        from_memory_id=mem_a["memory_id"],
        to_memory_id=mem_b["memory_id"],
        relation_type="supports",
    )

    resp = _create_relation(
        client,
        from_memory_id=mem_a["memory_id"],
        to_memory_id=mem_b["memory_id"],
        relation_type="supports",
        expect_status=409,
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert "already exists" in err["message"] or "unique" in err["message"].lower()


def test_create_relation_self_reference_rejected(rel_client):
    """CHECK(from_memory_id <> to_memory_id) rejects self-reference."""
    client, _engine = rel_client
    mem = _create_memory(client, title="Self", memory_text="Self")

    resp = _create_relation(
        client,
        from_memory_id=mem["memory_id"],
        to_memory_id=mem["memory_id"],
        relation_type="supports",
        expect_status=400,
    )
    # The DB layer raises ValueError for self-reference, API returns 400 or 409
    assert resp.status_code in (400, 409)


def test_create_relation_different_type_same_pair_ok(rel_client):
    """Different relation types between the same memory pair are allowed."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    d1 = _create_relation(client, from_memory_id=mem_a["memory_id"],
                          to_memory_id=mem_b["memory_id"], relation_type="supports")
    d2 = _create_relation(client, from_memory_id=mem_a["memory_id"],
                          to_memory_id=mem_b["memory_id"], relation_type="duplicates")

    assert d1["memory_relation_id"] != d2["memory_relation_id"]
    assert d1["relation_type"] == "supports"
    assert d2["relation_type"] == "duplicates"


def test_create_relation_from_nonexistent_memory(rel_client):
    """Non-existent from_memory_id returns 400."""
    client, _engine = rel_client
    mem_b = _create_memory(client, title="B", memory_text="B")

    resp = _create_relation(
        client,
        from_memory_id=uuid4(),
        to_memory_id=mem_b["memory_id"],
        relation_type="supports",
        expect_status=400,
    )
    assert resp.status_code in (400, 404)


def test_create_relation_to_nonexistent_memory(rel_client):
    """Non-existent to_memory_id returns 400."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")

    resp = _create_relation(
        client,
        from_memory_id=mem_a["memory_id"],
        to_memory_id=uuid4(),
        relation_type="supports",
        expect_status=400,
    )
    assert resp.status_code in (400, 404)


def test_create_relation_with_metadata(rel_client):
    """Create a relation with metadata_json."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    data = _create_relation(
        client,
        from_memory_id=mem_a["memory_id"],
        to_memory_id=mem_b["memory_id"],
        relation_type="supports",
        metadata_json={"confidence": 0.95, "source": "manual"},
    )
    assert data["metadata_json"] == {"confidence": 0.95, "source": "manual"}


def test_create_relation_with_review_item(rel_client):
    """Create a relation with created_by_review_item_id."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    data = _create_relation(
        client,
        from_memory_id=mem_a["memory_id"],
        to_memory_id=mem_b["memory_id"],
        relation_type="supersedes",
        created_by_review_item_id=TEST_REVIEW_ITEM_ID,
    )
    assert data["created_by_review_item_id"] == str(TEST_REVIEW_ITEM_ID)


# ═══════════════════════════════════════════════════════════════════════
# Tests — List Relations
# ═══════════════════════════════════════════════════════════════════════


def test_list_relations_for_memory(rel_client):
    """List all relations involving a memory (both directions)."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="Hub", memory_text="Hub")
    mem_b = _create_memory(client, title="Spoke1", memory_text="S1")
    mem_c = _create_memory(client, title="Spoke2", memory_text="S2")

    _create_relation(client, from_memory_id=mem_a["memory_id"],
                     to_memory_id=mem_b["memory_id"], relation_type="supports")
    _create_relation(client, from_memory_id=mem_c["memory_id"],
                     to_memory_id=mem_a["memory_id"], relation_type="conflicts_with")

    resp = client.get(f"/api/v4/memory/{mem_a['memory_id']}/relations")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] == 2
    assert len(data["items"]) == 2

    types = {r["relation_type"] for r in data["items"]}
    assert "supports" in types
    assert "conflicts_with" in types


def test_list_relations_empty(rel_client):
    """Memory with no relations returns empty list."""
    client, _engine = rel_client
    mem = _create_memory(client, title="Lonely", memory_text="Lonely")

    resp = client.get(f"/api/v4/memory/{mem['memory_id']}/relations")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] == 0
    assert data["items"] == []


def test_list_relations_pagination(rel_client):
    """List relations respects pagination."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="Pag Hub", memory_text="PH")

    for i in range(5):
        mem_x = _create_memory(client, title=f"Pag-{i}", memory_text=f"P{i}")
        _create_relation(client, from_memory_id=mem_a["memory_id"],
                         to_memory_id=mem_x["memory_id"], relation_type="supports")

    resp = client.get(f"/api/v4/memory/{mem_a['memory_id']}/relations?page=1&page_size=2")
    data = _assert_ok(resp)
    assert data["page_info"]["total_items"] == 5
    assert data["page_info"]["total_pages"] == 3
    assert len(data["items"]) == 2
    assert data["page_info"]["has_next"] is True


# ═══════════════════════════════════════════════════════════════════════
# Tests — Get Relation
# ═══════════════════════════════════════════════════════════════════════


def test_get_relation(rel_client):
    """Get a single relation by ID."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports", reason="Test reason")

    resp = client.get(f"/api/v4/memory/relations/{created['memory_relation_id']}")
    data = _assert_ok(resp)
    assert data["memory_relation_id"] == created["memory_relation_id"]
    assert data["from_memory_id"] == mem_a["memory_id"]
    assert data["to_memory_id"] == mem_b["memory_id"]
    assert data["relation_type"] == "supports"
    assert data["reason"] == "Test reason"


def test_get_relation_not_found(rel_client):
    """Non-existent relation returns 404."""
    client, _engine = rel_client

    resp = client.get(f"/api/v4/memory/relations/{uuid4()}")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# Tests — Update Relation
# ═══════════════════════════════════════════════════════════════════════


def test_update_relation_reason(rel_client):
    """Update the reason field of a relation."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports", reason="Old reason")

    resp = client.patch(
        f"/api/v4/memory/relations/{created['memory_relation_id']}",
        json={"reason": "Updated reason"},
        headers=_idem_headers(),
    )
    data = _assert_ok(resp)
    assert data["reason"] == "Updated reason"
    assert data["memory_relation_id"] == created["memory_relation_id"]


def test_update_relation_metadata(rel_client):
    """Update the metadata_json field of a relation."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports",
                               metadata_json={"k": "v"})

    resp = client.patch(
        f"/api/v4/memory/relations/{created['memory_relation_id']}",
        json={"metadata_json": {"new_key": "new_val"}},
        headers=_idem_headers(),
    )
    data = _assert_ok(resp)
    assert data["metadata_json"] == {"new_key": "new_val"}


def test_update_relation_missing_idempotency(rel_client):
    """Missing Idempotency-Key on update returns 400."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports")

    resp = client.patch(
        f"/api/v4/memory/relations/{created['memory_relation_id']}",
        json={"reason": "No key"},
    )
    assert resp.status_code == 400


def test_update_relation_not_found(rel_client):
    """Updating non-existent relation returns 404."""
    client, _engine = rel_client

    resp = client.patch(
        f"/api/v4/memory/relations/{uuid4()}",
        json={"reason": "Ghost"},
        headers=_idem_headers(),
    )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# Tests — Resolve Relation
# ═══════════════════════════════════════════════════════════════════════


def test_resolve_relation(rel_client):
    """Resolve an active relation (active → resolved)."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="conflicts_with")

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/resolve",
        headers=_idem_headers(),
    )
    data = _assert_ok(resp)
    assert data["relation_status"] == "resolved"
    assert data["memory_relation_id"] == created["memory_relation_id"]

    # Verify via GET
    get_resp = client.get(f"/api/v4/memory/relations/{created['memory_relation_id']}")
    get_data = _assert_ok(get_resp)
    assert get_data["relation_status"] == "resolved"


def test_resolve_already_resolved_fails(rel_client):
    """Cannot resolve an already resolved relation."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports")

    client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/resolve",
        headers=_idem_headers(),
    )

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/resolve",
        headers=_idem_headers(),
    )
    assert resp.status_code == 409


def test_resolve_cancelled_fails(rel_client):
    """Cannot resolve a cancelled relation."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports")

    client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/cancel",
        headers=_idem_headers(),
    )

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/resolve",
        headers=_idem_headers(),
    )
    assert resp.status_code == 409


def test_resolve_not_found(rel_client):
    """Resolving non-existent relation returns 404."""
    client, _engine = rel_client

    resp = client.post(
        f"/api/v4/memory/relations/{uuid4()}/resolve",
        headers=_idem_headers(),
    )
    assert resp.status_code == 404


def test_resolve_missing_idempotency(rel_client):
    """Missing Idempotency-Key on resolve returns 400."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports")

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/resolve",
    )
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# Tests — Cancel Relation
# ═══════════════════════════════════════════════════════════════════════


def test_cancel_relation(rel_client):
    """Cancel an active relation (active → cancelled)."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="duplicates")

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/cancel",
        headers=_idem_headers(),
    )
    data = _assert_ok(resp)
    assert data["relation_status"] == "cancelled"

    # Verify via GET
    get_resp = client.get(f"/api/v4/memory/relations/{created['memory_relation_id']}")
    get_data = _assert_ok(get_resp)
    assert get_data["relation_status"] == "cancelled"


def test_cancel_already_cancelled_fails(rel_client):
    """Cannot cancel an already cancelled relation."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports")

    client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/cancel",
        headers=_idem_headers(),
    )

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/cancel",
        headers=_idem_headers(),
    )
    assert resp.status_code == 409


def test_cancel_resolved_fails(rel_client):
    """Cannot cancel a resolved relation."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports")

    client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/resolve",
        headers=_idem_headers(),
    )

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/cancel",
        headers=_idem_headers(),
    )
    assert resp.status_code == 409


def test_cancel_not_found(rel_client):
    """Cancelling non-existent relation returns 404."""
    client, _engine = rel_client

    resp = client.post(
        f"/api/v4/memory/relations/{uuid4()}/cancel",
        headers=_idem_headers(),
    )
    assert resp.status_code == 404


def test_cancel_missing_idempotency(rel_client):
    """Missing Idempotency-Key on cancel returns 400."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    created = _create_relation(client, from_memory_id=mem_a["memory_id"],
                               to_memory_id=mem_b["memory_id"],
                               relation_type="supports")

    resp = client.post(
        f"/api/v4/memory/relations/{created['memory_relation_id']}/cancel",
    )
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# Tests — Merge auto-creates relation
# ═══════════════════════════════════════════════════════════════════════


def test_merge_auto_creates_merged_into_relation(rel_client):
    """Merge operation automatically creates merged_into relation."""
    client, engine = rel_client
    mem_a = _create_memory(client, title="Survivor", memory_text="Text A")
    mem_b = _create_memory(client, title="Consumed", memory_text="Text B")

    resp = client.post(f"/api/v4/memory/{mem_a['memory_id']}/merge", json={
        "target_memory_id": mem_b["memory_id"],
        "reason": "Dedup merge",
    }, headers=_idem_headers())
    merge_data = _assert_ok(resp)
    assert "Text A" in merge_data["memory_text"]
    assert "Text B" in merge_data["memory_text"]

    # Verify merged_into relation exists from consumed → survivor
    rel_resp = client.get(f"/api/v4/memory/{mem_b['memory_id']}/relations")
    rel_data = _assert_ok(rel_resp)
    assert rel_data["page_info"]["total_items"] >= 1

    merged_relations = [
        r for r in rel_data["items"] if r["relation_type"] == "merged_into"
    ]
    assert len(merged_relations) >= 1
    rel = merged_relations[0]
    assert rel["from_memory_id"] == mem_b["memory_id"]
    assert rel["to_memory_id"] == mem_a["memory_id"]
    assert rel["reason"] == "Dedup merge"


# ═══════════════════════════════════════════════════════════════════════
# Tests — Idempotency
# ═══════════════════════════════════════════════════════════════════════


def test_create_relation_idempotent(rel_client):
    """Same idempotency key returns existing relation (or 409 for conflict)."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    ikey = str(uuid4())
    headers = {"Idempotency-Key": ikey}
    body = {
        "from_memory_id": mem_a["memory_id"],
        "to_memory_id": mem_b["memory_id"],
        "relation_type": "supports",
    }

    resp1 = client.post("/api/v4/memory/relations", json=body, headers=headers)
    assert resp1.status_code == 201

    resp2 = client.post("/api/v4/memory/relations", json=body, headers=headers)
    # Idempotent replay should either return 201 (existing) or 409 (duplicate)
    assert resp2.status_code in (201, 409)
    if resp2.status_code == 201:
        d1 = resp1.json()["data"]
        d2 = resp2.json()["data"]
        assert d1["memory_relation_id"] == d2["memory_relation_id"]


# ═══════════════════════════════════════════════════════════════════════
# Tests — Direction sensitivity
# ═══════════════════════════════════════════════════════════════════════


def test_relation_direction_matters(rel_client):
    """A→B supports does not conflict with B→A supports."""
    client, _engine = rel_client
    mem_a = _create_memory(client, title="A", memory_text="A")
    mem_b = _create_memory(client, title="B", memory_text="B")

    d1 = _create_relation(client, from_memory_id=mem_a["memory_id"],
                          to_memory_id=mem_b["memory_id"], relation_type="supports")
    d2 = _create_relation(client, from_memory_id=mem_b["memory_id"],
                          to_memory_id=mem_a["memory_id"], relation_type="supports")

    assert d1["memory_relation_id"] != d2["memory_relation_id"]
    assert d1["from_memory_id"] == mem_a["memory_id"]
    assert d1["to_memory_id"] == mem_b["memory_id"]
    assert d2["from_memory_id"] == mem_b["memory_id"]
    assert d2["to_memory_id"] == mem_a["memory_id"]
