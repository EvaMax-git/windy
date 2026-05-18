"""R3 Memory System contract tests.

Covers new-architecture memory endpoints:
* Memory CRUD:             POST/GET/PATCH /api/v4/memory
* Memory activate:         POST /api/v4/memory/activate
* Memory lifecycle:        POST /api/v4/memory/{id}/merge|expire|restore
* Memory delete:           DELETE /api/v4/memory/{id}
* Memory versions:         GET  /api/v4/memory/{id}/versions
* Memory sources:          POST/GET/DELETE /api/v4/memory/{id}/sources
* Memory search:           GET  /api/v4/memory/search
* Memory search status:    GET  /api/v4/memory/search/status
* Memory extract:          POST /api/v4/memory/extract
* Memory batch:            POST /api/v4/memory/approve|reject
* Memory candidates:       POST/GET /api/v4/memory-candidates
* Memory index:            GET  /api/v4/memory-index/entries
* Memory relations:        POST/GET /api/v4/memory/relations
* Conversations:           POST/GET /api/v4/conversations
* Messages:                POST/GET /api/v4/messages
* Raw events:              POST/GET /api/v4/raw-events
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

    # Monkey-patch PostgreSQL-specific bootstrap functions so they don't
    # crash during test lifespan on SQLite (they use ::jsonb, ANY(?), etc.)
    import mneme.db.sub_library_registry as _slr
    import mneme.db.pipelines as _pipelines
    _orig_bootstrap_sub = _slr.bootstrap_sub_libraries
    _orig_seed_pipelines = _pipelines.seed_default_asset_import_pipelines

    def _safe_bootstrap():
        try:
            return _orig_bootstrap_sub()
        except Exception:
            return 0

    def _safe_seed():
        try:
            return _orig_seed_pipelines()
        except Exception:
            return 0

    _slr.bootstrap_sub_libraries = _safe_bootstrap
    _pipelines.seed_default_asset_import_pipelines = _safe_seed

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
            "uname": "r3_user",
            "email": "r3_user@test.local",
            "dname": "R3 User",
            "role": "owner",
            "status": "active",
            "phash": hash_password("r3pass123"),
            "mfa": "none",
            "now": now_val,
        },
    )
    db.commit()
    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "r3_user", "password": "r3pass123"},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.json()}"
    return client


@pytest.fixture
def test_project(auth_client, db):
    from sqlalchemy import text
    pid = uuid4()
    pcode = f"R3PROJ-{pid.hex[:6].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default, created_at, updated_at) "
            "VALUES (:pid, :code, :name, 'active', 'normal', datetime('now'), datetime('now'))"
        ),
        {"pid": str(pid), "code": pcode, "name": "R3 Test Project"},
    )
    db.commit()
    return pid, pcode


@pytest.fixture
def draft_memory(auth_client, test_project, db):
    """Create a draft memory and return its memory_id."""
    pid, pcode = test_project
    resp = auth_client.post(
        "/api/v4/memory",
        json={
            "project_id": str(pid),
            "title": "Test Memory",
            "memory_text": "This is a test memory for R3.",
            "sensitivity_level": "normal",
        },
        headers=_idem_headers(),
    )
    assert resp.status_code == 201, f"Create memory failed: {resp.json()}"
    return UUID(resp.json()["data"]["memory_id"])


# ═══════════════════════════════════════════════════════════════════════════
# R3.1 — Memory CRUD
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryCRUD:
    """Memory create, read, update, delete."""

    def test_create_memory_requires_idempotency_key(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/memory",
            json={
                "project_id": str(pid),
                "title": "No Key",
                "memory_text": "Missing idempotency key.",
            },
        )
        assert resp.status_code == 400

    def test_create_memory_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/memory",
            json={
                "project_id": str(pid),
                "title": "My Memory",
                "memory_text": "Memory content here.",
                "sensitivity_level": "normal",
            },
            headers=_idem_headers(),
        )
        import sys, json
        from sqlalchemy import text as _t
        from mneme.db.base import SessionLocal
        _check_db = SessionLocal()
        _rows = _check_db.execute(_t("SELECT project_id, project_code FROM projects")).all()
        print("\n\n=== PROJECTS IN API SESSION ===", flush=True, file=sys.stderr)
        for _r in _rows:
            print(f"  id={_r[0]!r}, type={type(_r[0]).__name__}, len={len(_r[0]) if isinstance(_r[0], str) else 'N/A'}", flush=True, file=sys.stderr)
        _check_db.close()
        print("\n\n=== RESPONSE ===", resp.status_code, json.dumps(resp.json(), indent=2, default=str), flush=True, file=sys.stderr)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["title"] == "My Memory"
        assert data["status"] == "draft"
        assert "memory_id" in data

    def test_create_memory_creates_draft_status(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/memory",
            json={
                "project_id": str(pid),
                "title": "Draft Memory",
                "memory_text": "Draft content.",
            },
            headers=_idem_headers(),
        )
        assert resp.json()["data"]["status"] == "draft"

    def test_list_memories_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/memory")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_list_memories_filter_by_project(self, auth_client, test_project, draft_memory):
        pid, _ = test_project
        resp = auth_client.get("/api/v4/memory", params={"project_id": str(pid)})
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) >= 1

    def test_get_memory_succeeds(self, auth_client, draft_memory):
        resp = auth_client.get(f"/api/v4/memory/{draft_memory}")
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "Test Memory"

    def test_get_memory_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/memory/{uuid4()}")
        assert resp.status_code == 404

    def test_update_memory_succeeds(self, auth_client, draft_memory):
        resp = auth_client.patch(
            f"/api/v4/memory/{draft_memory}",
            json={"title": "Updated Memory Title"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "Updated Memory Title"

    def test_delete_memory_succeeds(self, auth_client, draft_memory):
        resp = auth_client.delete(
            f"/api/v4/memory/{draft_memory}",
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True

    def test_delete_already_deleted_returns_409(self, auth_client, draft_memory):
        auth_client.delete(
            f"/api/v4/memory/{draft_memory}",
            headers=_idem_headers(),
        )
        resp = auth_client.delete(
            f"/api/v4/memory/{draft_memory}",
            headers=_idem_headers(),
        )
        assert resp.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
# R3.2 — Memory Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryLifecycle:
    """Memory expire, restore, merge."""

    def test_expire_active_memory(self, auth_client, test_project, db):
        """Expire requires active status; draft must be approved first."""
        # Direct insert an active memory since activate requires candidate+review
        from sqlalchemy import text
        pid, pcode = test_project
        mem_id = uuid4()
        db.execute(
            text(
                "INSERT INTO memories (memory_id, project_id, canonical_key, title, memory_text, "
                "current_version, sensitivity_level, status, activated_by_review_item_id, "
                "created_at, updated_at) "
                "VALUES (:mid, :pid, :ckey, :title, :mtext, 1, 'normal', 'active', :arid, "
                "datetime('now'), datetime('now'))"
            ),
            {
                "mid": str(mem_id),
                "pid": str(pid),
                "ckey": f"{pcode}-mem-99",
                "title": "Active Memory",
                "mtext": "Active content.",
                "arid": str(uuid4()),
            },
        )
        db.commit()
        resp = auth_client.post(
            f"/api/v4/memory/{mem_id}/expire",
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "expired"

    def test_expire_non_active_fails(self, auth_client, draft_memory):
        resp = auth_client.post(
            f"/api/v4/memory/{draft_memory}/expire",
            headers=_idem_headers(),
        )
        assert resp.status_code == 409

    def test_merge_memory_not_found_target(self, auth_client, draft_memory):
        resp = auth_client.post(
            f"/api/v4/memory/{draft_memory}/merge",
            json={"target_memory_id": str(uuid4())},
            headers=_idem_headers(),
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.3 — Memory Versions
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryVersions:
    """Version history for memories."""

    def test_list_versions_succeeds(self, auth_client, draft_memory):
        resp = auth_client.get(f"/api/v4/memory/{draft_memory}/versions")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "items" in data
        assert "page_info" in data

    def test_list_versions_memory_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/memory/{uuid4()}/versions")
        assert resp.status_code == 404

    def test_get_version_not_found(self, auth_client, draft_memory):
        resp = auth_client.get(f"/api/v4/memory/{draft_memory}/versions/999")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.4 — Memory Sources
# ═══════════════════════════════════════════════════════════════════════════


class TestMemorySources:
    """Evidence source links for memories."""

    def test_list_sources_empty(self, auth_client, draft_memory):
        resp = auth_client.get(f"/api/v4/memory/{draft_memory}/sources")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "items" in data

    def test_add_source_to_memory(self, auth_client, draft_memory):
        resp = auth_client.post(
            f"/api/v4/memory/{draft_memory}/sources",
            json={"source_role": "evidence"},
            headers=_idem_headers(),
        )
        # May fail if no candidate_id/asset_id/etc provided
        assert resp.status_code in (200, 201, 400)

    def test_list_sources_memory_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/memory/{uuid4()}/sources")
        assert resp.status_code == 404

    def test_remove_source_not_found(self, auth_client):
        resp = auth_client.delete(
            f"/api/v4/memory/sources/{uuid4()}",
            headers=_idem_headers(),
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.5 — Memory Search
# ═══════════════════════════════════════════════════════════════════════════


class TestMemorySearch:
    """Memory search endpoints."""

    def test_memory_search_requires_query(self, auth_client):
        resp = auth_client.get("/api/v4/memory/search")
        assert resp.status_code == 400  # custom handler converts 422→400

    def test_memory_search_returns_results(self, auth_client):
        resp = auth_client.get("/api/v4/memory/search", params={"q": "test"})
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "search_mode" in data

    def test_memory_search_status(self, auth_client):
        resp = auth_client.get("/api/v4/memory/search/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # Should return aggregate counts
        assert isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════════════════
# R3.6 — Memory Candidates
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryCandidates:
    """Memory candidate endpoints."""

    def test_list_candidates_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/memory-candidates")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body

    def test_list_candidates_filter_by_project(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.get(
            "/api/v4/memory-candidates", params={"project_id": str(pid)}
        )
        assert resp.status_code == 200

    def test_get_candidate_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/memory-candidates/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.7 — Memory Index
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryIndex:
    """Memory index entry endpoints."""

    def test_list_index_entries_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/memory-index/entries")
        assert resp.status_code == 200

    def test_get_index_entry_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/memory-index/entries/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.8 — Memory Relations
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryRelations:
    """Memory relation endpoints."""

    def test_create_and_get_relation(self, auth_client, test_project, db):
        """Create two active memories, link them, then fetch the relation."""
        from sqlalchemy import text
        pid, pcode = test_project
        mem_a_id = uuid4()
        mem_b_id = uuid4()

        for mid, title in [(mem_a_id, "Relation A"), (mem_b_id, "Relation B")]:
            db.execute(
                text(
                    "INSERT INTO memories (memory_id, project_id, canonical_key, "
                    "title, memory_text, current_version, sensitivity_level, status, "
                    "activated_by_review_item_id, created_at, updated_at) "
                    "VALUES (:mid, :pid, :ckey, :title, :mtext, 1, 'normal', 'active', "
                    ":arid, datetime('now'), datetime('now'))"
                ),
                {
                    "mid": str(mid),
                    "pid": str(pid),
                    "ckey": f"{pcode}-mem-{mid.hex[:4]}",
                    "title": title,
                    "mtext": f"Content for {title}",
                    "arid": str(uuid4()),
                },
            )
        db.commit()

        resp = auth_client.post(
            "/api/v4/memory/relations",
            json={
                "from_memory_id": str(mem_a_id),
                "to_memory_id": str(mem_b_id),
                "relation_type": "references",
                "reason": "test relation",
            },
            headers=_idem_headers(),
        )
        assert resp.status_code == 201, f"Create relation failed: {resp.json()}"
        relation_id = resp.json()["data"]["memory_relation_id"]

        resp = auth_client.get(f"/api/v4/memory/relations/{relation_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["relation_type"] == "references"

    def test_get_relation_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/memory/relations/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.9 — Conversations
# ═══════════════════════════════════════════════════════════════════════════


class TestConversations:
    """Conversation endpoints."""

    def test_list_conversations_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/conversations")
        assert resp.status_code == 200

    def test_get_conversation_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/conversations/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.10 — Messages
# ═══════════════════════════════════════════════════════════════════════════


class TestMessages:
    """Message endpoints."""

    def _create_conversation(self, auth_client, test_project) -> str:
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/conversations",
            json={
                "project_id": str(pid),
                "source_platform": "test",
                "conversation_type": "chat",
            },
            headers=_idem_headers(),
        )
        assert resp.status_code in (200, 201), f"Create conversation failed: {resp.json()}"
        return resp.json()["data"]["conversation_id"]

    def test_list_messages_returns_paginated(self, auth_client, test_project):
        conv_id = self._create_conversation(auth_client, test_project)
        resp = auth_client.get(f"/api/v4/conversations/{conv_id}/messages")
        assert resp.status_code == 200

    def test_get_message_not_found(self, auth_client, test_project):
        conv_id = self._create_conversation(auth_client, test_project)
        resp = auth_client.get(f"/api/v4/conversations/{conv_id}/messages/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.11 — Raw Events
# ═══════════════════════════════════════════════════════════════════════════


class TestRawEvents:
    """Raw event endpoints."""

    def test_list_raw_events_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/raw-events")
        assert resp.status_code == 200

    def test_get_raw_event_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/raw-events/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R3.12 — Memory Extract
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryExtract:
    """Memory extract pipeline trigger."""

    def test_extract_requires_idempotency_key(self, auth_client):
        resp = auth_client.post(
            "/api/v4/memory/extract",
            json={
                "source_type": "message",
                "source_id": str(uuid4()),
                "project_id": str(uuid4()),
            },
        )
        assert resp.status_code == 400

    def test_extract_source_not_found(self, auth_client):
        resp = auth_client.post(
            "/api/v4/memory/extract",
            json={
                "source_type": "message",
                "source_id": str(uuid4()),
                "project_id": str(uuid4()),
            },
            headers=_idem_headers(),
        )
        assert resp.status_code in (404, 400, 422)
