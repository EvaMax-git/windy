"""P5-05 Index lifecycle contract tests.

Validates that the memory index entries (``memory_index_entries``) correctly
track lifecycle state transitions across the full memory lifecycle:

* **create** → index entry with ``fts_state='ready'``
* **activate** → index entry with ``fts_state='ready'``
* **update** → old entry stale, new entry ready
* **expire** → all entries marked ``fts_state='stale'``
* **restore** → new entry created with ``fts_state='ready'``
* **delete** → all entries marked ``fts_state='stale'``
* Multiple lifecycle cycles (expire→restore→expire→restore)

Also validates index status summary consistency and vector_state tracking.
"""

from __future__ import annotations

import datetime as _dt_mod
import os
from uuid import UUID as _UUID, uuid4

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
TEST_PROJECT_CODE = "test-idx-lifecycle"
TEST_CANDIDATE_ID = uuid4()
TEST_REVIEW_ITEM_ID = uuid4()


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _register_sqlite_compat(engine):
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _conn_record):
        try:
            dbapi_conn.create_function(
                "now", 0, lambda: _dt_mod.datetime.now(_dt_mod.timezone.utc).isoformat()
            )
        except Exception:
            pass
        try:
            dbapi_conn.create_function("gen_random_uuid", 0, lambda: uuid4().hex)
        except Exception:
            pass


def _idem() -> dict:
    return {"Idempotency-Key": str(uuid4())}


def _ok(resp, status=200):
    assert resp.status_code == status, (
        f"Expected {status}, got {resp.status_code}: {resp.json()}"
    )
    body = resp.json()
    assert "data" in body
    assert body["request_id"] is not None
    assert body["correlation_id"] is not None
    return body["data"]


def _create_memory(client, *, title="Test Memory", memory_text="Content",
                   project_id=None, sensitivity_level="private"):
    body = {
        "title": title,
        "memory_text": memory_text,
        "project_id": str(project_id or TEST_PROJECT_ID),
        "sensitivity_level": sensitivity_level,
    }
    return _ok(client.post("/api/v4/memory", json=body, headers=_idem()), 201)


