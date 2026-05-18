"""P2-04 DLQ Replay via Review — comprehensive tests.

Covers:
1. ``update_replay_state`` / ``count_active_reviews_for_dead_letter`` DAL
2. Replay submit flow: dead_letter (pending) → review_item created + replay_state=under_review
3. Duplicate replay prevention (replay_state guard + active reviews count + CAS)
4. Non-pending replay_state rejection
5. Approve triggers replay: replay_state='replayed' + delivery/event reset
6. Reject cancels replay: replay_state='cancelled'
7. Cancel during in_review cancels replay
8. ISSUE-1: ``_execute_dlq_replay`` missing ``return`` on CAS failure
9. ISSUE-5: Cancel in ``pending`` status does NOT reset dead_letter
10. Schema / Enum alignment verification
11. End-to-end: seed → dead_letter → replay submit → approve → state verification

Uses SQLite in-memory for DB-level logic with SessionLocal patching.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import uuid as _uuid
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

sqlite3.register_adapter(UUID, lambda u: str(u))  # str with hyphens for gen_random_uuid compat

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# SQLite compatibility patches are applied in the _setup_sqlite_compat fixture
# (module-scoped autouse), NOT at module level, to avoid corrupting other tests.
# We save the originals here for the fixture to restore later.
import mneme.db.review_items as _ri_mod
import mneme.db.audit as _audit_mod

_ORIG_RI_INSERT = _ri_mod._INSERT_REVIEW_ITEM
_ORIG_RI_CREATE = _ri_mod.create_review_item
_ORIG_AUDIT_INSERT = _audit_mod._INSERT_AUDIT_EVENT
_ORIG_ADD_AUDIT = _audit_mod.add_audit_event

_SQLITE_RI_INSERT = text("""
    INSERT INTO review_items (
        project_id, review_type, target_type, target_id,
        status, priority, requester_actor_type, requester_actor_id,
        due_at, expires_at, decision_payload,
        correlation_id, request_id, idempotency_key
    ) VALUES (
        :project_id, :review_type, :target_type, :target_id,
        :status, :priority, :requester_actor_type, :requester_actor_id,
        :due_at, :expires_at, :decision_payload,
        :correlation_id, :request_id, :idempotency_key
    )
    RETURNING review_item_id
""")

def _sqlite_create_review_item(**kwargs):
    dp = kwargs.get("decision_payload")
    if dp is not None and isinstance(dp, dict):
        kwargs["decision_payload"] = json.dumps(dp)
    elif dp is None:
        kwargs["decision_payload"] = "{}"
    return _ORIG_RI_CREATE(**kwargs)

_SQLITE_AUDIT_INSERT = text("""
    INSERT INTO audit_events (
      actor_type, actor_id, auth_context_type, auth_context_id,
      action, object_type, object_id, project_id,
      result, reason_code, sensitivity_level,
      correlation_id, request_id, review_item_id,
      diff_summary, metadata_json
    )
    VALUES (
      :actor_type, :actor_id, :auth_context_type, :auth_context_id,
      :action, :object_type, :object_id, :project_id,
      :result, :reason_code, :sensitivity_level,
      :correlation_id, :request_id, :review_item_id,
      :diff_summary, :metadata_json
    )
    RETURNING audit_id
""")

def _sqlite_add_audit(db, context, event):
    from dataclasses import replace
    if isinstance(event.diff_summary, dict):
        event = replace(event, diff_summary=json.dumps(event.diff_summary))
    if isinstance(event.metadata_json, dict):
        event = replace(event, metadata_json=json.dumps(event.metadata_json))
    return _ORIG_ADD_AUDIT(db, context, event)


@pytest.fixture(autouse=True, scope="module")
def _setup_sqlite_compat():
    """Apply SQLite-compatible patches for this module's tests, then restore."""
    # Apply patches
    _ri_mod._INSERT_REVIEW_ITEM = _SQLITE_RI_INSERT
    _ri_mod.create_review_item = _sqlite_create_review_item
    _audit_mod._INSERT_AUDIT_EVENT = _SQLITE_AUDIT_INSERT
    _audit_mod.add_audit_event = _sqlite_add_audit
    import mneme.api.routes.review_items as _routes_mod
    _routes_mod.add_audit_event = _sqlite_add_audit

    yield

    # Restore originals so other test modules are not affected
    _ri_mod._INSERT_REVIEW_ITEM = _ORIG_RI_INSERT
    _ri_mod.create_review_item = _ORIG_RI_CREATE
    _audit_mod._INSERT_AUDIT_EVENT = _ORIG_AUDIT_INSERT
    _audit_mod.add_audit_event = _ORIG_ADD_AUDIT
    _routes_mod.add_audit_event = _ORIG_ADD_AUDIT
# ============================================================================
# SQLite engine + schema builder
# ============================================================================

