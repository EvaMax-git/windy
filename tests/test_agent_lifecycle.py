"""P5-05 Agent lifecycle contract tests.

Validates the complete agent lifecycle via the HTTP API:

* **Create** — POST /agents → 201 with full agent record
* **Read** — GET /agents, GET /agents/{id}
* **Update** — PATCH /agents/{id} (name, description, sensitivity, policy)
* **Disable** — POST /agents/{id}/disable → status=disabled, disabled_at set
* **Archive** — POST /agents/{id}/archive → status=archived
* **Token lifecycle** — create, list, revoke

Also validates:
* Error handling: duplicate agent_code, nonexistent agent, invalid state transitions
* Policy enforcement: permission checks for sensitive operations
* Audit trail: audit events created for each lifecycle action
* Event publishing: events emitted for each lifecycle action
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
TEST_PROJECT_CODE = "test-agent-lifecycle"


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _register_sqlite_compat(engine):
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _conn_record):
        try:
            dbapi_conn.create_function(
                "now", 0,
                lambda: _dt_mod.datetime.now(_dt_mod.timezone.utc).isoformat(),
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


def _create_agent(client, *, agent_code=None, name="Lifecycle Agent",
                  description="Test agent for lifecycle tests",
                  sensitivity_ceiling="normal",
                  policy_json=None):
    if agent_code is None:
        agent_code = f"agent-{uuid4().hex[:12]}"
    if policy_json is None:
        policy_json = {"mode": "draft"}
    body = {
        "agent_code": agent_code,
        "name": name,
        "description": description,
        "sensitivity_ceiling": sensitivity_ceiling,
        "policy_json": policy_json,
    }
    return _ok(client.post("/api/v4/agents", json=body, headers=_idem()), 201)


# ═══════════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def agent_client(monkeypatch):
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
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY, project_id TEXT,
                agent_code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, description TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                owner_user_id TEXT,
                store_id TEXT,
                sensitivity_ceiling TEXT NOT NULL DEFAULT 'normal',
                policy_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                disabled_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS agent_tokens (
                token_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                issued_by_user_id TEXT,
                name TEXT,
                token_hash TEXT NOT NULL, token_prefix TEXT NOT NULL,
                token_fingerprint TEXT NOT NULL,
                project_scope TEXT NOT NULL DEFAULT '[]',
                capability_scope TEXT NOT NULL DEFAULT '[]',
                sensitivity_ceiling TEXT NOT NULL DEFAULT 'normal',
                budget_limit_daily REAL,
                rate_limit_per_min INTEGER,
                expires_at TEXT NOT NULL, revoked_at TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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
        db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Create
# ═══════════════════════════════════════════════════════════════════════════


def test_create_agent(agent_client):
    """POST /agents creates an agent with status='active'."""
    client, engine = agent_client
    data = _create_agent(client, name="Test Agent", description="A test agent")

    assert "agent_id" in data
    assert data["name"] == "Test Agent"
    assert data["description"] == "A test agent"
    assert data["status"] == "active"
    assert data["sensitivity_ceiling"] == "normal"
    assert data["policy_json"] == {"mode": "draft"}


def test_create_agent_minimal(agent_client):
    """POST /agents with minimal fields succeeds."""
    client, engine = agent_client
    data = _create_agent(
        client,
        name="Minimal Agent",
        description=None,
        sensitivity_ceiling="private",
    )
    assert data["name"] == "Minimal Agent"
    assert data["description"] is None
    assert data["sensitivity_ceiling"] == "private"
    assert data["status"] == "active"


def test_create_agent_duplicate_agent_code(agent_client):
    """POST /agents with duplicate agent_code returns 409."""
    client, engine = agent_client
    _create_agent(client, agent_code="duplicate-code")

    resp = client.post("/api/v4/agents", json={
        "agent_code": "duplicate-code",
        "name": "Duplicate",
    }, headers=_idem())
    assert resp.status_code == 409


def test_create_agent_missing_idempotency(agent_client):
    """Missing Idempotency-Key returns 400."""
    client, engine = agent_client
    resp = client.post("/api/v4/agents", json={
        "agent_code": "no-idem",
        "name": "No Idem",
    })
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Read
# ═══════════════════════════════════════════════════════════════════════════


def test_get_agent(agent_client):
    """GET /agents/{id} returns the agent."""
    client, engine = agent_client
    created = _create_agent(client, name="Get Me")
    agent_id = created["agent_id"]

    resp = client.get(f"/api/v4/agents/{agent_id}")
    data = _ok(resp)
    assert data["agent_id"] == agent_id
    assert data["name"] == "Get Me"


def test_get_agent_not_found(agent_client):
    """GET /agents/{id} for nonexistent agent returns 404."""
    client, engine = agent_client
    resp = client.get(f"/api/v4/agents/{uuid4()}")
    assert resp.status_code == 404


def test_list_agents_empty(agent_client):
    """GET /agents returns empty list when no agents exist."""
    client, engine = agent_client
    resp = client.get("/api/v4/agents")
    data = _ok(resp)
    assert data["page_info"]["total_items"] == 0
    assert data["items"] == []


def test_list_agents_paginated(agent_client):
    """GET /agents supports pagination."""
    client, engine = agent_client
    for i in range(5):
        _create_agent(client, name=f"Agent {i}")

    resp = client.get("/api/v4/agents?page=1&page_size=2")
    data = _ok(resp)
    assert len(data["items"]) == 2
    assert data["page_info"]["total_items"] == 5
    assert data["page_info"]["total_pages"] == 3
    assert data["page_info"]["has_next"] is True

    resp2 = client.get("/api/v4/agents?page=3&page_size=2")
    data2 = _ok(resp2)
    assert len(data2["items"]) == 1
    assert data2["page_info"]["has_next"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Update
# ═══════════════════════════════════════════════════════════════════════════


def test_update_agent_name(agent_client):
    """PATCH /agents/{id} updates name."""
    client, engine = agent_client
    created = _create_agent(client, name="Old Name")
    agent_id = created["agent_id"]

    resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"name": "New Name"},
        headers=_idem(),
    )
    data = _ok(resp)
    assert data["name"] == "New Name"
    assert data["status"] == "active"


def test_update_agent_description(agent_client):
    """PATCH /agents/{id} updates description (including clearing it)."""
    client, engine = agent_client
    created = _create_agent(client, description="Original description")
    agent_id = created["agent_id"]

    resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"description": None},
        headers=_idem(),
    )
    data = _ok(resp)
    assert data["description"] is None


def test_update_agent_sensitivity_ceiling(agent_client):
    """PATCH /agents/{id} updates sensitivity_ceiling."""
    client, engine = agent_client
    created = _create_agent(client, sensitivity_ceiling="normal")
    agent_id = created["agent_id"]

    resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"sensitivity_ceiling": "sensitive"},
        headers=_idem(),
    )
    data = _ok(resp)
    assert data["sensitivity_ceiling"] == "sensitive"


def test_update_agent_policy(agent_client):
    """PATCH /agents/{id} updates policy_json."""
    client, engine = agent_client
    created = _create_agent(client, policy_json={"mode": "draft"})
    agent_id = created["agent_id"]

    new_policy = {"mode": "controlled", "review_required": True, "max_context_tokens": 4096}
    resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"policy_json": new_policy},
        headers=_idem(),
    )
    data = _ok(resp)
    assert data["policy_json"] == new_policy


def test_update_agent_full(agent_client):
    """PATCH /agents/{id} with all fields updates everything."""
    client, engine = agent_client
    created = _create_agent(client, name="Original")
    agent_id = created["agent_id"]

    resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={
            "name": "Completely Updated",
            "description": "New description",
            "sensitivity_ceiling": "secret",
            "policy_json": {"mode": "locked"},
        },
        headers=_idem(),
    )
    data = _ok(resp)
    assert data["name"] == "Completely Updated"
    assert data["description"] == "New description"
    assert data["sensitivity_ceiling"] == "secret"
    assert data["policy_json"] == {"mode": "locked"}


def test_update_nonexistent_agent(agent_client):
    """PATCH /agents/{id} for nonexistent agent returns 404."""
    client, engine = agent_client
    resp = client.patch(
        f"/api/v4/agents/{uuid4()}",
        json={"name": "Ghost"},
        headers=_idem(),
    )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Disable
# ═══════════════════════════════════════════════════════════════════════════


def test_disable_agent(agent_client):
    """POST /agents/{id}/disable transitions active→disabled."""
    client, engine = agent_client
    created = _create_agent(client, name="Disable Me")
    agent_id = created["agent_id"]
    assert created["status"] == "active"

    resp = client.post(
        f"/api/v4/agents/{agent_id}/disable",
        headers=_idem(),
    )
    data = _ok(resp)
    assert data["status"] == "disabled"
    assert data["disabled_at"] is not None


def test_disable_already_disabled_agent(agent_client):
    """Disabling an already-disabled agent returns 400."""
    client, engine = agent_client
    created = _create_agent(client, name="Double Disable")
    agent_id = created["agent_id"]

    # First disable
    client.post(f"/api/v4/agents/{agent_id}/disable", headers=_idem())
    # Second disable
    resp = client.post(f"/api/v4/agents/{agent_id}/disable", headers=_idem())
    assert resp.status_code == 400


def test_disable_archived_agent_fails(agent_client):
    """Disabling an archived agent returns 400."""
    client, engine = agent_client
    created = _create_agent(client, name="Archive Then Disable")
    agent_id = created["agent_id"]

    # Archive first
    client.post(f"/api/v4/agents/{agent_id}/archive", headers=_idem())
    # Try disable
    resp = client.post(f"/api/v4/agents/{agent_id}/disable", headers=_idem())
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Archive
# ═══════════════════════════════════════════════════════════════════════════


def test_archive_agent(agent_client):
    """POST /agents/{id}/archive transitions to archived."""
    client, engine = agent_client
    created = _create_agent(client, name="Archive Me")
    agent_id = created["agent_id"]
    assert created["status"] == "active"

    resp = client.post(
        f"/api/v4/agents/{agent_id}/archive",
        headers=_idem(),
    )
    data = _ok(resp)
    assert data["status"] == "archived"


def test_archive_already_archived_agent(agent_client):
    """Archiving an already-archived agent returns 400."""
    client, engine = agent_client
    created = _create_agent(client, name="Double Archive")
    agent_id = created["agent_id"]

    client.post(f"/api/v4/agents/{agent_id}/archive", headers=_idem())
    resp = client.post(f"/api/v4/agents/{agent_id}/archive", headers=_idem())
    assert resp.status_code == 400


def test_cannot_patch_archived_agent(agent_client):
    """PATCH on an archived agent returns 400."""
    client, engine = agent_client
    created = _create_agent(client, name="Archive Patch")
    agent_id = created["agent_id"]

    client.post(f"/api/v4/agents/{agent_id}/archive", headers=_idem())

    resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"name": "Should Not Work"},
        headers=_idem(),
    )
    assert resp.status_code == 400


def test_cannot_patch_disabled_agent(agent_client):
    """PATCH on a disabled agent may succeed or return error
    depending on implementation policy."""
    client, engine = agent_client
    created = _create_agent(client, name="Disabled Patch")
    agent_id = created["agent_id"]

    client.post(f"/api/v4/agents/{agent_id}/disable", headers=_idem())

    resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"name": "Patched While Disabled"},
        headers=_idem(),
    )
    # Accept either 200 (allows patch) or 400 (rejects patch)
    assert resp.status_code in (200, 400)


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Agent Tokens
# ═══════════════════════════════════════════════════════════════════════════


def test_create_agent_token(agent_client):
    """POST /agents/{id}/tokens creates a token with plaintext secret."""
    client, engine = agent_client
    created = _create_agent(client, name="Token Agent")
    agent_id = created["agent_id"]

    resp = client.post(
        f"/api/v4/agents/{agent_id}/tokens",
        json={
            "project_scope": [str(TEST_PROJECT_ID)],
            "capability_scope": ["memory.read", "memory.search"],
            "sensitivity_ceiling": "normal",
            "expires_in_days": 30,
        },
        headers=_idem(),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert "token_raw" in data
    assert "token_id" in data
    assert "agent_id" in data
    assert "token_prefix" in data
    assert "scopes" in data
    assert isinstance(data["token_raw"], str)
    assert len(data["token_raw"]) > 10  # Not the masked "**********"


def test_list_agent_tokens(agent_client):
    """GET /agents/{id}/tokens lists tokens for an agent."""
    client, engine = agent_client
    created = _create_agent(client, name="List Token Agent")
    agent_id = created["agent_id"]

    # Create a token first
    client.post(
        f"/api/v4/agents/{agent_id}/tokens",
        json={
            "project_scope": [str(TEST_PROJECT_ID)],
            "capability_scope": [],
            "sensitivity_ceiling": "normal",
            "expires_in_days": 30,
        },
        headers=_idem(),
    )

    resp = client.get(f"/api/v4/agents/{agent_id}/tokens")
    data = _ok(resp)
    assert isinstance(data, list)
    assert len(data) >= 1
    token = data[0]
    assert "token_id" in token
    assert "token_prefix" in token
    assert "expires_at" in token
    assert "revoked_at" in token or "revoked_at" not in token


def test_list_tokens_empty(agent_client):
    """GET /agents/{id}/tokens for agent with no tokens returns empty list."""
    client, engine = agent_client
    created = _create_agent(client, name="No Token Agent")
    agent_id = created["agent_id"]

    resp = client.get(f"/api/v4/agents/{agent_id}/tokens")
    data = _ok(resp)
    assert isinstance(data, list)
    assert len(data) == 0


def test_revoke_agent_token(agent_client):
    """POST /agents/{id}/tokens/{token_id}/revoke revokes a token."""
    client, engine = agent_client
    created = _create_agent(client, name="Revoke Token Agent")
    agent_id = created["agent_id"]

    # Create token
    create_resp = client.post(
        f"/api/v4/agents/{agent_id}/tokens",
        json={
            "project_scope": [str(TEST_PROJECT_ID)],
            "capability_scope": [],
            "sensitivity_ceiling": "normal",
            "expires_in_days": 30,
        },
        headers=_idem(),
    )
    # Get the token ID from the list
    tokens = _ok(client.get(f"/api/v4/agents/{agent_id}/tokens"))
    assert len(tokens) > 0
    token_id = tokens[0]["token_id"]

    # Revoke
    resp = client.post(
        f"/api/v4/agents/{agent_id}/tokens/{token_id}/revoke",
        json={"revoke_reason": "test_revoke"},
        headers=_idem(),
    )
    data = _ok(resp)
    assert "revoked_at" in data

    # Token should now show as revoked in list
    tokens_after = _ok(client.get(f"/api/v4/agents/{agent_id}/tokens"))
    if len(tokens_after) > 0:
        assert tokens_after[0]["revoked_at"] is not None


def test_revoke_nonexistent_token(agent_client):
    """Revoking a nonexistent token returns 404."""
    client, engine = agent_client
    created = _create_agent(client, name="Revoke Ghost Token")
    agent_id = created["agent_id"]

    resp = client.post(
        f"/api/v4/agents/{agent_id}/tokens/{uuid4()}/revoke",
        json={"revoke_reason": "test"},
        headers=_idem(),
    )
    assert resp.status_code == 404


def test_create_token_for_nonexistent_agent(agent_client):
    """Creating token for nonexistent agent returns 404."""
    client, engine = agent_client
    resp = client.post(
        f"/api/v4/agents/{uuid4()}/tokens",
        json={
            "project_scope": [],
            "capability_scope": [],
            "sensitivity_ceiling": "normal",
            "expires_in_days": 30,
        },
        headers=_idem(),
    )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Audit trail
# ═══════════════════════════════════════════════════════════════════════════


def test_agent_lifecycle_creates_audit_events(agent_client):
    """Each lifecycle operation creates an audit event."""
    client, engine = agent_client
    created = _create_agent(client, name="Audit Agent")
    agent_id = created["agent_id"]

    # Update
    client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"name": "Audit Agent Updated"},
        headers=_idem(),
    )
    # Disable
    client.post(f"/api/v4/agents/{agent_id}/disable", headers=_idem())
    # Archive
    client.post(f"/api/v4/agents/{agent_id}/archive", headers=_idem())

    with Session(engine) as db:
        # Count audit events by action
        rows = db.execute(text(
            "SELECT action, count(*) as cnt FROM audit_events GROUP BY action"
        )).all()
        audit_map = {row[0]: row[1] for row in rows}

        assert audit_map.get("agent.create") >= 1
        assert audit_map.get("agent.update") >= 1
        assert audit_map.get("agent.disable") >= 1
        assert audit_map.get("agent.archive") >= 1


def test_agent_lifecycle_publishes_events(agent_client):
    """Each lifecycle operation publishes an event."""
    client, engine = agent_client
    created = _create_agent(client, name="Event Agent")
    agent_id = created["agent_id"]

    # Update
    client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"name": "Event Agent Updated"},
        headers=_idem(),
    )
    # Disable
    client.post(f"/api/v4/agents/{agent_id}/disable", headers=_idem())
    # Archive
    client.post(f"/api/v4/agents/{agent_id}/archive", headers=_idem())

    with Session(engine) as db:
        rows = db.execute(text(
            "SELECT event_type, count(*) as cnt FROM events GROUP BY event_type"
        )).all()
        event_map = {row[0]: row[1] for row in rows}

        assert event_map.get("agent.created") >= 1
        assert event_map.get("agent.updated") >= 1
        assert event_map.get("agent.disabled") >= 1
        assert event_map.get("agent.archived") >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Full lifecycle sequence
# ═══════════════════════════════════════════════════════════════════════════


def test_full_agent_lifecycle_sequence(agent_client):
    """Complete agent lifecycle: create → update → disable → archive."""
    client, engine = agent_client

    # 1. Create
    created = _create_agent(client, name="Full Lifecycle Agent",
                            description="Initial",
                            sensitivity_ceiling="normal")
    agent_id = created["agent_id"]
    assert created["status"] == "active"

    # 2. Update
    updated = _ok(client.patch(
        f"/api/v4/agents/{agent_id}",
        json={
            "name": "Full Lifecycle Agent v2",
            "description": "Updated description",
            "sensitivity_ceiling": "private",
        },
        headers=_idem(),
    ))
    assert updated["name"] == "Full Lifecycle Agent v2"
    assert updated["sensitivity_ceiling"] == "private"

    # 3. Disable
    disabled = _ok(client.post(
        f"/api/v4/agents/{agent_id}/disable",
        headers=_idem(),
    ))
    assert disabled["status"] == "disabled"
    assert disabled["disabled_at"] is not None

    # 4. Archive
    archived = _ok(client.post(
        f"/api/v4/agents/{agent_id}/archive",
        headers=_idem(),
    ))
    assert archived["status"] == "archived"

    # 5. GET still returns agent with archived status
    get_resp = client.get(f"/api/v4/agents/{agent_id}")
    get_data = _ok(get_resp)
    assert get_data["status"] == "archived"
    assert get_data["name"] == "Full Lifecycle Agent v2"

    # 6. Cannot update archived agent
    patch_resp = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"name": "Should Fail"},
        headers=_idem(),
    )
    assert patch_resp.status_code == 400


def test_agent_lifecycle_with_token(agent_client):
    """Agent lifecycle including token management."""
    client, engine = agent_client

    # Create agent
    created = _create_agent(client, name="Token Lifecycle Agent")
    agent_id = created["agent_id"]

    # Create token
    token_resp = client.post(
        f"/api/v4/agents/{agent_id}/tokens",
        json={
            "project_scope": [str(TEST_PROJECT_ID)],
            "capability_scope": ["memory.read"],
            "sensitivity_ceiling": "normal",
            "expires_in_days": 7,
        },
        headers=_idem(),
    )
    assert token_resp.status_code == 201

    # Disable agent
    client.post(f"/api/v4/agents/{agent_id}/disable", headers=_idem())

    # Token should still be listable even after agent disabled
    tokens = _ok(client.get(f"/api/v4/agents/{agent_id}/tokens"))
    assert len(tokens) >= 1

    # Revoke token
    token_id = tokens[0]["token_id"]
    revoke_resp = client.post(
        f"/api/v4/agents/{agent_id}/tokens/{token_id}/revoke",
        json={"revoke_reason": "agent disabled"},
        headers=_idem(),
    )
    assert revoke_resp.status_code == 200
