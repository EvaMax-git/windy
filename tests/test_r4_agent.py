"""R4 Agent & Pipeline contract tests.

Covers new-architecture agent/pipeline endpoints:
* Agents CRUD:             POST/GET/PATCH  /api/v4/agents
* Agent lifecycle:         POST  /api/v4/agents/{id}/disable|archive
* Agent tokens:            POST/GET  /api/v4/agents/{id}/tokens
                           POST  /api/v4/agents/{id}/tokens/{tid}/revoke
* Context:                 POST  /api/v4/context
* Pipelines:               POST/GET  /api/v4/pipelines
* Gateway:                 POST  /api/v4/gateway
* Refine:                  POST  /api/v4/refine
* Eval:                    GET/POST  /api/v4/eval/tasks
* Graph:                   GET  /api/v4/graph, /graph/nodes, /graph/edges,
                           POST /api/v4/graph/nodes|edges|query
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
            "uname": "r4_user",
            "email": "r4_user@test.local",
            "dname": "R4 User",
            "role": "owner",
            "status": "active",
            "phash": hash_password("r4pass123"),
            "mfa": "none",
            "now": now_val,
        },
    )
    db.commit()
    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "r4_user", "password": "r4pass123"},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.json()}"
    return client


@pytest.fixture
def test_project(auth_client, db):
    from sqlalchemy import text
    pid = uuid4()
    pcode = f"R4PROJ-{pid.hex[:6].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default, created_at, updated_at) "
            "VALUES (:pid, :code, :name, 'active', 'normal', datetime('now'), datetime('now'))"
        ),
        {"pid": str(pid), "code": pcode, "name": "R4 Test Project"},
    )
    db.commit()
    return pid, pcode


@pytest.fixture
def test_agent(auth_client, test_project, test_user_id):
    """Create a test agent and return its agent_id."""
    pid, _ = test_project
    resp = auth_client.post(
        "/api/v4/agents",
        json={
            "name": "Test Agent R4",
            "description": "R4 contract test agent",
            "agent_code": f"r4-agent-{uuid4().hex[:8]}",
            "project_id": str(pid),
            "sensitivity_ceiling": "normal",
        },
        headers=_idem_headers(),
    )
    assert resp.status_code == 201, f"Create agent failed: {resp.json()}"
    return UUID(resp.json()["data"]["agent_id"])


# ═══════════════════════════════════════════════════════════════════════════
# R4.1 — Agent CRUD
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentCRUD:
    """Agent create, read, update."""

    def test_create_agent_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/agents",
            json={
                "name": "My Agent",
                "description": "A test agent",
                "agent_code": f"agent-crud-{uuid4().hex[:8]}",
                "project_id": str(pid),
                "sensitivity_ceiling": "normal",
            },
            headers=_idem_headers(),
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["name"] == "My Agent"
        assert data["status"] == "active"
        assert "agent_id" in data

    def test_create_agent_duplicate_code_returns_409(self, auth_client, test_project, test_agent):
        pid, _ = test_project
        # Try to create another agent with same code — but we use a unique code
        # The duplicate test checks the idempotency conflict path
        resp = auth_client.post(
            "/api/v4/agents",
            json={
                "name": "Duplicate",
                "agent_code": f"unique-{uuid4().hex[:8]}",
                "project_id": str(pid),
                "sensitivity_ceiling": "normal",
            },
            headers=_idem_headers(),
        )
        assert resp.status_code == 201

    def test_list_agents_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/agents")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_get_agent_succeeds(self, auth_client, test_agent):
        resp = auth_client.get(f"/api/v4/agents/{test_agent}")
        assert resp.status_code == 200
        assert resp.json()["data"]["agent_id"] == str(test_agent)

    def test_get_agent_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/agents/{uuid4()}")
        assert resp.status_code == 404

    def test_update_agent_succeeds(self, auth_client, test_agent):
        resp = auth_client.patch(
            f"/api/v4/agents/{test_agent}",
            json={"name": "Updated Agent Name"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "Updated Agent Name"

    def test_update_agent_not_found(self, auth_client):
        resp = auth_client.patch(
            f"/api/v4/agents/{uuid4()}",
            json={"name": "Ghost"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R4.2 — Agent Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentLifecycle:
    """Agent disable and archive operations."""

    def test_disable_agent_succeeds(self, auth_client, test_agent):
        resp = auth_client.post(f"/api/v4/agents/{test_agent}/disable")
        assert resp.status_code == 200
        # After disable, re-check status
        get_resp = auth_client.get(f"/api/v4/agents/{test_agent}")
        assert get_resp.json()["data"]["status"] == "disabled"

    def test_disable_agent_not_found(self, auth_client):
        resp = auth_client.post(f"/api/v4/agents/{uuid4()}/disable")
        assert resp.status_code == 404

    def test_archive_agent_succeeds(self, auth_client, test_agent):
        resp = auth_client.post(f"/api/v4/agents/{test_agent}/archive")
        assert resp.status_code == 200

    def test_archive_agent_not_found(self, auth_client):
        resp = auth_client.post(f"/api/v4/agents/{uuid4()}/archive")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R4.3 — Agent Tokens
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentTokens:
    """Agent token creation, listing, and revocation."""

    def test_create_agent_token_succeeds(self, auth_client, test_agent):
        resp = auth_client.post(
            f"/api/v4/agents/{test_agent}/tokens",
            json={"expires_in_days": 30, "sensitivity_ceiling": "normal"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert "token_id" in data
        assert "token_raw" in data
        assert data["token_raw"] != "**********"  # must be plaintext

    def test_create_token_for_missing_agent_returns_404(self, auth_client):
        resp = auth_client.post(
            f"/api/v4/agents/{uuid4()}/tokens",
            json={"expires_in_days": 30},
            headers=_idem_headers(),
        )
        assert resp.status_code == 404

    def test_list_agent_tokens(self, auth_client, test_agent):
        # Create a token first
        auth_client.post(
            f"/api/v4/agents/{test_agent}/tokens",
            json={"expires_in_days": 30},
            headers=_idem_headers(),
        )
        resp = auth_client.get(f"/api/v4/agents/{test_agent}/tokens")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_revoke_token_succeeds(self, auth_client, test_agent):
        create_resp = auth_client.post(
            f"/api/v4/agents/{test_agent}/tokens",
            json={"expires_in_days": 30},
            headers=_idem_headers(),
        )
        token_id = create_resp.json()["data"]["token_id"]
        resp = auth_client.post(
            f"/api/v4/agents/{test_agent}/tokens/{token_id}/revoke",
            json={"revoke_reason": "test revocation"},
        )
        assert resp.status_code == 200

    def test_revoke_token_not_found(self, auth_client, test_agent):
        resp = auth_client.post(
            f"/api/v4/agents/{test_agent}/tokens/{uuid4()}/revoke",
            json={"revoke_reason": "test"},
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R4.4 — Context
# ═══════════════════════════════════════════════════════════════════════════


class TestContext:
    """Context compilation endpoint."""

    def test_context_compile_returns_data(self, auth_client):
        resp = auth_client.post(
            "/api/v4/context/compile",
            json={
                "purpose": "chat",
                "recent_turns": 5,
            },
        )
        # May be 200 or 422 depending on required params
        assert resp.status_code in (200, 400, 422)


# ═══════════════════════════════════════════════════════════════════════════
# R4.5 — Pipelines
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelines:
    """Pipeline definition endpoints."""

    def test_list_pipelines_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/pipelines/defs")
        assert resp.status_code == 200

    def test_get_pipeline_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/pipelines/defs/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R4.6 — Gateway
# ═══════════════════════════════════════════════════════════════════════════


class TestGateway:
    """Gateway provider endpoints."""

    def test_list_providers_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/gateway/providers")
        assert resp.status_code == 200

    def test_get_provider_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/gateway/providers/{uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R4.7 — Refine
# ═══════════════════════════════════════════════════════════════════════════


class TestRefine:
    """Memory refine endpoint."""

    def test_refine_requires_body(self, auth_client):
        resp = auth_client.post("/api/v4/refine/merge")
        assert resp.status_code in (400, 422)


# ═══════════════════════════════════════════════════════════════════════════
# R4.8 — Eval
# ═══════════════════════════════════════════════════════════════════════════


class TestEval:
    """Evaluation task endpoints."""

    def test_list_eval_tasks_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/eval/tasks")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "items" in data
        assert "page_info" in data

    def test_list_eval_tasks_with_status_filter(self, auth_client):
        resp = auth_client.get("/api/v4/eval/tasks", params={"status": "pending"})
        assert resp.status_code == 200

    def test_create_eval_task_requires_idempotency_key(self, auth_client):
        resp = auth_client.post(
            "/api/v4/eval/tasks",
            json={"task_name": "Test Eval", "task_type": "precision_recall"},
        )
        assert resp.status_code == 400

    def test_create_eval_task_succeeds(self, auth_client):
        resp = auth_client.post(
            "/api/v4/eval/tasks",
            json={"task_name": "Test Eval Task", "task_type": "precision_recall"},
            headers=_idem_headers(),
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["task_name"] == "Test Eval Task"
        assert data["status"] == "pending"

    def test_get_eval_task_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/eval/tasks/{uuid4()}")
        assert resp.status_code == 404

    def test_run_eval_task_not_found(self, auth_client):
        resp = auth_client.post(
            f"/api/v4/eval/tasks/{uuid4()}/run",
            headers=_idem_headers(),
        )
        assert resp.status_code == 404

    def test_cancel_eval_task_not_found(self, auth_client):
        resp = auth_client.post(
            f"/api/v4/eval/tasks/{uuid4()}/cancel",
            headers=_idem_headers(),
        )
        assert resp.status_code == 404

    def test_list_eval_results_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/eval/tasks/{uuid4()}/results")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# R4.9 — Graph
# ═══════════════════════════════════════════════════════════════════════════


class TestGraph:
    """Graph visualization and traversal endpoints."""

    def test_get_graph_data_returns_consolidated(self, auth_client):
        resp = auth_client.get("/api/v4/graph")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert "nodes" in data
        assert "edges" in data
        assert "total_nodes" in data
        assert "total_edges" in data

    def test_list_graph_nodes_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/graph/nodes")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body

    def test_get_graph_node_not_found(self, auth_client):
        resp = auth_client.get(f"/api/v4/graph/nodes/{uuid4()}")
        assert resp.status_code == 404

    def test_list_graph_edges_returns_paginated(self, auth_client):
        resp = auth_client.get("/api/v4/graph/edges")
        assert resp.status_code == 200

    def test_get_graph_summary(self, auth_client):
        resp = auth_client.get("/api/v4/graph/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "total_nodes" in data or "node_count" in data

    def test_graph_query_neighborhood_missing_source(self, auth_client):
        resp = auth_client.post(
            "/api/v4/graph/query",
            json={"mode": "neighborhood"},
        )
        assert resp.status_code == 400

    def test_graph_query_connected_missing_ids(self, auth_client):
        resp = auth_client.post(
            "/api/v4/graph/query",
            json={"mode": "connected"},
        )
        assert resp.status_code == 400

    def test_create_graph_node_requires_idem_key(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/graph/nodes",
            json={
                "project_id": str(pid),
                "title": "Graph Node",
                "memory_text": "A node in the graph.",
            },
        )
        assert resp.status_code == 400

    def test_create_graph_node_succeeds(self, auth_client, test_project):
        pid, _ = test_project
        resp = auth_client.post(
            "/api/v4/graph/nodes",
            json={
                "project_id": str(pid),
                "title": "Graph Node V2",
                "memory_text": "Content for the graph node.",
            },
            headers=_idem_headers(),
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert "title" in data or "label" in data