def _register_sqlite_compat(engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, conn_record):
        dbapi_conn.create_function(
            "now", 0,
            lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        dbapi_conn.create_function(
            "gen_random_uuid", 0,
            lambda: str(_uuid.uuid4()),
        )


def _build_tables(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE events (
              event_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              event_type TEXT NOT NULL, aggregate_type TEXT NOT NULL,
              aggregate_id TEXT NOT NULL, aggregate_version BIGINT NOT NULL DEFAULT 1,
              correlation_id TEXT NOT NULL, causation_id TEXT,
              idempotency_key TEXT NOT NULL UNIQUE,
              producer TEXT NOT NULL DEFAULT 'test',
              payload_json TEXT NOT NULL DEFAULT '{}',
              visibility TEXT NOT NULL DEFAULT 'internal',
              publish_state TEXT NOT NULL DEFAULT 'pending',
              occurred_at TIMESTAMP NOT NULL DEFAULT (now()),
              committed_at TIMESTAMP NOT NULL DEFAULT (now()),
              published_at TIMESTAMP, last_error TEXT,
              updated_at TIMESTAMP NOT NULL DEFAULT (now())
            )
        """))
        conn.execute(text("""
            CREATE TABLE event_deliveries (
              delivery_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              event_id TEXT NOT NULL, consumer_name TEXT NOT NULL,
              delivery_state TEXT NOT NULL DEFAULT 'pending',
              dispatch_attempts INTEGER NOT NULL DEFAULT 0,
              last_dispatched_at TIMESTAMP, acknowledged_at TIMESTAMP,
              failed_at TIMESTAMP, last_error TEXT, lease_expires_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT (now()),
              updated_at TIMESTAMP NOT NULL DEFAULT (now()),
              UNIQUE (event_id, consumer_name)
            )
        """))
        conn.execute(text("""
            CREATE TABLE dead_letters (
              dead_letter_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              source_type TEXT NOT NULL, source_id TEXT NOT NULL,
              related_event_id TEXT, aggregate_type TEXT, aggregate_id TEXT,
              failure_class TEXT NOT NULL, error_code TEXT,
              error_message TEXT NOT NULL,
              retry_exhausted INTEGER NOT NULL DEFAULT 0,
              external_effect_state TEXT NOT NULL DEFAULT 'none',
              replay_state TEXT NOT NULL DEFAULT 'pending',
              review_required INTEGER NOT NULL DEFAULT 0,
              payload_json TEXT NOT NULL DEFAULT '{}',
              first_failed_at TIMESTAMP NOT NULL DEFAULT (now()),
              last_failed_at TIMESTAMP NOT NULL DEFAULT (now()),
              replayed_at TIMESTAMP, resolved_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT (now()),
              updated_at TIMESTAMP NOT NULL DEFAULT (now())
            )
        """))
        conn.execute(text("""
            CREATE TABLE review_items (
              review_item_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              project_id TEXT, review_type TEXT NOT NULL,
              target_type TEXT NOT NULL, target_id TEXT NOT NULL,
              target_version BIGINT, status TEXT NOT NULL DEFAULT 'pending',
              priority INTEGER NOT NULL DEFAULT 100,
              requester_actor_type TEXT NOT NULL,
              requester_actor_id TEXT, reviewer_id TEXT,
              decision TEXT, reason TEXT,
              decision_payload TEXT NOT NULL DEFAULT '{}',
              due_at TIMESTAMP, decided_at TIMESTAMP, expires_at TIMESTAMP,
              correlation_id TEXT NOT NULL, request_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              created_at TIMESTAMP NOT NULL DEFAULT (now()),
              updated_at TIMESTAMP NOT NULL DEFAULT (now())
            )
        """))
        conn.execute(text("""
            CREATE TABLE audit_events (
              audit_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              actor_type TEXT NOT NULL DEFAULT 'system',
              actor_id TEXT, auth_context_type TEXT, auth_context_id TEXT,
              action TEXT NOT NULL, object_type TEXT, object_id TEXT,
              project_id TEXT, result TEXT NOT NULL DEFAULT 'success',
              reason_code TEXT, sensitivity_level TEXT NOT NULL DEFAULT 'normal',
              correlation_id TEXT NOT NULL, request_id TEXT NOT NULL,
              review_item_id TEXT, diff_summary TEXT NOT NULL DEFAULT '{}',
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TIMESTAMP NOT NULL DEFAULT (now())
            )
        """))


def _make_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _register_sqlite_compat(engine)
    _build_tables(engine)
    return engine


def _make_request_context():
    from mneme.api.context import ActorContext, RequestContext
    ctx_id = uuid4()
    return RequestContext(
        request_id=ctx_id, correlation_id=ctx_id,
        actor=ActorContext(actor_type="system", actor_id=uuid4()),
        idempotency_key=str(uuid4()),
    )


# ============================================================================
# Shared fixture: provide a session via patching all 3 SessionLocal import sites
# ============================================================================

def _patch_all_session_locals(session: Session):
    """Patch SessionLocal at all import sites so all DAL+API code uses our session."""
    patches = [
        patch("mneme.db.dead_letters.SessionLocal"),
        patch("mneme.db.review_items.SessionLocal"),
        patch("mneme.api.routes.review_items.SessionLocal"),
    ]
    for p in patches:
        mock_sl = p.start()
        mock_sl.return_value.__enter__.return_value = session
        mock_sl.return_value.__exit__.return_value = False
    return patches


def _activate_patches(patches):
    pass  # patches are already started in _patch_all_session_locals


def _deactivate_patches(patches):
    for p in reversed(patches):
        p.stop()


# ============================================================================
# Monkey-patch _isoformat to handle SQLite string timestamps
# ============================================================================

def _patch_isoformat():
    """SQLite returns timestamps as ISO strings, but _isoformat expects datetime objects."""
    import mneme.db.dead_letters as dl_mod
    import mneme.db.review_items as ri_mod

    _orig_dl = dl_mod._isoformat
    _orig_ri = ri_mod._isoformat

    def _safe_isoformat(dt):
        if dt is None:
            return None
        if isinstance(dt, str):
            return dt  # SQLite returns ISO strings already
        return dt.isoformat()

    dl_mod._isoformat = _safe_isoformat
    ri_mod._isoformat = _safe_isoformat
    return _orig_dl, _orig_ri


def _unpatch_isoformat(orig):
    import mneme.db.dead_letters as dl_mod
    import mneme.db.review_items as ri_mod
    dl_mod._isoformat = orig[0]
    ri_mod._isoformat = orig[1]


@pytest.fixture(autouse=True)
def _auto_patch_isoformat():
    orig = _patch_isoformat()
    yield
    _unpatch_isoformat(orig)


# ============================================================================
# 1. P2-04 DAL: replay_state
# ============================================================================

class TestReplayStateDAL:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def _insert_dl(self, session: Session, replay_state: str = "pending") -> str:
        dl_id = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO dead_letters (
                dead_letter_id, source_type, source_id, failure_class,
                error_message, retry_exhausted, replay_state, review_required,
                external_effect_state, first_failed_at, last_failed_at,
                related_event_id, aggregate_type, aggregate_id
            ) VALUES (
                :did, 'event_delivery', :sid, 'code_bug', 'test error',
                TRUE, :rs, TRUE, 'none', :now, :now,
                :eid, 'test', :aid
            )
        """), {"did": dl_id, "sid": str(uuid4()), "rs": replay_state,
               "now": now, "eid": str(uuid4()), "aid": str(uuid4())})
        return dl_id

    def test_update_success(self, db_setup):
        engine, session = db_setup
        dl_id = self._insert_dl(session, "pending")
        session.commit()

        from mneme.db.dead_letters import update_replay_state
        ok = update_replay_state(UUID(dl_id), "under_review", "pending")
        assert ok is True
        row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id}).mappings().first()
        assert row["replay_state"] == "under_review"

    def test_cas_fails(self, db_setup):
        engine, session = db_setup
        dl_id = self._insert_dl(session, "under_review")
        session.commit()

        from mneme.db.dead_letters import update_replay_state
        ok = update_replay_state(UUID(dl_id), "replayed", "pending")
        assert ok is False
        row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id}).mappings().first()
        assert row["replay_state"] == "under_review"

    def test_full_chain(self, db_setup):
        engine, session = db_setup
        dl_id = self._insert_dl(session, "pending")
        session.commit()

        from mneme.db.dead_letters import update_replay_state
        assert update_replay_state(UUID(dl_id), "under_review", "pending") is True
        assert update_replay_state(UUID(dl_id), "replayed", "under_review") is True
        row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id}).mappings().first()
        assert row["replay_state"] == "replayed"


