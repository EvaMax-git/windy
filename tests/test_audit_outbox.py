"""P1-08: Audit / Events / Outbox write-loop tests.

Covers:
1. audit_events helper correctness.
2. events outbox helper correctness.
3. Idempotency key prevents duplicate writes.
4. Same-transaction write: business-table + audit_events + events.
5. Rollback on failure: all three tables are empty after a forced error.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.api.context import ActorContext, RequestContext  # noqa: E402
from mneme.db.audit import (  # noqa: E402
    AuditEvent,
    OutboxEvent,
    add_audit_event,
    add_outbox_event,
    add_audit_and_outbox,
    write_with_audit_and_outbox,
    write_with_audit_outbox_idempotency,
)
from mneme.db.idempotency import (  # noqa: E402
    IdempotencyConflict,
    check_idempotency_key,
    check_idempotency_key_any,
)
from mneme.db.transactions import session_scope  # noqa: E402
from mneme.security.audit import (  # noqa: E402
    audit_event_for_action,
    audit_event_for_policy_denied,
    audit_event_for_auth,
)
from mneme.security.policy import (  # noqa: E402
    Decision,
    DenyReason,
    PolicyDecision,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_context(
    *,
    request_id: UUID | None = None,
    correlation_id: UUID | None = None,
    actor_type: str = "user",
    actor_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> RequestContext:
    return RequestContext(
        request_id=request_id or uuid4(),
        correlation_id=correlation_id or uuid4(),
        actor=ActorContext(actor_type=actor_type, actor_id=actor_id),
        idempotency_key=idempotency_key,
    )


def _build_minimal_tables(engine) -> None:
    """Create the three tables needed for audit/outbox tests."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE items (
                  item_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
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
        conn.execute(
            text(
                """
                CREATE TABLE events (
                  event_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                  event_type TEXT NOT NULL,
                  aggregate_type TEXT NOT NULL,
                  aggregate_id TEXT NOT NULL,
                  aggregate_version BIGINT NOT NULL,
                  correlation_id TEXT NOT NULL,
                  causation_id TEXT,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  producer TEXT NOT NULL,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  visibility TEXT NOT NULL DEFAULT 'internal',
                  publish_state TEXT NOT NULL DEFAULT 'pending',
                  occurred_at TIMESTAMP NOT NULL,
                  committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  published_at TIMESTAMP,
                  last_error TEXT
                )
                """
            )
        )


