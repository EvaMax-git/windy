from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import install_request_context_middleware
from mneme.api.errors import install_exception_handlers
from mneme.api.routes.agent.agents import router as agents_router
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.schemas.auth import UserRead, UserSessionRead


@pytest.fixture
def agents_client(db: Session, test_user_id: UUID):
    app = FastAPI()
    install_request_context_middleware(app)
    install_exception_handlers(app)
    app.include_router(agents_router, prefix="/api/v4")

    now = datetime.now(timezone.utc)
    auth = AuthenticatedSession(
        user=UserRead(
            user_id=test_user_id,
            username="test_integration_user",
            email="test@integration.local",
            display_name="Integration Test User",
            role_code="owner",
            status="active",
            mfa_mode="none",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            last_login_at=None,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        ),
        session=UserSessionRead(
            session_id=uuid4(),
            user_id=test_user_id,
            session_token_prefix="test-session",
            auth_method="password",
            device_label="pytest",
            step_up_verified_at=None,
            last_seen_at=now,
            expires_at=now + timedelta(hours=1),
            revoked_at=None,
            revoke_reason=None,
            created_at=now,
            updated_at=now,
        ),
        session_token_hash="test-hash",
    )

    def override_get_db():
        yield db

    def override_auth():
        return auth

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_session] = override_auth

    with TestClient(app) as client:
        yield client, db

    app.dependency_overrides.clear()


def _create_agent(client: TestClient) -> dict:
    response = client.post(
        "/api/v4/agents",
        json={
            "agent_code": f"agent-{uuid4().hex[:12]}",
            "name": "Lifecycle Agent",
            "description": "initial",
            "sensitivity_ceiling": "normal",
            "policy_json": {"mode": "draft"},
        },
        headers={"Idempotency-Key": f"create-{uuid4()}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _count(db: Session, table: str, column: str, value: str, **extra) -> int:
    query = f"SELECT count(*) FROM {table} WHERE {column} = :value"
    params = {"value": value}
    for k, v in extra.items():
        query += f" AND {k} = :{k}"
        params[k] = v
    return db.execute(text(query), params).scalar_one()


def test_agent_patch_disable_archive_contract(agents_client):
    client, db = agents_client
    agent = _create_agent(client)
    agent_id = agent["agent_id"]

    patch_response = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={
            "name": "Lifecycle Agent Updated",
            "description": None,
            "sensitivity_ceiling": "private",
            "policy_json": {"mode": "controlled", "review_required": True},
        },
        headers={"Idempotency-Key": f"update-{uuid4()}"},
    )
    assert patch_response.status_code == 200, patch_response.text
    patched = patch_response.json()["data"]
    assert patched["name"] == "Lifecycle Agent Updated"
    assert patched["description"] is None
    assert patched["sensitivity_ceiling"] == "private"
    assert patched["policy_json"] == {"mode": "controlled", "review_required": True}
    assert patched["status"] == "active"

    disable_response = client.post(
        f"/api/v4/agents/{agent_id}/disable",
        headers={"Idempotency-Key": f"disable-{uuid4()}"},
    )
    assert disable_response.status_code == 200, disable_response.text
    disabled = disable_response.json()["data"]
    assert disabled["status"] == "disabled"
    assert disabled["disabled_at"] is not None

    archive_response = client.post(
        f"/api/v4/agents/{agent_id}/archive",
        headers={"Idempotency-Key": f"archive-{uuid4()}"},
    )
    assert archive_response.status_code == 200, archive_response.text
    archived = archive_response.json()["data"]
    assert archived["status"] == "archived"
    assert archived["disabled_at"] is not None

    assert _count(db, "audit_events", "action", "agent.update", object_id=agent_id) == 1
    assert _count(db, "audit_events", "action", "agent.disable", object_id=agent_id) == 1
    assert _count(db, "audit_events", "action", "agent.archive", object_id=agent_id) == 1
    assert _count(db, "events", "event_type", "agent.updated", aggregate_id=agent_id) == 1
    assert _count(db, "events", "event_type", "agent.disabled", aggregate_id=agent_id) == 1
    assert _count(db, "events", "event_type", "agent.archived", aggregate_id=agent_id) == 1


def test_archived_agent_cannot_be_patched(agents_client):
    client, _db = agents_client
    agent = _create_agent(client)
    agent_id = agent["agent_id"]

    archive_response = client.post(
        f"/api/v4/agents/{agent_id}/archive",
        headers={"Idempotency-Key": f"archive-{uuid4()}"},
    )
    assert archive_response.status_code == 200, archive_response.text

    patch_response = client.patch(
        f"/api/v4/agents/{agent_id}",
        json={"name": "Should Not Change"},
        headers={"Idempotency-Key": f"update-{uuid4()}"},
    )
    assert patch_response.status_code == 400
    assert patch_response.json()["error"]["code"] == "bad_request"