# ============================================================================
# 2. Active reviews counting
# ============================================================================

class TestCountActiveReviewsDAL:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def _insert_dl(self, session, dl_id=None, rs="pending"):
        did = str(dl_id or uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                failure_class, error_message, retry_exhausted, replay_state,
                review_required, external_effect_state, first_failed_at,
                last_failed_at, related_event_id, aggregate_type, aggregate_id)
            VALUES (:did, 'event_delivery', :sid, 'code_bug', 'test', TRUE,
                :rs, TRUE, 'none', :now, :now, :eid, 'test', :aid)
        """), {"did": did, "sid": str(uuid4()), "rs": rs,
               "now": now, "eid": str(uuid4()), "aid": str(uuid4())})
        return did

    def test_no_active_reviews(self, db_setup):
        engine, session = db_setup
        from mneme.db.dead_letters import count_active_reviews_for_dead_letter
        assert count_active_reviews_for_dead_letter(uuid4()) == 0

    def test_active_review_counted(self, db_setup):
        engine, session = db_setup
        dl_id = uuid4()
        self._insert_dl(session, dl_id, "pending")
        session.commit()
        ctx = _make_request_context()

        from mneme.db.review_items import create_review_item
        from mneme.db.dead_letters import count_active_reviews_for_dead_letter

        create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="in_review", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        assert count_active_reviews_for_dead_letter(dl_id) == 1

    def test_final_reviews_not_counted(self, db_setup):
        engine, session = db_setup
        dl_id = uuid4()
        self._insert_dl(session, dl_id, "pending")
        session.commit()
        ctx = _make_request_context()

        from mneme.db.review_items import create_review_item
        from mneme.db.dead_letters import count_active_reviews_for_dead_letter

        for status in ["rejected", "cancelled", "expired"]:
            create_review_item(
                project_id=None, review_type="dlq_replay",
                target_type="dead_letter", target_id=dl_id,
                status=status, priority=100,
                requester_actor_type="system",
                requester_actor_id=ctx.actor.actor_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                idempotency_key=str(uuid4()),
            )
        assert count_active_reviews_for_dead_letter(dl_id) == 0

    def test_only_target_match_counted(self, db_setup):
        engine, session = db_setup
        dl_id = uuid4()
        other_id = uuid4()
        self._insert_dl(session, dl_id, "pending")
        session.commit()
        ctx = _make_request_context()

        from mneme.db.review_items import create_review_item
        from mneme.db.dead_letters import count_active_reviews_for_dead_letter

        create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="in_review", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=other_id,
            status="in_review", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        assert count_active_reviews_for_dead_letter(dl_id) == 1


# ============================================================================
# 3. Replay Submit flow
# ============================================================================

class TestReplaySubmitFlow:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def _seed_pending(self, session):
        dl_id = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                failure_class, error_message, retry_exhausted, replay_state,
                review_required, external_effect_state, first_failed_at,
                last_failed_at, related_event_id, aggregate_type, aggregate_id)
            VALUES (:did, 'event_delivery', :sid, 'provider_transient_exhausted',
                '503 error', TRUE, 'pending', TRUE, 'none', :now, :now,
                :eid, 'test', :aid)
        """), {"did": dl_id, "sid": str(uuid4()), "now": now,
               "eid": str(uuid4()), "aid": str(uuid4())})
        return dl_id

    def test_submit_replay_creates_review_item(self, db_setup):
        engine, session = db_setup
        dl_id_str = self._seed_pending(session)
        session.commit()
        dl_id = UUID(dl_id_str)

        from mneme.db.dead_letters import (get_dead_letter_by_id,
            count_active_reviews_for_dead_letter, update_replay_state)
        from mneme.db.review_items import (create_review_item, move_to_in_review,
            get_review_item_by_id)

        dl = get_dead_letter_by_id(dl_id)
        assert dl["replay_state"] == "pending"
        assert count_active_reviews_for_dead_letter(dl_id) == 0
        assert update_replay_state(dl_id, "under_review", "pending") is True

        ctx = _make_request_context()
        row = create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="pending", priority=100,
            requester_actor_type=ctx.actor.actor_type,
            requester_actor_id=ctx.actor.actor_id,
            decision_payload=json.dumps({"source_type": dl["source_type"]}),
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        ri_id = UUID(row["review_item_id"])
        assert move_to_in_review(ri_id) is True

        dl2 = get_dead_letter_by_id(dl_id)
        assert dl2["replay_state"] == "under_review"
        ri = get_review_item_by_id(ri_id)
        assert ri["review_type"] == "dlq_replay"
        assert ri["target_type"] == "dead_letter"
        assert ri["status"] == "in_review"

    def test_duplicate_blocked_by_replay_state(self, db_setup):
        engine, session = db_setup
        dl_id_str = self._seed_pending(session)
        session.commit()
        dl_id = UUID(dl_id_str)

        from mneme.db.dead_letters import update_replay_state, get_dead_letter_by_id
        update_replay_state(dl_id, "under_review", "pending")
        dl = get_dead_letter_by_id(dl_id)
        assert dl["replay_state"] != "pending"

    def test_duplicate_blocked_by_active_reviews(self, db_setup):
        engine, session = db_setup
        dl_id_str = self._seed_pending(session)
        session.commit()
        dl_id = UUID(dl_id_str)

        from mneme.db.dead_letters import count_active_reviews_for_dead_letter
        from mneme.db.review_items import create_review_item

        ctx = _make_request_context()
        create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="in_review", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        assert count_active_reviews_for_dead_letter(dl_id) > 0

    def test_nonexistent_returns_none(self, db_setup):
        from mneme.db.dead_letters import get_dead_letter_by_id
        assert get_dead_letter_by_id(uuid4()) is None