@pytest.fixture
def db_session():
    """In-memory SQLite session with the three minimal tables."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _build_minimal_tables(engine)
    with Session(engine) as db:
        yield db


# ── audit_event_for_* helpers ─────────────────────────────────────────────────


class TestAuditEventHelpers:
    def test_audit_event_for_action_defaults(self) -> None:
        event = audit_event_for_action(
            action="test.action",
            object_type="test_object",
            object_id=uuid4(),
        )
        assert event.action == "test.action"
        assert event.result == "success"
        assert event.object_type == "test_object"
        assert event.reason_code is None
        assert event.sensitivity_level == "normal"

    def test_audit_event_for_action_failed(self) -> None:
        event = audit_event_for_action(
            action="test.action",
            result="failed",
            reason_code="validation_error",
        )
        assert event.result == "failed"
        assert event.reason_code == "validation_error"

    def test_audit_event_for_policy_denied(self) -> None:
        decision = PolicyDecision.deny(
            DenyReason.user_role_forbidden,
            "viewer cannot create",
            allowed_roles=["owner", "operator"],
        )
        event = audit_event_for_policy_denied(
            action="project.create",
            decision=decision,
            object_type="project",
        )
        assert event.result == "denied"
        assert event.reason_code == "user_role_forbidden"
        assert event.metadata_json["policy_message"] == "viewer cannot create"
        assert "policy_details" in event.metadata_json

    def test_audit_event_for_auth(self) -> None:
        event = audit_event_for_auth(
            action="auth.login",
            result="failed",
            reason_code="invalid_credentials",
        )
        assert event.action == "auth.login"
        assert event.result == "failed"
        assert event.reason_code == "invalid_credentials"


# ── add_audit_event / add_outbox_event ────────────────────────────────────────


class TestWriteAuditEvent:
    def test_add_audit_event_returns_uuid(self, db_session) -> None:
        ctx = _make_context()
        audit_id = add_audit_event(
            db_session,
            ctx,
            AuditEvent(action="test.write", object_type="item", object_id=uuid4()),
        )
        assert isinstance(audit_id, UUID)

        row = db_session.execute(
            text("SELECT action, result, request_id FROM audit_events")
        ).one()
        assert row.action == "test.write"
        assert row.result == "success"
        assert UUID(row.request_id) == ctx.request_id

    def test_add_audit_event_stores_all_fields(self, db_session) -> None:
        ctx = _make_context(actor_type="agent", actor_id=uuid4())
        audit_id = add_audit_event(
            db_session,
            ctx,
            AuditEvent(
                action="agent.create",
                result="denied",
                object_type="agent",
                object_id=uuid4(),
                reason_code="agent_disabled",
                sensitivity_level="sensitive",
            ),
        )
        row = db_session.execute(
            text(
                "SELECT actor_type, action, result, reason_code, sensitivity_level, "
                "correlation_id, request_id FROM audit_events"
            )
        ).one()
        assert row.actor_type == "agent"
        assert row.action == "agent.create"
        assert row.result == "denied"
        assert row.reason_code == "agent_disabled"
        assert row.sensitivity_level == "sensitive"
        assert UUID(row.correlation_id) == ctx.correlation_id
        assert UUID(row.request_id) == ctx.request_id


class TestWriteOutboxEvent:
    def test_add_outbox_event_returns_uuid(self, db_session) -> None:
        ctx = _make_context(idempotency_key="key-01")
        aggregate_id = uuid4()
        event_id = add_outbox_event(
            db_session,
            ctx,
            OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=aggregate_id,
                aggregate_version=1,
                idempotency_key="key-01",
            ),
        )
        assert isinstance(event_id, UUID)

        row = db_session.execute(
            text(
                "SELECT event_type, aggregate_type, aggregate_id, idempotency_key, "
                "publish_state FROM events"
            )
        ).one()
        assert row.event_type == "item.created"
        assert row.aggregate_type == "item"
        assert UUID(row.aggregate_id) == aggregate_id
        assert row.idempotency_key == "key-01"
        assert row.publish_state == "pending"

    def test_outbox_payload_includes_request_context(self, db_session) -> None:
        ctx = _make_context(idempotency_key="key-payload")
        add_outbox_event(
            db_session,
            ctx,
            OutboxEvent(
                event_type="test.payload",
                aggregate_type="test",
                aggregate_id=uuid4(),
                aggregate_version=1,
                idempotency_key="key-payload",
                payload_json={"custom": "value"},
            ),
        )
        import json

        row = db_session.execute(
            text("SELECT payload_json FROM events")
        ).one()
        payload = json.loads(row.payload_json) if isinstance(row.payload_json, str) else row.payload_json
        assert payload["request_id"] == str(ctx.request_id)
        assert payload["correlation_id"] == str(ctx.correlation_id)
        assert payload["custom"] == "value"


class TestAddAuditAndOutbox:
    def test_both_inserted_in_same_transaction(self, db_session) -> None:
        ctx = _make_context(idempotency_key="both-01")
        item_id = uuid4()
        audit_id, event_id = add_audit_and_outbox(
            db_session,
            ctx,
            audit_event=AuditEvent(action="item.create", object_type="item", object_id=item_id),
            outbox_event=OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_version=1,
                idempotency_key="both-01",
            ),
        )
        db_session.commit()

        audit_count = db_session.execute(text("SELECT count(*) FROM audit_events")).scalar_one()
        event_count = db_session.execute(text("SELECT count(*) FROM events")).scalar_one()
        assert audit_count == 1
        assert event_count == 1

    def test_both_rolled_back_on_failure(self, db_session) -> None:
        ctx = _make_context(idempotency_key="rollback-01")
        item_id = uuid4()

        # Insert a row that will cause a UNIQUE violation on the second insert
        # We simulate this by raising an error after the first insert.
        try:
            add_audit_event(
                db_session,
                ctx,
                AuditEvent(action="item.create", object_type="item", object_id=item_id),
            )
            # Force an error that rolls back the implicit transaction
            raise RuntimeError("simulated failure after audit, before outbox")
        except RuntimeError:
            db_session.rollback()

        # Both tables should be empty
        audit_count = db_session.execute(text("SELECT count(*) FROM audit_events")).scalar_one()
        event_count = db_session.execute(text("SELECT count(*) FROM events")).scalar_one()
        assert audit_count == 0
        assert event_count == 0


# ── write_with_audit_and_outbox ───────────────────────────────────────────────


class TestWriteWithAuditAndOutbox:
    def test_business_write_plus_audit_plus_outbox(self, db_session) -> None:
        ctx = _make_context(idempotency_key="full-write-01")
        item_id = uuid4()

        def _create_item(db: Session) -> str:
            db.execute(
                text("INSERT INTO items (item_id, name) VALUES (:id, :name)"),
                {"id": str(item_id), "name": "test-item"},
            )
            return "created"

        result = write_with_audit_and_outbox(
            db_session,
            ctx,
            work=_create_item,
            audit_event=AuditEvent(action="item.create", object_type="item", object_id=item_id),
            outbox_event=OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_version=1,
                idempotency_key="full-write-01",
            ),
        )
        assert result == "created"

        item_count = db_session.execute(text("SELECT count(*) FROM items")).scalar_one()
        audit_count = db_session.execute(text("SELECT count(*) FROM audit_events")).scalar_one()
        event_count = db_session.execute(text("SELECT count(*) FROM events")).scalar_one()
        assert item_count == 1
        assert audit_count == 1
        assert event_count == 1

    def test_all_three_rollback_on_business_error(self, db_session) -> None:
        ctx = _make_context(idempotency_key="rollback-all-01")
        item_id = uuid4()

        def _failing_work(db: Session) -> str:
            db.execute(
                text("INSERT INTO items (item_id, name) VALUES (:id, :name)"),
                {"id": str(item_id), "name": "will-rollback"},
            )
            raise RuntimeError("business logic failure")

        with pytest.raises(RuntimeError, match="business logic failure"):
            write_with_audit_and_outbox(
                db_session,
                ctx,
                work=_failing_work,
                audit_event=AuditEvent(action="item.create", object_type="item", object_id=item_id),
                outbox_event=OutboxEvent(
                    event_type="item.created",
                    aggregate_type="item",
                    aggregate_id=item_id,
                    aggregate_version=1,
                    idempotency_key="rollback-all-01",
                ),
            )

        # All three tables must be empty — the transaction rolled back.
        item_count = db_session.execute(text("SELECT count(*) FROM items")).scalar_one()
        audit_count = db_session.execute(text("SELECT count(*) FROM audit_events")).scalar_one()
        event_count = db_session.execute(text("SELECT count(*) FROM events")).scalar_one()
        assert item_count == 0, "business table must be empty after rollback"
        assert audit_count == 0, "audit_events must be empty after rollback"
        assert event_count == 0, "events must be empty after rollback"


# ── idempotency key ───────────────────────────────────────────────────────────


class TestIdempotencyKeyCheck:
    def test_check_returns_none_for_unknown_key(self, db_session) -> None:
        result = check_idempotency_key(
            db_session,
            idempotency_key="nonexistent",
            aggregate_type="item",
        )
        assert result is None

    def test_check_returns_none_when_key_is_none(self, db_session) -> None:
        result = check_idempotency_key(
            db_session,
            idempotency_key=None,
            aggregate_type="item",
        )
        assert result is None

    def test_check_returns_aggregate_id_for_existing_key(self, db_session) -> None:
        ctx = _make_context(idempotency_key="known-key")
        item_id = uuid4()
        add_outbox_event(
            db_session,
            ctx,
            OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_version=1,
                idempotency_key="known-key",
            ),
        )
        db_session.commit()

        result = check_idempotency_key(
            db_session,
            idempotency_key="known-key",
            aggregate_type="item",
        )
        assert result == item_id

    def test_check_mismatched_aggregate_type_returns_none(self, db_session) -> None:
        ctx = _make_context(idempotency_key="type-key")
        add_outbox_event(
            db_session,
            ctx,
            OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=uuid4(),
                aggregate_version=1,
                idempotency_key="type-key",
            ),
        )
        db_session.commit()

        # Key exists for "item" but we're asking for "other"
        result = check_idempotency_key(
            db_session,
            idempotency_key="type-key",
            aggregate_type="other",
        )
        assert result is None

    def test_check_any_finds_key_regardless_of_type(self, db_session) -> None:
        ctx = _make_context(idempotency_key="any-key")
        item_id = uuid4()
        add_outbox_event(
            db_session,
            ctx,
            OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_version=1,
                idempotency_key="any-key",
            ),
        )
        db_session.commit()

        result = check_idempotency_key_any(db_session, idempotency_key="any-key")
        assert result is not None
        event_id, agg_type, agg_id = result
        assert agg_type == "item"
        assert agg_id == item_id

    def test_unique_constraint_prevents_duplicate_key(self, db_session) -> None:
        ctx1 = _make_context(idempotency_key="dup-key")
        ctx2 = _make_context(idempotency_key="dup-key")

        add_outbox_event(
            db_session,
            ctx1,
            OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=uuid4(),
                aggregate_version=1,
                idempotency_key="dup-key",
            ),
        )
        db_session.commit()

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            add_outbox_event(
                db_session,
                ctx2,
                OutboxEvent(
                    event_type="item.created",
                    aggregate_type="item",
                    aggregate_id=uuid4(),
                    aggregate_version=1,
                    idempotency_key="dup-key",
                ),
            )
            db_session.commit()


class TestWriteWithIdempotency:
    def test_same_key_returns_existing_object(self, db_session) -> None:
        ctx = _make_context(idempotency_key="idem-001")

        # First write creates the object
        def _create_first(db: Session) -> str:
            db.execute(
                text("INSERT INTO items (item_id, name) VALUES (:id, :name)"),
                {"id": str(uuid4()), "name": "first"},
            )
            return "first"

        result1 = write_with_audit_outbox_idempotency(
            db_session,
            ctx,
            work=_create_first,
            audit_event=AuditEvent(action="item.create", object_type="item"),
            outbox_event=OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=uuid4(),
                aggregate_version=1,
                idempotency_key="idem-001",
            ),
            resolve_existing=lambda db, agg_id: "replayed",
        )
        # The first write succeeds
        assert result1 == "first"

        # Second write with same key → should resolve to existing
        ctx2 = _make_context(idempotency_key="idem-001")

        def _create_second(db: Session) -> str:
            pytest.fail("should not be called — idempotent replay")
            return "second"

        result2 = write_with_audit_outbox_idempotency(
            db_session,
            ctx2,
            work=_create_second,
            audit_event=AuditEvent(action="item.create", object_type="item"),
            outbox_event=OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=uuid4(),
                aggregate_version=1,
                idempotency_key="idem-001",
            ),
            resolve_existing=lambda db, agg_id: "replayed",
        )
        assert result2 == "replayed"

        # Only one item and one event should exist
        item_count = db_session.execute(text("SELECT count(*) FROM items")).scalar_one()
        event_count = db_session.execute(text("SELECT count(*) FROM events")).scalar_one()
        assert item_count == 1, "duplicate business object must not be created"
        assert event_count == 1, "duplicate outbox event must not be created"

    def test_no_resolve_callback_raises_conflict(self, db_session) -> None:
        ctx = _make_context(idempotency_key="idem-noresolve")

        # Write once to insert the key
        write_with_audit_outbox_idempotency(
            db_session,
            ctx,
            work=lambda db: "ok",
            audit_event=AuditEvent(action="test"),
            outbox_event=OutboxEvent(
                event_type="test",
                aggregate_type="test",
                aggregate_id=uuid4(),
                aggregate_version=1,
                idempotency_key="idem-noresolve",
            ),
            resolve_existing=lambda db, agg_id: "replayed",
        )

        # Second call without resolve_existing
        ctx2 = _make_context(idempotency_key="idem-noresolve")
        with pytest.raises(IdempotencyConflict, match="idem-noresolve"):
            write_with_audit_outbox_idempotency(
                db_session,
                ctx2,
                work=lambda db: "should-not-run",
                audit_event=AuditEvent(action="test"),
                outbox_event=OutboxEvent(
                    event_type="test",
                    aggregate_type="test",
                    aggregate_id=uuid4(),
                    aggregate_version=1,
                    idempotency_key="idem-noresolve",
                ),
                resolve_existing=None,
            )

    def test_null_idempotency_key_proceeds_without_check(self, db_session) -> None:
        ctx = _make_context(idempotency_key=None)

        result = write_with_audit_outbox_idempotency(
            db_session,
            ctx,
            work=lambda db: "no-idem",
            audit_event=AuditEvent(action="test"),
            outbox_event=OutboxEvent(
                event_type="test",
                aggregate_type="test",
                aggregate_id=uuid4(),
                aggregate_version=1,
                idempotency_key="",
            ),
            resolve_existing=None,
        )
        assert result == "no-idem"
        event_count = db_session.execute(text("SELECT count(*) FROM events")).scalar_one()
        assert event_count == 1


# ── cross-check: audit and event are request_id-traceable ─────────────────────


class TestTraceability:
    def test_audit_and_event_share_request_id(self, db_session) -> None:
        request_id = uuid4()
        ctx = _make_context(request_id=request_id, idempotency_key="trace-01")
        item_id = uuid4()

        write_with_audit_and_outbox(
            db_session,
            ctx,
            work=lambda db: None,
            audit_event=AuditEvent(action="item.create", object_type="item", object_id=item_id),
            outbox_event=OutboxEvent(
                event_type="item.created",
                aggregate_type="item",
                aggregate_id=item_id,
                aggregate_version=1,
                idempotency_key="trace-01",
            ),
        )

        audit_row = db_session.execute(
            text("SELECT request_id FROM audit_events")
        ).one()
        event_row = db_session.execute(
            text("SELECT correlation_id FROM events")
        ).one()

        assert UUID(audit_row.request_id) == request_id
        assert UUID(event_row.correlation_id) == ctx.correlation_id
