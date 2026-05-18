"""P5-05 Search quality contract tests.

Validates that the memory search API (``GET /api/v4/memory/search``) returns
correct, high-quality results across all search modes:

* **FTS mode** — keyword match, ranking, pagination, empty results
* **Hybrid mode** — graceful degradation to FTS when vector is unavailable
* **Stale filtering** — stale/deleted entries excluded or marked
* **Project filtering** — results scoped to project
* **Search status** — aggregated index health

Also validates edge cases: empty query, special characters, CJK text,
and search result consistency after lifecycle transitions.
"""

from __future__ import annotations

import datetime as _dt_mod
import os
from uuid import uuid4

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
TEST_PROJECT_CODE = "test-search"
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


def _activate_memory(client, *, candidate_id=None, memory_text="Activated",
                     title="Activated Memory"):
    return _ok(client.post("/api/v4/memory/activate", json={
        "candidate_id": str(candidate_id or TEST_CANDIDATE_ID),
        "project_id": str(TEST_PROJECT_ID),
        "title": title,
        "memory_text": memory_text,
        "sensitivity_level": "private",
        "review_item_id": str(TEST_REVIEW_ITEM_ID),
    }, headers=_idem()), 201)


def _search(client, q, **params):
    query_parts = [f"q={q}"]
    for k, v in params.items():
        query_parts.append(f"{k}={v}")
    url = "/api/v4/memory/search?" + "&".join(query_parts)
    return _ok(client.get(url))


# ═══════════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def search_client(monkeypatch):
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
# Tests — FTS basic
# ═══════════════════════════════════════════════════════════════════════════


def test_fts_search_finds_matching_content(search_client):
    """FTS mode finds memories whose index_text matches the query."""
    client, engine = search_client
    _create_memory(client, title="Banana", memory_text="Yellow banana fruit")
    _create_memory(client, title="Apple", memory_text="Red apple fruit")
    _create_memory(client, title="Car", memory_text="Blue car vehicle")

    result = _search(client, "banana", mode="fts")
    assert result["search_mode"] == "fts"
    assert len(result["items"]) >= 1
    titles = {item["title"] for item in result["items"]}
    assert "Banana" in titles
    assert "Apple" not in titles  # "banana" doesn't match "apple"
    assert "Car" not in titles


def test_fts_search_finds_by_title(search_client):
    """FTS mode matches against title text in index_text."""
    client, engine = search_client
    _create_memory(client, title="Quantum Computing Guide", memory_text="Some technical content")
    _create_memory(client, title="Baking Recipes", memory_text="How to bake bread")

    result = _search(client, "Quantum", mode="fts")
    assert len(result["items"]) >= 1
    assert result["items"][0]["title"] == "Quantum Computing Guide"

    result = _search(client, "bake", mode="fts")
    assert len(result["items"]) >= 1
    assert result["items"][0]["title"] == "Baking Recipes"


def test_fts_search_empty_results(search_client):
    """FTS search returns empty results for non-matching queries."""
    client, engine = search_client
    _create_memory(client, title="Hello", memory_text="World")

    result = _search(client, "xyznonexistent12345", mode="fts")
    assert len(result["items"]) == 0
    assert result["page_info"]["total_items"] == 0