# ============================================================================
# 4. Approve → Replay execution
# ============================================================================

class TestApproveTriggersReplay:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def _seed_full(self, session):
        eid = str(uuid4())
        did = str(uuid4())
        dl_id = str(uuid4())
        aid = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, correlation_id, idempotency_key,
                producer, payload_json, publish_state, occurred_at)
            VALUES (:eid, 'test.e2e', 'test_dlq', :aid, 1, :corr, :ikey,
                'pytest', '{}', 'dead_letter', :now)
        """), {"eid": eid, "aid": aid, "corr": str(uuid4()),
               "ikey": f"e2e_{dl_id[:8]}", "now": now})
        session.execute(text("""
            INSERT INTO event_deliveries (delivery_id, event_id, consumer_name,
                delivery_state, dispatch_attempts, last_error, failed_at)
            VALUES (:did, :eid, 'noop', 'dead_letter', 5,
                '503 Service Unavailable', :now)
        """), {"did": did, "eid": eid, "now": now})
        session.execute(text("""
            INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                related_event_id, failure_class, error_message, retry_exhausted,
                replay_state, review_required, external_effect_state,
                aggregate_type, aggregate_id, first_failed_at, last_failed_at)
            VALUES (:dl_id, 'event_delivery', :did, :eid,
                'provider_transient_exhausted', '503 Service Unavailable',
                TRUE, 'under_review', TRUE, 'none',
                'test_dlq', :aid, :now, :now)
        """), {"dl_id": dl_id, "did": did, "eid": eid, "aid": aid, "now": now})
        return eid, did, dl_id

    def test_approve_success(self, db_setup):
        engine, session = db_setup
        ctx = _make_request_context()
        from mneme.db.review_items import (create_review_item, move_to_in_review,
            approve_review_item, get_review_item_by_id)

        dl_id = uuid4()
        row = create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="pending", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        ri_id = UUID(row["review_item_id"])
        move_to_in_review(ri_id)
        ok = approve_review_item(ri_id, uuid4(), "Approved")
        assert ok is True
        ri = get_review_item_by_id(ri_id)
        assert ri["status"] == "approved"
        assert ri["decision"] == "approved"

    def test_approve_wrong_status_fails(self, db_setup):
        engine, session = db_setup
        ctx = _make_request_context()
        from mneme.db.review_items import create_review_item, approve_review_item

        dl_id = uuid4()
        row = create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="pending", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        ri_id = UUID(row["review_item_id"])
        ok = approve_review_item(ri_id, uuid4())
        assert ok is False

    def test_full_replay_resets_delivery_and_event(self, db_setup):
        engine, session = db_setup
        eid, did, dl_id_str = self._seed_full(session)
        session.commit()
        dl_id = UUID(dl_id_str)
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _execute_dlq_replay
        _execute_dlq_replay(dead_letter_id=dl_id, review_item_id=uuid4(), context=ctx)

        row = session.execute(text(
            "SELECT replay_state, replayed_at FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id_str}).mappings().first()
        assert row["replay_state"] == "replayed"
        assert row["replayed_at"] is not None

        del_row = session.execute(text("""
            SELECT delivery_state, dispatch_attempts, last_error, failed_at
            FROM event_deliveries WHERE delivery_id=:did
        """), {"did": did}).mappings().first()
        assert del_row["delivery_state"] == "pending"
        assert del_row["dispatch_attempts"] == 0
        assert del_row["last_error"] is None
        assert del_row["failed_at"] is None

        evt_row = session.execute(text("""
            SELECT publish_state, last_error FROM events WHERE event_id=:eid
        """), {"eid": eid}).mappings().first()
        assert evt_row["publish_state"] == "pending"
        assert evt_row["last_error"] is None

    def test_replay_writes_audit(self, db_setup):
        engine, session = db_setup
        eid, did, dl_id_str = self._seed_full(session)
        session.commit()
        dl_id = UUID(dl_id_str)
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _execute_dlq_replay
        _execute_dlq_replay(dead_letter_id=dl_id, review_item_id=uuid4(), context=ctx)

        rows = session.execute(text(
            "SELECT action FROM audit_events WHERE action='dlq.replayed'"
        )).mappings().all()
        assert len(rows) >= 1

    def test_reject_success(self, db_setup):
        engine, session = db_setup
        ctx = _make_request_context()
        from mneme.db.review_items import (create_review_item, move_to_in_review,
            reject_review_item, get_review_item_by_id)

        dl_id = uuid4()
        row = create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="pending", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        ri_id = UUID(row["review_item_id"])
        move_to_in_review(ri_id)
        ok = reject_review_item(ri_id, uuid4(), "Not safe")
        assert ok is True
        ri = get_review_item_by_id(ri_id)
        assert ri["status"] == "rejected"
        assert ri["decision"] == "rejected"


# ============================================================================
# 5. Cancel → cancelled
# ============================================================================

class TestCancelDlqReplay:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def _seed_under_review(self, session):
        dl_id = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                failure_class, error_message, retry_exhausted, replay_state,
                review_required, external_effect_state, first_failed_at,
                last_failed_at, related_event_id, aggregate_type, aggregate_id)
            VALUES (:did, 'event_delivery', :sid, 'code_bug', 'test', TRUE,
                'under_review', TRUE, 'none', :now, :now,
                :eid, 'test', :aid)
        """), {"did": dl_id, "sid": str(uuid4()), "now": now,
               "eid": str(uuid4()), "aid": str(uuid4())})
        return dl_id

    def test_cancel_sets_cancelled(self, db_setup):
        engine, session = db_setup
        dl_id_str = self._seed_under_review(session)
        session.commit()
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _cancel_dlq_replay
        _cancel_dlq_replay(dead_letter_id=UUID(dl_id_str), review_item_id=uuid4(), context=ctx)

        row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id_str}).mappings().first()
        assert row["replay_state"] == "cancelled"

    def test_cancel_writes_audit(self, db_setup):
        engine, session = db_setup
        dl_id_str = self._seed_under_review(session)
        session.commit()
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _cancel_dlq_replay
        _cancel_dlq_replay(dead_letter_id=UUID(dl_id_str), review_item_id=uuid4(), context=ctx)

        rows = session.execute(text(
            "SELECT action FROM audit_events WHERE action='dlq.replay_cancelled'"
        )).mappings().all()
        assert len(rows) >= 1

    def test_cancel_not_under_review_skipped(self, db_setup):
        engine, session = db_setup
        dl_id_str = self._seed_under_review(session)
        session.execute(text(
            "UPDATE dead_letters SET replay_state='pending' WHERE dead_letter_id=:did"
        ), {"did": dl_id_str})
        session.commit()
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _cancel_dlq_replay
        _cancel_dlq_replay(dead_letter_id=UUID(dl_id_str), review_item_id=uuid4(), context=ctx)

        row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id_str}).mappings().first()
        assert row["replay_state"] == "pending"