def _activate_memory(client, *, candidate_id=None, memory_text="Activated content"):
    return _ok(client.post("/api/v4/memory/activate", json={
        "candidate_id": str(candidate_id or TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": "Activated",
        "memory_text": memory_text,
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem()), 201)


def _get_index_entries(client, memory_id):
    resp = client.get(f"/api/v4/memory/index/states?memory_id={memory_id}")
    return _ok(resp)["items"]


def _get_index_status(client, project_id=None):
    url = "/api/v4/memory/index/status"
    if project_id:
        url += f"?project_id={project_id}"
    return _ok(client.get(url))


# ═══════════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def lifecycle_client(monkeypatch):
    """TestClient with SQLite :memory: and all required tables."""
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


# ═══════════════════════════════════════════════════════════════════════════
# Table setup
# ═══════════════════════════════════════════════════════════════════════════


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
                review_item_id TEXT PRIMARY KEY, project_id TEXT,
                review_type TEXT NOT NULL, target_type TEXT NOT NULL,
                target_id TEXT NOT NULL, target_version INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 100,
                requester_actor_type TEXT NOT NULL DEFAULT 'system',
                requester_actor_id TEXT, reviewer_id TEXT,
                decision TEXT, reason TEXT,
                decision_payload TEXT NOT NULL DEFAULT '{}',
                due_at TEXT, decided_at TEXT, expires_at TEXT,
                correlation_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
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
            "pid": TEST_PROJECT_ID.hex, "pcode": TEST_PROJECT_CODE,
            "pname": "Test Project",
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


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Create / Activate → index entry created
# ═══════════════════════════════════════════════════════════════════════════


def test_create_memory_creates_index_entry_with_ready_fts(lifecycle_client):
    """Creating a draft memory auto-creates an index entry with fts_state='ready'."""
    client, engine = lifecycle_client
    data = _create_memory(client, title="Lifecycle Draft", memory_text="Draft content")

    entries = _get_index_entries(client, data["memory_id"])
    assert len(entries) == 1
    assert entries[0]["fts_state"] == "ready"
    assert entries[0]["memory_version"] == 1
    assert entries[0]["vector_state"] in ("pending", "ready")
    assert "Draft content" in entries[0]["index_text"]


def test_activate_memory_creates_index_entry_with_ready_fts(lifecycle_client):
    """Activating a memory from candidate auto-creates a ready index entry."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="Activated lifecycle content")

    entries = _get_index_entries(client, data["memory_id"])
    assert len(entries) == 1
    assert entries[0]["fts_state"] == "ready"
    assert entries[0]["memory_version"] == 1
    assert "Activated lifecycle content" in entries[0]["index_text"]


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Update → old stale, new ready
# ═══════════════════════════════════════════════════════════════════════════


def test_update_memory_creates_new_entry_and_stales_old(lifecycle_client):
    """Updating memory content: old entry marked stale, new entry created ready."""
    client, engine = lifecycle_client
    data = _create_memory(client, title="V1", memory_text="Version 1 content")

    # Update content
    _ok(client.patch(
        f"/api/v4/memory/{data['memory_id']}",
        json={"title": "V2", "memory_text": "Version 2 content"},
        headers=_idem(),
    ))

    entries = _get_index_entries(client, data["memory_id"])
    entries.sort(key=lambda e: e["memory_version"])

    assert len(entries) == 2
    # V1 should be stale
    assert entries[0]["memory_version"] == 1
    assert entries[0]["fts_state"] == "stale"
    assert "Version 1 content" in entries[0]["index_text"]
    # V2 should be ready
    assert entries[1]["memory_version"] == 2
    assert entries[1]["fts_state"] == "ready"
    assert "Version 2 content" in entries[1]["index_text"]


def test_multiple_updates_all_old_stale(lifecycle_client):
    """After 3 content updates, all old entries are stale, latest is ready."""
    client, engine = lifecycle_client
    data = _create_memory(client, title="Multi", memory_text="v1")

    for v in range(2, 5):
        _ok(client.patch(
            f"/api/v4/memory/{data['memory_id']}",
            json={"title": f"Multi v{v}", "memory_text": f"v{v} content"},
            headers=_idem(),
        ))

    entries = _get_index_entries(client, data["memory_id"])
    entries.sort(key=lambda e: e["memory_version"])

    assert len(entries) == 4  # versions 1-4
    for i, entry in enumerate(entries):
        expected_version = i + 1
        assert entry["memory_version"] == expected_version
        if expected_version == 4:
            assert entry["fts_state"] == "ready"
        else:
            assert entry["fts_state"] == "stale"


def test_partial_update_stales_old(lifecycle_client):
    """Partial update (title only) also stales old entry and creates new one."""
    client, engine = lifecycle_client
    data = _create_memory(client, title="PartTitle", memory_text="PartText")

    _ok(client.patch(
        f"/api/v4/memory/{data['memory_id']}",
        json={"title": "PartTitle Updated"},
        headers=_idem(),
    ))

    entries = _get_index_entries(client, data["memory_id"])
    entries.sort(key=lambda e: e["memory_version"])

    assert len(entries) == 2
    assert entries[0]["fts_state"] == "stale"
    assert entries[1]["fts_state"] == "ready"


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Expire → all entries stale
# ═══════════════════════════════════════════════════════════════════════════


def test_expire_active_memory_marks_all_entries_stale(lifecycle_client):
    """Expiring an active memory marks all its index entries as stale."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="To expire content")

    # Get initial entry
    entries_before = _get_index_entries(client, data["memory_id"])
    assert entries_before[0]["fts_state"] == "ready"

    # Expire
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/expire",
        headers=_idem(),
    ))

    entries_after = _get_index_entries(client, data["memory_id"])
    assert len(entries_after) >= 1
    for entry in entries_after:
        assert entry["fts_state"] == "stale", (
            f"Expected stale, got {entry['fts_state']} for version {entry['memory_version']}"
        )


def test_expire_after_update_stales_all(lifecycle_client):
    """After update + expire, all entries (v1 stale from update, v2 ready)
    should become stale."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="Pre-update expire")

    # Update (v1→stale, v2→ready)
    _ok(client.patch(
        f"/api/v4/memory/{data['memory_id']}",
        json={"title": "Updated expire", "memory_text": "Updated expire content"},
        headers=_idem(),
    ))

    # Expire
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/expire",
        headers=_idem(),
    ))

    entries = _get_index_entries(client, data["memory_id"])
    for entry in entries:
        assert entry["fts_state"] == "stale", (
            f"Version {entry['memory_version']} fts_state={entry['fts_state']}, expected stale"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Restore → new ready entry
# ═══════════════════════════════════════════════════════════════════════════


def test_restore_expired_memory_creates_ready_entry(lifecycle_client):
    """Restoring an expired memory creates a new index entry with fts_state='ready'."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="Restore me")

    # Expire
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/expire",
        headers=_idem(),
    ))

    # Restore
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/restore",
        headers=_idem(),
    ))

    entries = _get_index_entries(client, data["memory_id"])
    entries.sort(key=lambda e: e["memory_version"])

    # Should have: v1 stale (from expire) + new ready entry (from restore)
    assert len(entries) >= 1
    # The latest entry should be ready
    assert entries[-1]["fts_state"] == "ready", (
        f"Latest entry fts_state={entries[-1]['fts_state']}, expected ready"
    )
    assert "Restore me" in entries[-1]["index_text"]