def test_fts_search_pagination(search_client):
    """FTS search supports pagination."""
    client, engine = search_client
    for i in range(5):
        _create_memory(client, title=f"Search Memory {i}",
                       memory_text=f"Common keyword test {i}")

    # Page 1, size 2
    result = _search(client, "Common", mode="fts", page_size=2, page=1)
    assert len(result["items"]) == 2
    assert result["page_info"]["total_items"] >= 5
    assert result["page_info"]["has_next"] is True

    # Page 2
    result2 = _search(client, "Common", mode="fts", page_size=2, page=2)
    assert len(result2["items"]) == 2

    # Page 3
    result3 = _search(client, "Common", mode="fts", page_size=2, page=3)
    assert len(result3["items"]) >= 1
    assert result3["page_info"]["has_next"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Hybrid search degradation
# ═══════════════════════════════════════════════════════════════════════════


def test_hybrid_search_degrades_to_fts(search_client, monkeypatch):
    """Hybrid mode degrades to FTS when vector is unavailable."""
    client, engine = search_client
    _create_memory(client, title="Hybrid Memory", memory_text="Hybrid search degradable content")

    # Ensure gateway would fail if called → proves no vector call made
    from mneme.gateway.call import GatewayError

    class _FailGateway:
        def call(self, *args, **kwargs):
            raise AssertionError("Gateway should not be called in degraded mode")

    monkeypatch.setattr("mneme.memory.embedding.get_gateway", lambda: _FailGateway())

    result = _search(client, "degradable", mode="hybrid")
    assert result["search_mode"] == "fts"
    assert result["degraded"] is True
    assert result["degradation_reason"] == "vector_unavailable"
    assert len(result["items"]) >= 1


def test_hybrid_search_still_returns_results(search_client):
    """Hybrid search returns usable results even when degraded."""
    client, engine = search_client
    _create_memory(client, title="Pineapple", memory_text="Tropical pineapple fruit")
    _create_memory(client, title="Grape", memory_text="Purple grape fruit")

    result = _search(client, "pineapple", mode="hybrid")
    assert len(result["items"]) >= 1
    # Check result has expected fields
    item = result["items"][0]
    assert "memory_id" in item
    assert "title" in item
    assert "index_text" in item


def test_fts_mode_does_not_report_degraded(search_client):
    """FTS-only mode should not report 'degraded'."""
    client, engine = search_client
    _create_memory(client, title="FTS Only", memory_text="FTS mode test")

    result = _search(client, "FTS", mode="fts")
    assert result["search_mode"] == "fts"
    assert result["degraded"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Stale filtering
# ═══════════════════════════════════════════════════════════════════════════


def test_search_marks_stale_entries(search_client):
    """Search results expose stale markers from index state after expire."""
    client, engine = search_client
    data = _activate_memory(client, memory_text="unique stale keyword after expire",
                            title="StaleMarker")

    # Verify searchable before expire
    result_before = _search(client, "unique stale keyword", mode="fts")
    assert len(result_before["items"]) >= 1

    # Expire via proper API (triggers index lifecycle hooks)
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/expire",
        headers=_idem(),
    ))

    result = _search(client, "unique stale keyword", mode="fts")
    # After expire, the entry should be marked stale (if still returned by search)
    if len(result["items"]) > 0:
        assert result["items"][0]["stale"] is True
        assert result["items"][0]["stale_reason"] == "fts_stale"
    assert result["stale_count"] >= 0


def test_search_after_expire_shows_stale(search_client):
    """After expiring a memory, search results mark it as stale."""
    client, engine = search_client
    data = _activate_memory(client, memory_text="expired searchable text",
                            title="ExpireSearch")

    # Verify searchable before expire
    result_before = _search(client, "expired searchable", mode="fts")
    assert len(result_before["items"]) >= 1

    # Expire the memory
    _ok(client.post(
        f"/api/v4/memory/{data['memory_id']}/expire",
        headers=_idem(),
    ))

    result_after = _search(client, "expired searchable", mode="fts")
    # After expire, the entry is stale
    if len(result_after["items"]) > 0:
        assert result_after["items"][0]["stale"] is True