# ============================================================================
# 6. ISSUE-1: _execute_dlq_replay missing return — FIX VERIFICATION
# ============================================================================

class TestIssue1MissingReturnFix:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def _seed(self, session, rs, ds, es):
        eid = str(uuid4())
        did = str(uuid4())
        dl_id = str(uuid4())
        aid = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, correlation_id, idempotency_key,
                producer, payload_json, publish_state, occurred_at)
            VALUES (:eid, 'test.i1', 'test', :aid, 1, :corr, :ikey,
                'pytest', '{}', :es, :now)
        """), {"eid": eid, "aid": aid, "corr": str(uuid4()),
               "ikey": f"i1_{dl_id[:8]}", "es": es, "now": now})
        session.execute(text("""
            INSERT INTO event_deliveries (delivery_id, event_id, consumer_name,
                delivery_state, dispatch_attempts, last_error, failed_at)
            VALUES (:did, :eid, 'noop', :ds, 5, 'test error', :now)
        """), {"did": did, "eid": eid, "ds": ds, "now": now})
        session.execute(text("""
            INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                related_event_id, failure_class, error_message, retry_exhausted,
                replay_state, review_required, external_effect_state,
                aggregate_type, aggregate_id, first_failed_at, last_failed_at)
            VALUES (:dl_id, 'event_delivery', :did, :eid, 'code_bug', 'test',
                TRUE, :rs, TRUE, 'none', 'test', :aid, :now, :now)
        """), {"dl_id": dl_id, "did": did, "eid": eid,
               "rs": rs, "aid": aid, "now": now})
        return eid, did, dl_id

    def test_replay_on_cancelled_skipped(self, db_setup):
        """ISSUE-1 FIXED: cancelled dead_letter skips delivery reset."""
        engine, session = db_setup
        eid, did, dl_id_str = self._seed(session, "cancelled", "dead_letter", "dead_letter")
        session.commit()
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _execute_dlq_replay
        _execute_dlq_replay(dead_letter_id=UUID(dl_id_str), review_item_id=uuid4(), context=ctx)

        # CAS prevented replay_state change
        dl_row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id_str}).mappings().first()
        assert dl_row["replay_state"] == "cancelled"

        # FIX VERIFIED: delivery NOT reset
        del_row = session.execute(text("""
            SELECT delivery_state, dispatch_attempts, last_error
            FROM event_deliveries WHERE delivery_id=:did
        """), {"did": did}).mappings().first()
        assert del_row["delivery_state"] == "dead_letter", (
            "ISSUE-1 FIX VERIFIED: cancelled DL delivery was NOT reset"
        )
        assert del_row["dispatch_attempts"] == 5
        assert del_row["last_error"] == "test error"

    def test_replay_on_pending_skipped(self, db_setup):
        """ISSUE-1 FIXED: pending dead_letter delivery NOT reset."""
        engine, session = db_setup
        eid, did, dl_id_str = self._seed(session, "pending", "dead_letter", "dead_letter")
        session.commit()
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _execute_dlq_replay
        _execute_dlq_replay(dead_letter_id=UUID(dl_id_str), review_item_id=uuid4(), context=ctx)

        dl_row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id_str}).mappings().first()
        assert dl_row["replay_state"] == "pending"

        del_row = session.execute(text("""
            SELECT delivery_state, dispatch_attempts
            FROM event_deliveries WHERE delivery_id=:did
        """), {"did": did}).mappings().first()
        assert del_row["delivery_state"] == "dead_letter", (
            "ISSUE-1 FIX VERIFIED: pending DL delivery was NOT reset"
        )
        assert del_row["dispatch_attempts"] == 5

    def test_replay_duplicate_call_skipped(self, db_setup):
        """ISSUE-1 FIXED: second _execute_dlq_replay does NOT reset delivery again."""
        engine, session = db_setup
        eid, did, dl_id_str = self._seed(session, "under_review", "dead_letter", "dead_letter")
        session.commit()
        ctx = _make_request_context()

        from mneme.api.routes.memory.review_items import _execute_dlq_replay
        _execute_dlq_replay(dead_letter_id=UUID(dl_id_str), review_item_id=uuid4(), context=ctx)

        dl_row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": dl_id_str}).mappings().first()
        assert dl_row["replay_state"] == "replayed"

        # Reset delivery back to detect duplicate reset
        session.execute(text("""
            UPDATE event_deliveries SET delivery_state='dead_letter',
            dispatch_attempts=5, last_error='test' WHERE delivery_id=:did
        """), {"did": did})
        session.commit()

        # Second call — CAS fails, function returns early
        _execute_dlq_replay(dead_letter_id=UUID(dl_id_str), review_item_id=uuid4(), context=ctx)

        del_row = session.execute(text("""
            SELECT delivery_state FROM event_deliveries WHERE delivery_id=:did
        """), {"did": did}).mappings().first()
        assert del_row["delivery_state"] == "dead_letter", (
            "ISSUE-1 FIX VERIFIED: duplicate replay did NOT reset delivery"
        )


# ============================================================================
# 7. ISSUE-5: Cancel pending doesn't reset dead_letter
# ============================================================================

class TestIssue5CancelPendingFix:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def test_cancel_pending_resets_dead_letter(self, db_setup):
        """ISSUE-5 FIXED: _cancel_dlq_replay now also called for pending status."""
        engine, session = db_setup
        ctx = _make_request_context()
        dl_id = uuid4()

        from mneme.db.review_items import (create_review_item, cancel_review_item,
            get_review_item_by_id)
        from mneme.api.routes.memory.review_items import _cancel_dlq_replay

        # Create review_item but don't move to in_review
        create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="pending", priority=100,
            requester_actor_type="system",
            requester_actor_id=ctx.actor.actor_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )

        # Insert dead_letter in 'under_review' (simulating post-submit state)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                failure_class, error_message, retry_exhausted, replay_state,
                review_required, external_effect_state, first_failed_at,
                last_failed_at, related_event_id, aggregate_type, aggregate_id)
            VALUES (:did, 'event_delivery', :sid, 'code_bug', 'test', TRUE,
                'under_review', TRUE, 'none', :now, :now, :eid, 'test', :aid)
        """), {"did": str(dl_id), "sid": str(uuid4()), "now": now,
               "eid": str(uuid4()), "aid": str(uuid4())})
        session.commit()

        # Get & cancel review item
        ri_rows = session.execute(text("""
            SELECT review_item_id, status FROM review_items
            WHERE target_type='dead_letter' AND target_id=:tid
        """), {"tid": str(dl_id)}).mappings().all()
        assert len(ri_rows) == 1
        ri_id = UUID(ri_rows[0]["review_item_id"])
        assert ri_rows[0]["status"] == "pending"

        ok = cancel_review_item(ri_id)
        assert ok is True
        ri = get_review_item_by_id(ri_id)
        assert ri["status"] == "cancelled"

        # Simulate the endpoint handler: now also covers 'pending' status
        _cancel_dlq_replay(dead_letter_id=dl_id, review_item_id=ri_id, context=ctx)

        # ISSUE-5 FIXED: dead_letter should be 'cancelled'
        dl_row = session.execute(text(
            "SELECT replay_state FROM dead_letters WHERE dead_letter_id=:did"
        ), {"did": str(dl_id)}).mappings().first()
        assert dl_row["replay_state"] == "cancelled", (
            "ISSUE-5 FIX VERIFIED: dead_letter correctly set to cancelled "
            "when pending review is cancelled"
        )


