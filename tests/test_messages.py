"""P4-02 Messages contract tests.

Tests Event Source + Messages API endpoints:
* POST   /api/v4/conversations/{id}/event-sources
* GET    /api/v4/conversations/{id}/event-sources
* POST   /api/v4/conversations/{id}/messages
* POST   /api/v4/conversations/{id}/messages/batch
* GET    /api/v4/conversations/{id}/messages
* GET    /api/v4/conversations/{id}/messages/{mid}

Completion standard: 20+ contract tests.
"""

from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

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
from mneme.security import hash_password


TEST_USER_ID = uuid4()
TEST_PROJECT_ID = uuid4()
TEST_CONVERSATION_ID = uuid4()
TEST_CONVERSATION2_ID = uuid4()


@pytest.fixture
def messages_client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MNEME_SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _create_p4_tables(engine)
    _seed_p4_data(engine)

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


def _create_p4_tables(engine) -> None:
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
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY, project_id TEXT, owner_user_id TEXT,
                conversation_type TEXT NOT NULL DEFAULT 'chat', title TEXT,
                source_platform TEXT NOT NULL, sensitivity_level TEXT NOT NULL DEFAULT 'private',
                retention_days INTEGER, conversation_status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT, ended_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS event_source (
                event_source_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                source_platform TEXT NOT NULL, external_conversation_id TEXT,
                source_account_id TEXT, source_uri TEXT,
                participants_json TEXT NOT NULL DEFAULT '[]',
                time_range_start TEXT, time_range_end TEXT,
                import_run_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                event_source_id TEXT, parent_message_id TEXT,
                role_code TEXT NOT NULL, sender_label TEXT,
                content_text TEXT NOT NULL, content_markdown TEXT,
                content_hash TEXT NOT NULL, sensitivity_level TEXT NOT NULL DEFAULT 'private',
                pii_flags TEXT NOT NULL DEFAULT '[]',
                message_time TEXT NOT NULL, ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (event_source_id, content_hash, message_time)
            )
        """))


def _seed_p4_data(engine) -> None:
    user_hex = TEST_USER_ID.hex
    project_hex = TEST_PROJECT_ID.hex
    conv_hex = TEST_CONVERSATION_ID.hex
    conv2_hex = TEST_CONVERSATION2_ID.hex

    with Session(engine) as db:
        db.execute(text("""
            INSERT INTO users (user_id, username, email, display_name, role_code,
                               status, password_hash, mfa_mode)
            VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa)
        """), {
            "uid": user_hex, "uname": "test_user", "email": "test@test.local",
            "dname": "Test User", "role": "owner", "status": "active",
            "phash": hash_password("test-pass"), "mfa": "none",
        })
        db.execute(text("""
            INSERT INTO projects (project_id, project_code, name, status)
            VALUES (:pid, :code, :name, 'active')
        """), {"pid": project_hex, "code": "TEST-P4", "name": "Test Project P4"})
        db.execute(text("""
            INSERT INTO conversations (conversation_id, project_id, owner_user_id,
                conversation_type, source_platform, sensitivity_level, conversation_status)
            VALUES (:cid, :pid, :uid, 'chat', 'mneme_api', 'private', 'active')
        """), {"cid": conv_hex, "pid": project_hex, "uid": user_hex})
        db.execute(text("""
            INSERT INTO conversations (conversation_id, project_id, owner_user_id,
                conversation_type, source_platform, sensitivity_level, conversation_status)
            VALUES (:cid, :pid, :uid, 'chat', 'mneme_api', 'private', 'active')
        """), {"cid": conv2_hex, "pid": project_hex, "uid": user_hex})
        db.commit()


def _ik() -> dict:
    return {"Idempotency-Key": f"ik-{uuid4().hex[:12]}"}

def _u(cid: UUID = TEST_CONVERSATION_ID) -> str:
    return f"/api/v4/conversations/{cid}"


# ═══════════════════════════════════════════════
# Event Source tests
# ═══════════════════════════════════════════════

class TestEventSourceCreate:
    def test_create_normal(self, messages_client):
        client, _ = messages_client
        r = client.post(f"{_u()}/event-sources",
            json={"source_platform": "slack", "participants_json": [
                {"actor_type": "user", "actor_id": str(TEST_USER_ID), "actor_label": "A", "role_in_session": "participant"}
            ]}, headers=_ik())
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["source_platform"] == "slack"
        assert d["conversation_id"] == str(TEST_CONVERSATION_ID)

    def test_missing_ik_400(self, messages_client):
        client, _ = messages_client
        r = client.post(f"{_u()}/event-sources", json={"source_platform": "slack"})
        assert r.status_code == 400

    def test_nonexistent_conv_404(self, messages_client):
        client, _ = messages_client
        r = client.post(f"/api/v4/conversations/{uuid4()}/event-sources",
            json={"source_platform": "slack"}, headers=_ik())
        assert r.status_code == 404

    def test_with_all_fields(self, messages_client):
        client, _ = messages_client
        r = client.post(f"{_u()}/event-sources", json={
            "source_platform": "email", "external_conversation_id": "ext-1",
            "source_account_id": "acct-1", "source_uri": "https://x.com/t/1",
            "participants_json": [
                {"actor_type": "user", "actor_id": str(TEST_USER_ID), "actor_label": "B", "role_in_session": "participant"}
            ],
            "time_range_start": "2026-01-01T00:00:00Z",
            "time_range_end": "2026-01-01T01:00:00Z",
            "metadata_json": {"k": "v"}
        }, headers=_ik())
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["external_conversation_id"] == "ext-1"


class TestEventSourceList:
    def test_empty(self, messages_client):
        client, _ = messages_client
        r = client.get(f"{_u()}/event-sources")
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_with_entries(self, messages_client):
        client, _ = messages_client
        for i in range(2):
            client.post(f"{_u()}/event-sources", json={"source_platform": f"p{i}"}, headers=_ik())
        r = client.get(f"{_u()}/event-sources")
        assert r.status_code == 200
        assert len(r.json()["data"]) == 2

    def test_nonexistent_conv_404(self, messages_client):
        client, _ = messages_client
        r = client.get(f"/api/v4/conversations/{uuid4()}/event-sources")
        assert r.status_code == 404


# ═══════════════════════════════════════════════
# Message create tests
# ═══════════════════════════════════════════════

class TestMessageCreate:
    def test_normal(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc).isoformat()
        r = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "Hello test",
            "message_time": t}, headers=_ik())
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["content_text"] == "Hello test"
        assert len(d["content_hash"]) == 64

    def test_missing_ik_400(self, messages_client):
        client, _ = messages_client
        r = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "x",
            "message_time": datetime.now(timezone.utc).isoformat()})
        assert r.status_code == 400

    def test_nonexistent_conv_404(self, messages_client):
        client, _ = messages_client
        r = client.post(f"/api/v4/conversations/{uuid4()}/messages", json={
            "role_code": "user", "content_text": "x",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        assert r.status_code == 404

    def test_archived_conv_400(self, messages_client):
        client, engine = messages_client
        with Session(engine) as db:
            db.execute(text("UPDATE conversations SET conversation_status='archived', ended_at=datetime('now') WHERE conversation_id=:c"), {"c": TEST_CONVERSATION2_ID.hex})
            db.commit()
        r = client.post(f"{_u(TEST_CONVERSATION2_ID)}/messages", json={
            "role_code": "user", "content_text": "x",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        assert r.status_code == 400

    def test_deleted_conv_400(self, messages_client):
        client, engine = messages_client
        with Session(engine) as db:
            db.execute(text("UPDATE conversations SET conversation_status='deleted' WHERE conversation_id=:c"), {"c": TEST_CONVERSATION2_ID.hex})
            db.commit()
        r = client.post(f"{_u(TEST_CONVERSATION2_ID)}/messages", json={
            "role_code": "user", "content_text": "x",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        assert r.status_code == 400

    def test_content_hash_auto(self, messages_client):
        client, _ = messages_client
        content = "hash auto test"
        r = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": content,
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        assert r.status_code == 200
        assert r.json()["data"]["content_hash"] == hashlib.sha256(content.encode()).hexdigest()

    def test_pii_flags_empty(self, messages_client):
        client, _ = messages_client
        r = client.post(f"{_u()}/messages", json={
            "role_code": "assistant", "content_text": "pii",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        assert r.json()["data"]["pii_flags"] == []

    def test_started_at_auto_set(self, messages_client):
        client, engine = messages_client
        now = datetime.now(timezone.utc)
        r = client.post(f"{_u()}/messages", json={
            "role_code": "system", "content_text": "first",
            "message_time": now.isoformat()}, headers=_ik())
        assert r.status_code == 200
        with Session(engine) as db:
            row = db.execute(text("SELECT started_at FROM conversations WHERE conversation_id=:c"), {"c": TEST_CONVERSATION_ID.hex}).one()
            assert row[0] is not None

    def test_with_event_source(self, messages_client):
        client, _ = messages_client
        es = client.post(f"{_u()}/event-sources", json={"source_platform": "web"}, headers=_ik())
        assert es.status_code == 200
        es_id = es.json()["data"]["event_source_id"]
        r = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "with es",
            "message_time": datetime.now(timezone.utc).isoformat(),
            "event_source_id": es_id}, headers=_ik())
        assert r.status_code == 200
        assert r.json()["data"]["event_source_id"] == es_id

    def test_with_parent(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc)
        p1 = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "parent",
            "message_time": t.isoformat()}, headers=_ik())
        assert p1.status_code == 200
        pid = p1.json()["data"]["message_id"]
        p2 = client.post(f"{_u()}/messages", json={
            "role_code": "assistant", "content_text": "reply",
            "message_time": (t + timedelta(seconds=1)).isoformat(),
            "parent_message_id": pid}, headers=_ik())
        assert p2.status_code == 200
        assert p2.json()["data"]["parent_message_id"] == pid

    def test_dedup_same_content_time(self, messages_client):
        client, engine = messages_client
        t = datetime.now(timezone.utc)
        content = "dedup test"
        r1 = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": content,
            "message_time": t.isoformat()}, headers=_ik())
        assert r1.status_code == 200
        mid = r1.json()["data"]["message_id"]
        r2 = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": content,
            "message_time": t.isoformat()}, headers=_ik())
        assert r2.status_code == 200
        assert r2.json()["data"]["message_id"] == mid
        with Session(engine) as db:
            c = db.execute(text("SELECT count(*) FROM messages WHERE content_text=:t"), {"t": content}).scalar_one()
            assert c == 1

    def test_idempotency_replay(self, messages_client):
        client, _ = messages_client
        ik = f"replay-{uuid4().hex[:8]}"
        r1 = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "ik replay",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers={"Idempotency-Key": ik})
        assert r1.status_code == 200
        mid = r1.json()["data"]["message_id"]
        r2 = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "ik replay",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers={"Idempotency-Key": ik})
        assert r2.status_code == 200
        assert r2.json()["data"]["message_id"] == mid

    def test_all_roles(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc)
        for role in ["user", "assistant", "agent", "system", "tool", "other"]:
            r = client.post(f"{_u()}/messages", json={
                "role_code": role, "content_text": f"from {role}",
                "message_time": (t + timedelta(seconds=1)).isoformat()}, headers=_ik())
            assert r.status_code == 200
            assert r.json()["data"]["role_code"] == role

    def test_sender_label(self, messages_client):
        client, _ = messages_client
        r = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "with label",
            "message_time": datetime.now(timezone.utc).isoformat(),
            "sender_label": "John"}, headers=_ik())
        assert r.status_code == 200
        assert r.json()["data"]["sender_label"] == "John"

    def test_sensitivity_level(self, messages_client):
        client, _ = messages_client
        r = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "secret msg",
            "message_time": datetime.now(timezone.utc).isoformat(),
            "sensitivity_level": "secret"}, headers=_ik())
        assert r.status_code == 200
        assert r.json()["data"]["sensitivity_level"] == "secret"


# ═══════════════════════════════════════════════
# Message list tests
# ═══════════════════════════════════════════════

class TestMessageList:
    def test_empty(self, messages_client):
        client, _ = messages_client
        r = client.get(f"{_u()}/messages")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["items"] == []
        assert d["page_info"]["total_items"] == 0

    def test_with_messages_ordered(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc)
        for i in range(3):
            client.post(f"{_u()}/messages", json={
                "role_code": "user", "content_text": f"msg{i}",
                "message_time": (t + timedelta(seconds=i)).isoformat()}, headers=_ik())
        r = client.get(f"{_u()}/messages")
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        assert len(items) == 3
        assert items[0]["content_text"] == "msg0"
        assert items[2]["content_text"] == "msg2"

    def test_pagination(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc)
        for i in range(10):
            client.post(f"{_u()}/messages", json={
                "role_code": "user", "content_text": f"m{i:02d}",
                "message_time": (t + timedelta(seconds=i)).isoformat()}, headers=_ik())
        r1 = client.get(f"{_u()}/messages?page=1&page_size=3")
        assert r1.status_code == 200
        pi = r1.json()["data"]["page_info"]
        assert pi["total_items"] == 10
        assert pi["total_pages"] == 4
        assert pi["has_next"] is True
        assert pi["has_previous"] is False
        r4 = client.get(f"{_u()}/messages?page=4&page_size=3")
        assert r4.status_code == 200
        pi4 = r4.json()["data"]["page_info"]
        assert pi4["has_next"] is False
        assert len(r4.json()["data"]["items"]) == 1

    def test_nonexistent_conv_404(self, messages_client):
        client, _ = messages_client
        r = client.get(f"/api/v4/conversations/{uuid4()}/messages")
        assert r.status_code == 404


# ═══════════════════════════════════════════════
# Message detail tests
# ═══════════════════════════════════════════════

class TestMessageDetail:
    def test_normal(self, messages_client):
        client, _ = messages_client
        cr = client.post(f"{_u()}/messages", json={
            "role_code": "assistant", "content_text": "detail test",
            "content_markdown": "**bold**",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        assert cr.status_code == 200
        mid = cr.json()["data"]["message_id"]
        r = client.get(f"{_u()}/messages/{mid}")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["content_text"] == "detail test"
        assert d["content_markdown"] == "**bold**"
        assert "ingested_at" in d

    def test_nonexistent_404(self, messages_client):
        client, _ = messages_client
        r = client.get(f"{_u()}/messages/{uuid4()}")
        assert r.status_code == 404

    def test_wrong_conversation_404(self, messages_client):
        client, _ = messages_client
        cr = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "cross conv",
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        msg_id = cr.json()["data"]["message_id"]
        r = client.get(f"{_u(TEST_CONVERSATION2_ID)}/messages/{msg_id}")
        assert r.status_code == 404


# ═══════════════════════════════════════════════
# Batch import tests
# ═══════════════════════════════════════════════

class TestMessageBatch:
    def test_normal(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc)
        msgs = [{"role_code": "user", "content_text": f"b{i}",
                 "message_time": (t + timedelta(seconds=i)).isoformat()} for i in range(5)]
        r = client.post(f"{_u()}/messages/batch", json={"messages": msgs}, headers=_ik())
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["imported_count"] == 5
        assert d["skipped_duplicates"] == 0

    def test_exceeds_500_400(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc)
        msgs = [{"role_code": "user", "content_text": f"x{i}",
                 "message_time": (t + timedelta(seconds=i)).isoformat()} for i in range(501)]
        r = client.post(f"{_u()}/messages/batch", json={"messages": msgs}, headers=_ik())
        assert r.status_code == 400

    def test_duplicate_skipping(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc)
        client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "dup batch msg",
            "message_time": t.isoformat()}, headers=_ik())
        msgs = [
            {"role_code": "user", "content_text": "dup batch msg", "message_time": t.isoformat()},
            {"role_code": "assistant", "content_text": "new batch msg",
             "message_time": (t + timedelta(seconds=1)).isoformat()},
        ]
        r = client.post(f"{_u()}/messages/batch", json={"messages": msgs}, headers=_ik())
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["imported_count"] == 1
        assert d["skipped_duplicates"] == 1

    def test_sets_started_at(self, messages_client):
        client, engine = messages_client
        with Session(engine) as db:
            db.execute(text("UPDATE conversations SET started_at=NULL WHERE conversation_id=:c"), {"c": TEST_CONVERSATION_ID.hex})
            db.commit()
        t = datetime.now(timezone.utc)
        r = client.post(f"{_u()}/messages/batch", json={"messages": [
            {"role_code": "user", "content_text": "batch started_at",
             "message_time": t.isoformat()}]}, headers=_ik())
        assert r.status_code == 200
        with Session(engine) as db:
            row = db.execute(text("SELECT started_at FROM conversations WHERE conversation_id=:c"), {"c": TEST_CONVERSATION_ID.hex}).one()
            assert row[0] is not None

    def test_archived_conv_400(self, messages_client):
        client, engine = messages_client
        with Session(engine) as db:
            db.execute(text("UPDATE conversations SET conversation_status='archived', ended_at=datetime('now') WHERE conversation_id=:c"), {"c": TEST_CONVERSATION2_ID.hex})
            db.commit()
        r = client.post(f"{_u(TEST_CONVERSATION2_ID)}/messages/batch", json={"messages": [
            {"role_code": "user", "content_text": "x", "message_time": datetime.now(timezone.utc).isoformat()}]}, headers=_ik())
        assert r.status_code == 400

    def test_with_event_source_id(self, messages_client):
        client, _ = messages_client
        es = client.post(f"{_u()}/event-sources", json={"source_platform": "batch_es"}, headers=_ik())
        es_id = es.json()["data"]["event_source_id"]
        t = datetime.now(timezone.utc)
        msgs = [{"role_code": "user", "content_text": f"b_es{i}",
                 "message_time": (t + timedelta(seconds=i)).isoformat()} for i in range(3)]
        r = client.post(f"{_u()}/messages/batch", json={"messages": msgs, "event_source_id": es_id}, headers=_ik())
        assert r.status_code == 200
        assert r.json()["data"]["imported_count"] == 3

    def test_time_range_in_response(self, messages_client):
        client, _ = messages_client
        t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 10, 5, 0, tzinfo=timezone.utc)
        r = client.post(f"{_u()}/messages/batch", json={"messages": [
            {"role_code": "user", "content_text": "first", "message_time": t1.isoformat()},
            {"role_code": "assistant", "content_text": "last", "message_time": t2.isoformat()},
        ]}, headers=_ik())
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["first_message_time"] is not None
        assert d["last_message_time"] is not None


# ═══════════════════════════════════════════════
# Hash integrity tests
# ═══════════════════════════════════════════════

class TestHashIntegrity:
    def test_different_content_different_hash(self, messages_client):
        client, _ = messages_client
        t = datetime.now(timezone.utc).isoformat()
        r1 = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "content A",
            "message_time": t}, headers=_ik())
        r2 = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": "content B",
            "message_time": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}, headers=_ik())
        assert r1.json()["data"]["content_hash"] != r2.json()["data"]["content_hash"]

    def test_unicode_hash(self, messages_client):
        client, _ = messages_client
        content = "你好世界 🌍 Café"
        r = client.post(f"{_u()}/messages", json={
            "role_code": "user", "content_text": content,
            "message_time": datetime.now(timezone.utc).isoformat()}, headers=_ik())
        assert r.status_code == 200
        assert r.json()["data"]["content_hash"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