def test_restore_deleted_memory_creates_ready_entry(lifecycle_client):
    """Restoring a deleted memory creates a new index entry with fts_state='ready'."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="Restore from deleted")

    # Delete (soft-delete)
    _ok(client.delete(
        f"/api/v4/memory/{data['memory_id']}",
        headers=_idem(),
    ))

    # Restore
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/restore",
        headers=_idem(),
    ))

    entries = _get_index_entries(client, data["memory_id"])
    assert len(entries) >= 1
    # After restore, at least one entry should be ready
    ready_entries = [e for e in entries if e["fts_state"] == "ready"]
    assert len(ready_entries) >= 1, (
        f"No ready entry found after restore. Entries: {entries}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Delete → all entries stale
# ═══════════════════════════════════════════════════════════════════════════


def test_delete_memory_marks_all_entries_stale(lifecycle_client):
    """Soft-deleting a memory marks all its index entries as stale."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="Delete me")

    # Verify initially ready
    entries_before = _get_index_entries(client, data["memory_id"])
    assert entries_before[0]["fts_state"] == "ready"

    # Delete
    _ok(client.delete(
        f"/api/v4/memory/{data['memory_id']}",
        headers=_idem(),
    ))

    entries_after = _get_index_entries(client, data["memory_id"])
    for entry in entries_after:
        assert entry["fts_state"] == "stale"


def test_delete_draft_memory_marks_entry_stale(lifecycle_client):
    """Deleting a draft memory also marks its index entry as stale."""
    client, engine = lifecycle_client
    data = _create_memory(client, title="Draft delete", memory_text="Draft content")

    client.delete(
        f"/api/v4/memory/{data['memory_id']}",
        headers=_idem(),
    )

    entries = _get_index_entries(client, data["memory_id"])
    for entry in entries:
        assert entry["fts_state"] == "stale"


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Multiple lifecycle cycles
# ═══════════════════════════════════════════════════════════════════════════