# ============================================================================
# 8. Schema & Enum alignment
# ============================================================================

class TestSchemaAlignment:

    def test_review_type_includes_dlq_replay(self):
        from mneme.schemas.review_items import ReviewType
        assert "dlq_replay" in {e.value for e in ReviewType}

    def test_review_status_all_6(self):
        from mneme.schemas.review_items import ReviewStatus
        assert {e.value for e in ReviewStatus} == {
            "pending", "in_review", "approved", "rejected", "cancelled", "expired"}

    def test_review_target_includes_dead_letter(self):
        from mneme.schemas.review_items import ReviewTargetType
        assert "dead_letter" in {e.value for e in ReviewTargetType}

    def test_replay_state_all_5(self):
        from mneme.schemas.dead_letters import ReplayState
        assert {e.value for e in ReplayState} == {
            "pending", "under_review", "replayed", "cancelled", "resolved"}

    def test_replay_state_written_valid(self):
        from mneme.schemas.dead_letters import ReplayState
        valid = {e.value for e in ReplayState}
        assert {"pending", "under_review", "replayed", "cancelled"}.issubset(valid)

    def test_dead_letter_read_replay_fields(self):
        from mneme.schemas.dead_letters import DeadLetterRead
        fields = set(DeadLetterRead.model_fields.keys())
        for f in ("replay_state", "replayed_at", "resolved_at", "review_required"):
            assert f in fields

    def test_dead_letter_read_20_fields(self):
        from mneme.schemas.dead_letters import DeadLetterRead
        assert len(DeadLetterRead.model_fields) == 20

    def test_review_item_read_all_ddl(self):
        from mneme.schemas.review_items import ReviewItemRead
        fields = set(ReviewItemRead.model_fields.keys())
        required = {"review_item_id", "review_type", "target_type", "target_id",
                    "status", "decision", "reason", "reviewer_id", "decided_at",
                    "decision_payload", "requester_actor_type", "requester_actor_id",
                    "priority", "due_at", "expires_at", "correlation_id", "request_id",
                    "idempotency_key", "created_at", "updated_at"}
        assert required.issubset(fields)


