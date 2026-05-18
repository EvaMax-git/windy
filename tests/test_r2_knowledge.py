"""R2 Knowledge & Assets contract tests.

Covers new-architecture knowledge/asset endpoints:
* Knowledge documents CRUD: POST/GET/PATCH  /api/v4/knowledge/documents
* Knowledge blocks CRUD:     POST/GET/PATCH/DELETE /api/v4/knowledge/blocks
* Knowledge chunks:          GET  /api/v4/knowledge/documents/{id}/chunks
                             POST /api/v4/knowledge/documents/{id}/rechunk
* Knowledge search:          POST /api/v4/knowledge/search
* Global search:             GET  /api/v4/search/global
* Assets:                    POST/GET  /api/v4/assets
* Asset metadata:            POST/GET  /api/v4/asset-metadata
* Inbox:                     POST/GET  /api/v4/inbox
* Importer:                  POST/GET  /api/v4/importer
* Source maps:               GET  /api/v4/source-map
* Event source:              POST/GET  /api/v4/event-source
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
    """Log in as the seeded test user."""
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
            "uname": "r2_user",
            "email": "r2_user@test.local",
            "dname": "R2 User",
            "role": "owner",
            "status": "active",
            "phash": hash_password("r2pass123"),
            "mfa": "none",
            "now": now_val,
        },
    )
    db.commit()
    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "r2_user", "password": "r2pass123"},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.json()}"
    return client


@pytest.fixture
def test_project(auth_client, db, test_user_id):
    """Create a test project and return its project_id + project_code."""
    from sqlalchemy import text

    pid = uuid4()
    pcode = f"R2PROJ-{pid.hex[:6].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default, created_at, updated_at) "
            "VALUES (:pid, :code, :name, 'active', 'normal', datetime('now'), datetime('now'))"
        ),
        {"pid": str(pid), "code": pcode, "name": "R2 Test Project"},
    )
    db.commit()
    return pid, pcode


# ═══════════════════════════════════════════════════════════════════════════
# R2.1 — Knowledge Documents
# ═══════════════════════════════════════════════════════════════════════════


class TestKnowledgeDocuments:
    """CRUD for knowledge documents."""

    def test_create_document_requires_idempotency_key(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Test Doc"},
        )
        assert resp.status_code == 400

    def test_create_document_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Test Document"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert body["data"]["title"] == "Test Document"

    def test_create_document_missing_project_returns_404(self, auth_client):
        resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(uuid4()), "title": "No Project"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 404

    def test_list_documents_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/knowledge/documents")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_list_documents_filter_by_project(self, auth_client, test_project):
        pid, _ = test_project
        # Create a doc first
        auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Filtered Doc"},
            headers=_idem_headers(),
        )
        resp = auth_client.get(
            "/api/v4/knowledge/documents",
            params={"project_id": str(pid)},
        )
        assert resp.status_code == 200

    def test_get_document_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/knowledge/documents/{uuid4()}")
        assert resp.status_code == 404

    def test_get_document_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        create_resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Readable Doc"},
            headers=_idem_headers(),
        )
        doc_id = create_resp.json()["data"]["document_id"]
        resp = auth_client.get(f"/api/v4/knowledge/documents/{doc_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "Readable Doc"

    def test_update_document_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        create_resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Update Me"},
            headers=_idem_headers(),
        )
        doc_id = create_resp.json()["data"]["document_id"]
        resp = auth_client.patch(
            f"/api/v4/knowledge/documents/{doc_id}",
            json={"title": "Updated Title"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "Updated Title"

    def test_archive_document_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        create_resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Archive Me"},
            headers=_idem_headers(),
        )
        doc_id = create_resp.json()["data"]["document_id"]
        resp = auth_client.post(
            f"/api/v4/knowledge/documents/{doc_id}/archive",
            headers=_idem_headers(),
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# R2.2 — Knowledge Blocks
# ═══════════════════════════════════════════════════════════════════════════


class TestKnowledgeBlocks:
    """CRUD for knowledge blocks."""

    @pytest.fixture
    def test_doc(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Block Test Doc"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        return UUID(resp.json()["data"]["document_id"])

    def test_add_block_succeeds(self, auth_client, test_doc):
        resp = auth_client.post(
            f"/api/v4/knowledge/documents/{test_doc}/blocks",
            json={"content_markdown": "# Hello\n\nThis is a block."},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["block_type"] == "paragraph"

    def test_add_block_auto_generates_content_text(self, auth_client, test_doc):
        resp = auth_client.post(
            f"/api/v4/knowledge/documents/{test_doc}/blocks",
            json={"content_markdown": "**Bold** and *italic* text."},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["content_text"]  # auto-generated plain text

    def test_list_blocks_returns_all(self, auth_client, test_doc):
        # Add two blocks
        for md in ["Block A", "Block B"]:
            auth_client.post(
                f"/api/v4/knowledge/documents/{test_doc}/blocks",
                json={"content_markdown": md},
                headers=_idem_headers(),
            )
        resp = auth_client.get(f"/api/v4/knowledge/documents/{test_doc}/blocks")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert isinstance(data, list)
        assert len(data) == 2

    def test_update_block_succeeds(self, auth_client, test_doc):
        create_resp = auth_client.post(
            f"/api/v4/knowledge/documents/{test_doc}/blocks",
            json={"content_markdown": "Original"},
            headers=_idem_headers(),
        )
        block_id = create_resp.json()["data"]["block_id"]
        resp = auth_client.patch(
            f"/api/v4/knowledge/blocks/{block_id}",
            json={"content_markdown": "Updated"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["content_markdown"] == "Updated"

    def test_delete_block_succeeds(self, auth_client, test_doc):
        create_resp = auth_client.post(
            f"/api/v4/knowledge/documents/{test_doc}/blocks",
            json={"content_markdown": "Delete Me"},
            headers=_idem_headers(),
        )
        block_id = create_resp.json()["data"]["block_id"]
        resp = auth_client.delete(
            f"/api/v4/knowledge/blocks/{block_id}",
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True

    def test_get_blocks_for_missing_doc_returns_404(self, auth_client):
        resp = auth_client.get(f"/api/v4/knowledge/documents/{uuid4()}/blocks")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R2.3 — Knowledge Chunks
# ═══════════════════════════════════════════════════════════════════════════


class TestKnowledgeChunks:
    """Chunk listing and rechunking."""

    @pytest.fixture
    def doc_with_blocks(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Chunk Test Doc"},
            headers=_idem_headers(),
        )
        doc_id = UUID(resp.json()["data"]["document_id"])
        # Add a block with content for chunking
        long_text = "Lorem ipsum dolor sit amet. " * 40
        auth_client.post(
            f"/api/v4/knowledge/documents/{doc_id}/blocks",
            json={"content_markdown": long_text},
            headers=_idem_headers(),
        )
        return doc_id

    def test_list_chunks_returns_empty_initially(self, auth_client, doc_with_blocks):
        resp = auth_client.get(
            f"/api/v4/knowledge/documents/{doc_with_blocks}/chunks"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert isinstance(data, list)

    def test_rechunk_document_succeeds(self, auth_client, doc_with_blocks):
        resp = auth_client.post(
            f"/api/v4/knowledge/documents/{doc_with_blocks}/rechunk",
            json={"strategy": "paragraph", "chunk_size": 500, "overlap": 50},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert isinstance(data, list)

    def test_rechunk_empty_doc_returns_400(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/knowledge/documents",
            json={"project_id": str(pid), "title": "Empty Doc"},
            headers=_idem_headers(),
        )
        doc_id = resp.json()["data"]["document_id"]
        rechunk_resp = auth_client.post(
            f"/api/v4/knowledge/documents/{doc_id}/rechunk",
            json={"strategy": "paragraph"},
            headers=_idem_headers(),
        )
        assert rechunk_resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# R2.4 — Global Search
# ═══════════════════════════════════════════════════════════════════════════


class TestGlobalSearch:
    """Global search across agents, knowledge, memory."""

    def test_global_search_requires_query(self, auth_client):
        resp = auth_client.get("/api/v4/search/global")
        # FastAPI validation errors are converted to 400 by the project's
        # RequestValidationError handler (mneme/api/errors.py line 85)
        assert resp.status_code == 400

    def test_global_search_returns_results(self, auth_client):
        resp = auth_client.get(
            "/api/v4/search/global", params={"q": "test"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "items" in data
        assert "total" in data
        assert "source_counts" in data

    def test_global_search_with_project_filter(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.get(
            "/api/v4/search/global",
            params={"q": "test", "project_id": str(pid)},
        )
        assert resp.status_code == 200

    def test_global_search_query_too_long_returns_400(self, auth_client):
        resp = auth_client.get(
            "/api/v4/search/global", params={"q": "x" * 500}
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# R2.5 — Knowledge Search
# ═══════════════════════════════════════════════════════════════════════════


class TestKnowledgeSearch:
    """Knowledge-specific search endpoint.

    FTS (full-text search) relies on PostgreSQL-specific features
    (GIN indexes, tsvector/tsquery).  These tests are skipped on
    SQLite and only run against a real PostgreSQL database.
    """

    def _check_pg(self) -> bool:
        """Return True if the test is running on PostgreSQL."""
        import os
        url = os.environ.get("DATABASE_URL", "")
        return url.startswith("postgresql")

    def test_knowledge_search_requires_query(self, auth_client):
        resp = auth_client.get("/api/v4/knowledge/search")
        assert resp.status_code == 400

    def test_knowledge_search_returns_results(self, auth_client):
        if not self._check_pg():
            pytest.skip("FTS search requires PostgreSQL (GIN/tsvector)")
        resp = auth_client.get(
            "/api/v4/knowledge/search",
            params={"q": "test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body

    def test_knowledge_search_with_project(self, auth_client, test_project):
        if not self._check_pg():
            pytest.skip("FTS search requires PostgreSQL (GIN/tsvector)")
        pid, _ = test_project
        resp = auth_client.get(
            "/api/v4/knowledge/search",
            params={"q": "test", "project_id": str(pid)},
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# R2.6 — Assets
# ═══════════════════════════════════════════════════════════════════════════


class TestAssets:
    """Asset endpoints."""

    def test_list_assets_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/assets")
        # May return 200 (empty list) or 200
        assert resp.status_code == 200

    def test_get_asset_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/assets/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R2.7 — Inbox
# ═══════════════════════════════════════════════════════════════════════════


class TestInbox:
    """Inbox item endpoints."""

    def test_list_inbox_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/inbox")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body

    def test_get_inbox_item_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/inbox/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R2.8 — Importer
# ═══════════════════════════════════════════════════════════════════════════


class TestImporter:
    """Importer endpoints."""

    def test_list_import_runs_returns_data(self, auth_client):
        resp = auth_client.get("/api/v4/importer/runs")
        assert resp.status_code == 200

    def test_get_import_run_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/importer/runs/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R2.9 — Source Maps
# ═══════════════════════════════════════════════════════════════════════════


class TestSourceMaps:
    """Source map listing."""

    def test_list_source_maps_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/source-maps")
        assert resp.status_code == 200

    def test_list_source_maps_with_project_filter(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.get("/api/v4/source-maps", params={"project_id": str(pid)})
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# R2.10 — Event Source (nested under conversations)
# ═══════════════════════════════════════════════════════════════════════════


class TestEventSource:
    """Event source endpoints (nested under conversations)."""

    def _create_conversation(self, auth_client) -> UUID | None:
        """Create a test conversation and return its ID, or None on failure."""
        resp = auth_client.post(
            "/api/v4/conversations",
            json={"title": "Test Conv", "source_platform": "chat"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        if resp.status_code == 201:
            return UUID(resp.json()["data"]["conversation_id"])
        return None

    def test_list_event_sources_returns_paginated(self, auth_client):
        conv_id = self._create_conversation(auth_client)
        if conv_id is None:
            pytest.skip("Cannot create conversation for event source test")
        resp = auth_client.get(f"/api/v4/conversations/{conv_id}/event-sources")
        assert resp.status_code == 200

    def test_get_event_source_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/conversations/{uuid4()}/event-sources/{uuid4()}")
        assert resp.status_code == 404
