from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.config import get_settings
from mneme.db.base import get_db
from mneme.main import create_app
from mneme.security import hash_password, hash_session_token


@pytest.fixture
def auth_client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MNEME_SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _create_auth_tables(engine)
    user_id = uuid4()
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.execute(
            text(
                """
                INSERT INTO users (
                  user_id,
                  username,
                  email,
                  display_name,
                  role_code,
                  status,
                  password_hash,
                  mfa_mode,
                  locale,
                  timezone,
                  created_at,
                  updated_at
                )
                VALUES (
                  :user_id,
                  'owner',
                  'owner@example.com',
                  'Owner',
                  'owner',
                  'active',
                  :password_hash,
                  'none',
                  'zh-CN',
                  'Asia/Shanghai',
                  :now,
                  :now
                )
                """
            ),
            {
                "user_id": user_id.hex,
                "password_hash": hash_password("correct-password"),
                "now": now,
            },
        )
        db.commit()

    app = create_app()

    def override_get_db():
        db = Session(engine, expire_on_commit=False)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        yield client, engine


def _create_auth_tables(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE users (
                  user_id TEXT PRIMARY KEY,
                  username TEXT NOT NULL UNIQUE,
                  email TEXT UNIQUE,
                  display_name TEXT NOT NULL,
                  role_code TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending_bootstrap',
                  password_hash TEXT NOT NULL,
                  mfa_mode TEXT NOT NULL DEFAULT 'none',
                  locale TEXT NOT NULL DEFAULT 'zh-CN',
                  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                  last_login_at TIMESTAMP,
                  disabled_at TIMESTAMP,
                  created_at TIMESTAMP NOT NULL,
                  updated_at TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE user_sessions (
                  session_id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  session_token_hash TEXT NOT NULL UNIQUE,
                  session_token_prefix TEXT NOT NULL,
                  auth_method TEXT NOT NULL DEFAULT 'password',
                  device_label TEXT,
                  device_fingerprint TEXT,
                  ip_hash TEXT,
                  user_agent TEXT,
                  step_up_verified_at TIMESTAMP,
                  last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  expires_at TIMESTAMP NOT NULL,
                  revoked_at TIMESTAMP,
                  revoke_reason TEXT,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE audit_events (
                  audit_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                  occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  actor_type TEXT NOT NULL,
                  actor_id TEXT,
                  auth_context_type TEXT,
                  auth_context_id TEXT,
                  action TEXT NOT NULL,
                  object_type TEXT,
                  object_id TEXT,
                  project_id TEXT,
                  result TEXT NOT NULL,
                  reason_code TEXT,
                  sensitivity_level TEXT NOT NULL DEFAULT 'normal',
                  correlation_id TEXT NOT NULL,
                  request_id TEXT NOT NULL,
                  review_item_id TEXT,
                  diff_summary TEXT NOT NULL DEFAULT '{}',
                  metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
        )


def test_login_creates_session_audit_and_returns_cookie(auth_client) -> None:
    client, engine = auth_client

    response = client.post(
        "/api/v4/auth/login",
        json={"username": "owner", "password": "correct-password", "device_label": "test"},
    )

    assert response.status_code == 200
    body = response.json()
    cookie_token = response.cookies.get("mneme_session")
    assert cookie_token, "session cookie must be set"
    assert "session_token" not in body.get("data", {}), "session_token must not appear in JSON body"
    assert body["data"]["user"]["username"] == "owner"
    session_prefix = body["data"]["session"]["session_token_prefix"]
    assert session_prefix == cookie_token[:12]
    assert "mneme_session" in response.cookies

    with Session(engine) as db:
        stored = db.execute(text("SELECT session_token_hash, session_token_prefix FROM user_sessions")).one()
        audit = db.execute(text("SELECT action, result FROM audit_events")).one()

    assert stored.session_token_hash == hash_session_token(cookie_token)
    assert stored.session_token_hash != cookie_token
    assert stored.session_token_prefix == cookie_token[:12]
    assert audit == ("auth.login", "success")


def test_failed_login_writes_failed_audit_without_session(auth_client) -> None:
    client, engine = auth_client

    response = client.post("/api/v4/auth/login", json={"username": "owner", "password": "wrong"})

    assert response.status_code == 401
    with Session(engine) as db:
        session_count = db.execute(text("SELECT count(*) FROM user_sessions")).scalar_one()
        audit = db.execute(text("SELECT action, result, reason_code FROM audit_events")).one()

    assert session_count == 0
    assert audit == ("auth.login", "failed", "invalid_credentials")


def test_me_accepts_session_cookie_and_updates_last_seen(auth_client) -> None:
    client, engine = auth_client
    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "owner", "password": "correct-password"},
    )
    assert login_resp.status_code == 200

    with Session(engine) as db:
        db.execute(
            text("UPDATE user_sessions SET last_seen_at = :old"),
            {"old": datetime.now(timezone.utc) - timedelta(hours=1)},
        )
        db.commit()

    # Session cookie is automatically sent by TestClient on subsequent requests
    response = client.get("/api/v4/auth/me")

    assert response.status_code == 200
    assert response.json()["data"]["user"]["username"] == "owner"
    with Session(engine) as db:
        last_seen_at = db.execute(text("SELECT last_seen_at FROM user_sessions")).scalar_one()

    parsed_last_seen_at = datetime.fromisoformat(str(last_seen_at))
    if parsed_last_seen_at.tzinfo is None:
        parsed_last_seen_at = parsed_last_seen_at.replace(tzinfo=timezone.utc)
    assert parsed_last_seen_at > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_logout_revokes_session_and_blocks_reuse(auth_client) -> None:
    client, engine = auth_client
    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "owner", "password": "correct-password"},
    )
    assert login_resp.status_code == 200

    response = client.post(
        "/api/v4/auth/logout",
        json={"revoke_reason": "test_logout"},
    )

    assert response.status_code == 200
    # Session cookie is cleared by logout; subsequent /me should fail
    assert client.get("/api/v4/auth/me").status_code == 401

    with Session(engine) as db:
        revoked = db.execute(text("SELECT revoked_at, revoke_reason FROM user_sessions")).one()
        audits = db.execute(text("SELECT action, result FROM audit_events ORDER BY occurred_at, action")).all()

    assert revoked.revoked_at is not None
    assert revoked.revoke_reason == "test_logout"
    assert ("auth.logout", "success") in audits


def test_expired_session_cannot_access_me(auth_client) -> None:
    client, engine = auth_client
    login_resp = client.post(
        "/api/v4/auth/login",
        json={"username": "owner", "password": "correct-password"},
    )
    assert login_resp.status_code == 200

    with Session(engine) as db:
        db.execute(
            text("UPDATE user_sessions SET expires_at = :expires_at"),
            {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        )
        db.commit()

    response = client.get("/api/v4/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"