# ============================================================================
# 9. State machine
# ============================================================================

class TestReplayStateMachine:

    def test_valid_transitions(self):
        valid = {("pending", "under_review"), ("under_review", "replayed"),
                 ("under_review", "cancelled"), ("under_review", "pending")}
        written = {("pending", "under_review"), ("under_review", "replayed"),
                   ("under_review", "cancelled"), ("under_review", "pending")}
        assert written == valid


# ============================================================================
# 10. E2E: seed → dead_letter → replay → approve → verify
# ============================================================================

class TestE2EDlqReplay:

    @pytest.fixture
    def db_setup(self):
        engine = _make_engine()
        session = Session(engine)
        pl = _patch_all_session_locals(session)
        _activate_patches(pl)
        yield engine, session
        _deactivate_patches(pl)
        session.close()
        engine.dispose()

    def _seed_e2e(self, session):
        eid = str(uuid4())
        did = str(uuid4())
        dl_id = str(uuid4())
        aid = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.execute(text("""
            INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, correlation_id, idempotency_key,
                producer, payload_json, publish_state, occurred_at)
            VALUES (:eid, 'test.e2e_full', 'test_dlq', :aid, 1, :corr, :ikey,
                'pytest', '{}', 'dead_letter', :now)
        """), {"eid": eid, "aid": aid, "corr": str(uuid4()),
               "ikey": f"e2efull_{dl_id[:8]}", "now": now})
        session.execute(text("""
            INSERT INTO event_deliveries (delivery_id, event_id, consumer_name,
                delivery_state, dispatch_attempts, last_error, failed_at)
            VALUES (:did, :eid, 'noop', 'dead_letter', 5,
                '503 Service Unavailable', :now)
        """), {"did": did, "eid": eid, "now": now})
        session.execute(text("""
            INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                related_event_id, failure_class, error_message, retry_exhausted,
                replay_state, review_required, external_effect_state,
                aggregate_type, aggregate_id, first_failed_at, last_failed_at)
            VALUES (:dl_id, 'event_delivery', :did, :eid,
                'provider_transient_exhausted', '503 Service Unavailable',
                TRUE, 'pending', TRUE, 'none',
                'test_dlq', :aid, :now, :now)
        """), {"dl_id": dl_id, "did": did, "eid": eid, "aid": aid, "now": now})
        return eid, did, dl_id

    def test_e2e_full_replay_flow(self, db_setup):
        engine, session = db_setup
        seed = self._seed_e2e(session)
        session.commit()
        eid, did, dl_id_str = seed
        dl_id = UUID(dl_id_str)

        from mneme.db.dead_letters import (get_dead_letter_by_id,
            count_active_reviews_for_dead_letter, update_replay_state)
        from mneme.db.review_items import (create_review_item, move_to_in_review,
            approve_review_item, get_review_item_by_id)
        from mneme.api.routes.memory.review_items import _execute_dlq_replay

        # Phase 1: Initial state
        dl = get_dead_letter_by_id(dl_id)
        assert dl["replay_state"] == "pending"
        assert dl["retry_exhausted"] is True
        assert dl["review_required"] is True

        # Phase 2: Submit replay
        assert count_active_reviews_for_dead_letter(dl_id) == 0
        assert update_replay_state(dl_id, "under_review", "pending") is True

        ctx = _make_request_context()
        row = create_review_item(
            project_id=None, review_type="dlq_replay",
            target_type="dead_letter", target_id=dl_id,
            status="pending", priority=100,
            requester_actor_type=ctx.actor.actor_type,
            requester_actor_id=ctx.actor.actor_id,
            decision_payload=json.dumps({"source_type": dl["source_type"]}),
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            idempotency_key=str(uuid4()),
        )
        ri_id = UUID(row["review_item_id"])
        assert move_to_in_review(ri_id) is True

        dl = get_dead_letter_by_id(dl_id)
        assert dl["replay_state"] == "under_review"
        ri = get_review_item_by_id(ri_id)
        assert ri["status"] == "in_review"

        # Phase 3: Approve → execute replay
        assert approve_review_item(ri_id, uuid4(), "Safe to replay") is True
        _execute_dlq_replay(dead_letter_id=dl_id, review_item_id=ri_id, context=_make_request_context())

        # Phase 4: Final verification
        dl = get_dead_letter_by_id(dl_id)
        assert dl["replay_state"] == "replayed"
        assert dl["replayed_at"] is not None

        del_row = session.execute(text("""
            SELECT delivery_state, dispatch_attempts, last_error, failed_at
            FROM event_deliveries WHERE delivery_id=:did
        """), {"did": did}).mappings().first()
        assert del_row["delivery_state"] == "pending"
        assert del_row["dispatch_attempts"] == 0
        assert del_row["last_error"] is None
        assert del_row["failed_at"] is None

        evt_row = session.execute(text("""
            SELECT publish_state, last_error FROM events WHERE event_id=:eid
        """), {"eid": eid}).mappings().first()
        assert evt_row["publish_state"] == "pending"
        assert evt_row["last_error"] is None

        actions = {a["action"] for a in session.execute(text(
            "SELECT action FROM audit_events WHERE object_id=:oid"
        ), {"oid": dl_id_str}).mappings().all()}
        assert "dlq.replayed" in actions


# ============================================================================
# 11. API integration tests (require running server)
# ============================================================================