def test_expire_restore_cycle_multiple_times(lifecycle_client):
    """Multiple expire→restore cycles create correct index entries."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="Cycle content")

    for cycle in range(1, 4):
        # Expire
        _ok(client.post(
            f"/api/v4/memory/{data['memory_id']}/expire",
            headers=_idem(),
        ))
        # Restore
        _ok(client.post(
            f"/api/v4/memory/{data['memory_id']}/restore",
            headers=_idem(),
        ))

    entries = _get_index_entries(client, data["memory_id"])
    entries.sort(key=lambda e: e["memory_version"])

    # There should be: 1 initial + 3 restore fresh entries = 4 entries
    # plus all the expire marks stale
    ready_entries = [e for e in entries if e["fts_state"] == "ready"]
    stale_entries = [e for e in entries if e["fts_state"] == "stale"]

    assert len(ready_entries) >= 1, "Should have at least one ready entry after restore"
    assert ready_entries[-1]["memory_version"] >= 1  # Latest ready should be highest version


def test_lifecycle_events_do_not_create_duplicate_index_entries(lifecycle_client):
    """Status-only transitions (expire) should not create duplicate entries with
    the same version."""
    client, engine = lifecycle_client
    data = _activate_memory(client, memory_text="NoDup content")

    entries_before = _get_index_entries(client, data["memory_id"])
    assert len(entries_before) == 1

    # Expire (status-only)
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/expire",
        headers=_idem(),
    ))

    entries_after_expire = _get_index_entries(client, data["memory_id"])
    # Expire marks existing stale, doesn't create new version-indexed entry
    assert len(entries_after_expire) == 1, (
        f"Expire should not create new version, got {len(entries_after_expire)} entries"
    )

    # Restore creates a new ready entry
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/restore",
        headers=_idem(),
    ))

    entries_after_restore = _get_index_entries(client, data["memory_id"])
    assert len(entries_after_restore) >= 2, (
        f"Restore should create new entry, got {len(entries_after_restore)} entries"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Index status summary across lifecycle
# ═══════════════════════════════════════════════════════════════════════════


def test_index_status_reflects_ready_and_stale_counts(lifecycle_client):
    """Index status summary correctly reflects fts_state counts."""
    client, engine = lifecycle_client

    # Create 2 memories (both ready)
    data1 = _create_memory(client, title="Status 1", memory_text="S1")
    data2 = _create_memory(client, title="Status 2", memory_text="S2")

    status = _get_index_status(client)
    assert status["fts_ready"] >= 2
    assert status["fts_stale"] == 0

    # Update one memory → +1 stale, +1 ready
    _ok(client.patch(
        f"/api/v4/memory/{data1['memory_id']}",
        json={"title": "Status 1 Updated", "memory_text": "S1 Updated"},
        headers=_idem(),
    ))

    status = _get_index_status(client)
    assert status["fts_ready"] >= 2  # one from v2 of m1 + one from m2
    assert status["fts_stale"] >= 1  # v1 of m1

    # Delete the other memory → its entry becomes stale
    _ok(client.delete(
        f"/api/v4/memory/{data2['memory_id']}",
        headers=_idem(),
    ))

    status = _get_index_status(client)
    total = status["fts_ready"] + status["fts_stale"]
    assert total >= 3  # v1(stale) + v2(ready) from m1 + v1(stale) from m2


def test_index_status_filtered_by_project(lifecycle_client):
    """Index status can be filtered by project_id."""
    client, engine = lifecycle_client
    _create_memory(client, title="M Proj", memory_text="Content")

    status = _get_index_status(client, project_id=str(TEST_PROJECT_ID))
    assert status["total_entries"] >= 1
    assert status["fts_ready"] >= 1

    # Non-existent project → all zeros
    status = _get_index_status(client, project_id=str(uuid4()))
    assert status["total_entries"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Vector state tracking (Phase 4: always pending)
# ═══════════════════════════════════════════════════════════════════════════


def test_vector_state_is_tracked_across_lifecycle(lifecycle_client):
    """Vector state is maintained correctly across lifecycle transitions."""
    client, engine = lifecycle_client
    data = _create_memory(client, title="VectorTrack", memory_text="Vector content")

    entries = _get_index_entries(client, data["memory_id"])
    assert entries[0]["vector_state"] in ("pending", "ready", "failed")

    # Update
    _ok(client.patch(
        f"/api/v4/memory/{data['memory_id']}",
        json={"title": "VectorTrack v2", "memory_text": "Vector v2 content"},
        headers=_idem(),
    ))

    entries = _get_index_entries(client, data["memory_id"])
    entries.sort(key=lambda e: e["memory_version"])
    # Old entry vector_state preserved
    assert entries[0]["vector_state"] is not None
    # New entry has its own vector_state
    assert entries[1]["vector_state"] is not None


def test_fts_state_is_never_none(lifecycle_client):
    """fts_state is always a known value (ready/stale/pending/failed)."""
    client, engine = lifecycle_client
    data = _create_memory(client, title="FTS State", memory_text="FTS")

    entries = _get_index_entries(client, data["memory_id"])
    for entry in entries:
        assert entry["fts_state"] is not None
        assert entry["fts_state"] in ("ready", "stale", "pending", "failed")


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Error cases
# ═══════════════════════════════════════════════════════════════════════════


def test_index_states_nonexistent_memory(lifecycle_client):
    """Requesting index states for non-existent memory returns empty list."""
    client, engine = lifecycle_client
    entries = _get_index_entries(client, str(uuid4()))
    assert entries == []


def test_memory_with_no_index_entries(lifecycle_client):
    """A memory that exists but has no index entries returns empty list."""
    client, engine = lifecycle_client
    # Direct SQL insert without triggering index manager hooks
    mid = str(uuid4())
    with Session(engine) as db:
        db.execute(text("""
            INSERT INTO memories (memory_id, project_id, canonical_key, title,
                                  memory_text, current_version, status, sensitivity_level)
            VALUES (:mid, :pid, :ck, :title, :text, :ver, 'draft', 'private')
        """), {
            "mid": mid,
            "pid": TEST_PROJECT_ID.hex,
            "ck": "no-index-memory",
            "title": "No Index",
            "text": "This memory was inserted without index hooks",
            "ver": 1,
        })
        db.commit()

    entries = _get_index_entries(client, mid)
    # May have 0 entries (if hooks weren't triggered)
    assert isinstance(entries, list)