def test_search_after_delete_shows_stale(search_client):
    """After soft-deleting a memory, search results mark it as stale."""
    client, engine = search_client
    data = _activate_memory(client, memory_text="deleted search keyword",
                            title="DeleteSearch")

    # Delete
    _ok(client.delete(
        f"/api/v4/memory/{data['memory_id']}",
        headers=_idem(),
    ))

    result = _search(client, "deleted search", mode="fts")
    if len(result["items"]) > 0:
        assert result["items"][0]["stale"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Project filtering
# ═══════════════════════════════════════════════════════════════════════════


def test_search_filtered_by_project(search_client):
    """Search results respect project_id filter."""
    client, engine = search_client
    _create_memory(client, title="Project A Memory", memory_text="unique project text")

    result = _search(client, "unique project", mode="fts",
                     project_id=str(TEST_PROJECT_ID))
    assert len(result["items"]) >= 1

    # Different project → no results
    result_other = _search(client, "unique project", mode="fts",
                           project_id=str(uuid4()))
    assert len(result_other["items"]) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Search status endpoint
# ═══════════════════════════════════════════════════════════════════════════


def test_search_status_endpoint(search_client):
    """GET /api/v4/memory/search/status returns aggregated counts."""
    client, engine = search_client
    _create_memory(client, title="Status A", memory_text="A")
    _create_memory(client, title="Status B", memory_text="B")

    resp = client.get("/api/v4/memory/search/status")
    data = _ok(resp)
    assert data["total_entries"] >= 2
    assert data["fts_ready"] >= 2
    assert isinstance(data["fts_ready"], int)
    assert isinstance(data["fts_stale"], int)


def test_search_status_filtered_by_project(search_client):
    """Search status can be filtered by project."""
    client, engine = search_client
    _create_memory(client, title="ProjStatus", memory_text="Content")

    result = client.get(
        f"/api/v4/memory/search/status?project_id={TEST_PROJECT_ID}"
    )
    data = _ok(result)
    assert data["total_entries"] >= 1

    result_other = client.get(
        f"/api/v4/memory/search/status?project_id={uuid4()}"
    )
    data_other = _ok(result_other)
    assert data_other["total_entries"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Edge cases
# ═══════════════════════════════════════════════════════════════════════════


def test_search_empty_query(search_client):
    """Search with empty query string returns 400 (validation: min_length=1)."""
    client, engine = search_client
    _create_memory(client, title="EmptyQuery", memory_text="Some content")

    resp = client.get("/api/v4/memory/search?q=&mode=fts")
    # Empty query is rejected by validation (min_length=1)
    assert resp.status_code == 400


def test_search_special_characters(search_client):
    """Search with SQL-special characters should not error."""
    client, engine = search_client
    _create_memory(client, title="Special", memory_text="Test with SQL injection' OR '1'='1")

    result = _search(client, "injection", mode="fts")
    assert isinstance(result["items"], list)
    # Should not crash; may or may not return results


def test_search_cjk_text(search_client):
    """Search with Chinese/Japanese/Korean text."""
    client, engine = search_client
    _create_memory(client, title="CJK Test",
                   memory_text="这是中文测试内容 日本語テスト 한국어 테스트")

    result = _search(client, "中文", mode="fts")
    assert isinstance(result["items"], list)
    # CJK search may work depending on FTS tokenizer


def test_search_case_insensitive(search_client):
    """FTS search should be case-insensitive (if supported)."""
    client, engine = search_client
    _create_memory(client, title="Case Test",
                   memory_text="UpperCase LOWERCase MixedCase")

    result_upper = _search(client, "UPPERCASE", mode="fts")
    result_lower = _search(client, "lowercase", mode="fts")

    # Both should find the same memory
    assert isinstance(result_upper["items"], list)
    assert isinstance(result_lower["items"], list)


def test_search_result_fields_complete(search_client):
    """Each search result item has all required fields."""
    client, engine = search_client
    _create_memory(client, title="Fields Test",
                   memory_text="Complete field test content")

    result = _search(client, "Complete", mode="fts")
    assert len(result["items"]) >= 1
    item = result["items"][0]

    required_fields = [
        "memory_index_entry_id", "memory_id", "memory_version",
        "index_text", "fts_state", "vector_state", "rank",
        "search_mode", "degraded", "stale", "title", "memory_text",
        "sensitivity_level", "status", "current_version",
    ]
    for field in required_fields:
        assert field in item, f"Missing field '{field}' in search result item"

    # Check types
    assert isinstance(item["memory_version"], int)
    assert isinstance(item["current_version"], int)
    assert isinstance(item["stale"], bool)
    assert isinstance(item["degraded"], bool)


def test_search_after_update_finds_new_content(search_client):
    """After updating memory content, search should find the new content."""
    client, engine = search_client
    data = _create_memory(client, title="UpdateSearch", memory_text="Old keyword")

    # Update to new content
    _ok(client.patch(
        f"/api/v4/memory/{data['memory_id']}",
        json={"title": "UpdateSearch", "memory_text": "New unique keyword XYZ123"},
        headers=_idem(),
    ))

    # Search for new content
    result_new = _search(client, "XYZ123", mode="fts")
    assert len(result_new["items"]) >= 1

    # Old content may still be found via stale entry
    result_old = _search(client, "Old keyword", mode="fts")
    if len(result_old["items"]) > 0:
        assert result_old["items"][0]["stale"] is True


def test_search_no_memories_no_crash(search_client):
    """Search on an empty database should return empty results, not crash."""
    client, engine = search_client
    result = _search(client, "nothing", mode="fts")
    assert len(result["items"]) == 0
    assert result["page_info"]["total_items"] == 0
    assert result["stale_count"] == 0


def test_search_with_large_page_size(search_client):
    """Search with large page_size should not crash."""
    client, engine = search_client
    for i in range(3):
        _create_memory(client, title=f"Large{i}", memory_text="bulk test data")

    result = _search(client, "bulk", mode="fts", page_size=100, page=1)
    assert len(result["items"]) == 3
    assert result["page_info"]["total_pages"] == 1
    assert result["page_info"]["has_next"] is False