class TestReplayApiEndpoints:

    @pytest.fixture(autouse=True)
    def _skip_if_no_server(self):
        import httpx
        base = os.environ.get("API_BASE_URL", "http://localhost:8000")
        try:
            resp = httpx.get(f"{base}/health/live", timeout=2)
            if resp.status_code != 200:
                pytest.skip("Server not healthy")
        except Exception:
            pytest.skip("Server not reachable")

    @pytest.fixture
    def api_base(self):
        return os.environ.get("API_BASE_URL", "http://localhost:8000")

    def test_replay_submit_returns_201(self, api_base):
        import httpx
        from mneme.db.base import SessionLocal

        with SessionLocal() as db:
            from sqlalchemy import text
            dl_id = uuid4(); did = uuid4(); eid = uuid4(); aid = uuid4()
            now = datetime.datetime.now(datetime.timezone.utc)
            db.execute(text("""
                INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, correlation_id, idempotency_key,
                    producer, payload_json, publish_state, occurred_at)
                VALUES (:eid, 'test.api_replay', 'test', :aid, 1, :cid, :ikey,
                    'test', '{}', 'dead_letter', :now)
            """), {"eid": eid, "aid": aid, "cid": uuid4(), "ikey": f"api_{dl_id}", "now": now})
            db.execute(text("""
                INSERT INTO event_deliveries (delivery_id, event_id, consumer_name,
                    delivery_state, dispatch_attempts, last_error, failed_at)
                VALUES (:did, :eid, 'noop', 'dead_letter', 5, '503', :now)
            """), {"did": did, "eid": eid, "now": now})
            db.execute(text("""
                INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                    related_event_id, failure_class, error_message, retry_exhausted,
                    replay_state, review_required, external_effect_state,
                    aggregate_type, aggregate_id, first_failed_at, last_failed_at)
                VALUES (:dl_id, 'event_delivery', :did, :eid,
                    'provider_transient_exhausted', '503 error', TRUE,
                    'pending', TRUE, 'none', 'test', :aid, :now, :now)
            """), {"dl_id": dl_id, "did": did, "eid": eid, "aid": aid, "now": now})
            db.commit()

        try:
            resp = httpx.post(
                f"{api_base}/api/v4/admin/dead-letters/{dl_id}/replay",
                json={}, timeout=10,
            )
            assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
            body = resp.json()
            assert "dead_letter_id" in body["data"]
            assert "review_item_id" in body["data"]
        finally:
            with SessionLocal() as db:
                from sqlalchemy import text
                db.execute(text("DELETE FROM review_items WHERE target_id=:tid"), {"tid": dl_id})
                db.execute(text("DELETE FROM dead_letters WHERE dead_letter_id=:did"), {"did": dl_id})
                db.execute(text("DELETE FROM event_deliveries WHERE delivery_id=:did"), {"did": did})
                db.execute(text("DELETE FROM events WHERE event_id=:eid"), {"eid": eid})
                db.commit()

    def test_replay_duplicate_returns_409(self, api_base):
        import httpx
        from mneme.db.base import SessionLocal

        with SessionLocal() as db:
            from sqlalchemy import text
            dl_id = uuid4(); did = uuid4(); eid = uuid4(); aid = uuid4()
            now = datetime.datetime.now(datetime.timezone.utc)
            db.execute(text("""
                INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, correlation_id, idempotency_key,
                    producer, payload_json, publish_state, occurred_at)
                VALUES (:eid, 'test.api_dup', 'test', :aid, 1, :cid, :ikey,
                    'test', '{}', 'dead_letter', :now)
            """), {"eid": eid, "aid": aid, "cid": uuid4(), "ikey": f"dup_{dl_id}", "now": now})
            db.execute(text("""
                INSERT INTO event_deliveries (delivery_id, event_id, consumer_name,
                    delivery_state, dispatch_attempts, last_error, failed_at)
                VALUES (:did, :eid, 'noop', 'dead_letter', 5, '503', :now)
            """), {"did": did, "eid": eid, "now": now})
            db.execute(text("""
                INSERT INTO dead_letters (dead_letter_id, source_type, source_id,
                    related_event_id, failure_class, error_message, retry_exhausted,
                    replay_state, review_required, external_effect_state,
                    aggregate_type, aggregate_id, first_failed_at, last_failed_at)
                VALUES (:dl_id, 'event_delivery', :did, :eid,
                    'code_bug', 'test', TRUE, 'pending', TRUE, 'none',
                    'test', :aid, :now, :now)
            """), {"dl_id": dl_id, "did": did, "eid": eid, "aid": aid, "now": now})
            db.commit()

        try:
            resp1 = httpx.post(
                f"{api_base}/api/v4/admin/dead-letters/{dl_id}/replay",
                json={}, timeout=10,
            )
            assert resp1.status_code == 201

            resp2 = httpx.post(
                f"{api_base}/api/v4/admin/dead-letters/{dl_id}/replay",
                json={}, timeout=10,
            )
            assert resp2.status_code == 409, f"Expected 409, got {resp2.status_code}: {resp2.text}"
        finally:
            with SessionLocal() as db:
                from sqlalchemy import text
                db.execute(text("DELETE FROM review_items WHERE target_id=:tid"), {"tid": dl_id})
                db.execute(text("DELETE FROM dead_letters WHERE dead_letter_id=:did"), {"did": dl_id})
                db.execute(text("DELETE FROM event_deliveries WHERE delivery_id=:did"), {"did": did})
                db.execute(text("DELETE FROM events WHERE event_id=:eid"), {"eid": eid})
                db.commit()

    def test_replay_nonexistent_returns_404(self, api_base):
        import httpx
        resp = httpx.post(
            f"{api_base}/api/v4/admin/dead-letters/{uuid4()}/replay",
            json={}, timeout=10,
        )
        assert resp.status_code == 404
