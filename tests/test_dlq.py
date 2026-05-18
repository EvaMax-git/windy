"""P2-03 DLQ ingestion & management tests.

Covers:
1. ``classify_failure()`` — all 5 failure_class categories.
2. DLQ data-access layer — pagination, filtering, single-row lookup.
3. DLQ admin API endpoints — ``GET /admin/dead-letters`` and
   ``GET /admin/dead-letters/{id}``.
4. End-to-end: failed dispatch → dead_letters table row.

Uses SQLite in-memory for DB-level logic with SessionLocal patching.
"""

from __future__ import annotations

import datetime
import json as _json
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

# ── SQLite UUID adapter ──────────────────────────────────────────────────────
sqlite3.register_adapter(UUID, lambda u: str(u))

# DO NOT set DATABASE_URL globally — other tests need PostgreSQL.
# DO NOT delete mneme modules from sys.modules — that corrupts other tests.
# This test file uses its own SQLite engines with SessionLocal patching.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.db.dead_letters import (  # noqa: E402
    get_dead_letter_by_id,
    get_dead_letters,
)
from mneme.worker.retry_sweeper import classify_failure  # noqa: E402


# ============================================================================
# SQLite PG-compatibility helpers
# ============================================================================

def _register_sqlite_compat(engine) -> None:
    """Register ``now()`` and ``gen_random_uuid()`` so PostgreSQL-authored
    SQL works against the SQLite test database."""
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, conn_record):
        # Use space separator (SQLite default format) so string comparisons
        # against datetime bind-parameters (also space-separated) are correct.
        dbapi_conn.create_function(
            "now", 0,
            lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(" "),
        )
        dbapi_conn.create_function(
            "gen_random_uuid", 0,
            lambda: str(_uuid.uuid4()),
        )


def _build_dlq_tables(engine) -> None:
    """Create ``dead_letters`` table for DAL tests (SQLite subset)."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE dead_letters (
              dead_letter_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              source_type TEXT NOT NULL,
              source_id TEXT NOT NULL,
              related_event_id TEXT,
              aggregate_type TEXT,
              aggregate_id TEXT,
              failure_class TEXT NOT NULL,
              error_code TEXT,
              error_message TEXT NOT NULL,
              retry_exhausted INTEGER NOT NULL DEFAULT 0,
              external_effect_state TEXT NOT NULL DEFAULT 'none',
              replay_state TEXT NOT NULL DEFAULT 'pending',
              review_required INTEGER NOT NULL DEFAULT 0,
              payload_json TEXT NOT NULL DEFAULT '{}',
              first_failed_at TIMESTAMP NOT NULL DEFAULT (now()),
              last_failed_at TIMESTAMP NOT NULL DEFAULT (now()),
              replayed_at TIMESTAMP,
              resolved_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT (now()),
              updated_at TIMESTAMP NOT NULL DEFAULT (now())
            )
        """))


def _build_e2e_tables(engine) -> None:
    """Create events, event_deliveries, and dead_letters for E2E tests."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE events (
              event_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              event_type TEXT NOT NULL,
              aggregate_type TEXT NOT NULL,
              aggregate_id TEXT NOT NULL,
              aggregate_version BIGINT NOT NULL DEFAULT 1,
              correlation_id TEXT NOT NULL,
              causation_id TEXT,
              idempotency_key TEXT NOT NULL UNIQUE,
              producer TEXT NOT NULL DEFAULT 'test',
              payload_json TEXT NOT NULL DEFAULT '{}',
              visibility TEXT NOT NULL DEFAULT 'internal',
              publish_state TEXT NOT NULL DEFAULT 'pending',
              occurred_at TIMESTAMP NOT NULL DEFAULT (now()),
              committed_at TIMESTAMP NOT NULL DEFAULT (now()),
              published_at TIMESTAMP,
              last_error TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE event_deliveries (
              delivery_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              event_id TEXT NOT NULL,
              consumer_name TEXT NOT NULL,
              delivery_state TEXT NOT NULL DEFAULT 'pending',
              dispatch_attempts INTEGER NOT NULL DEFAULT 0,
              last_dispatched_at TIMESTAMP,
              acknowledged_at TIMESTAMP,
              failed_at TIMESTAMP,
              last_error TEXT,
              lease_expires_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT (now()),
              updated_at TIMESTAMP NOT NULL DEFAULT (now()),
              UNIQUE (event_id, consumer_name)
            )
        """))
        conn.execute(text("""
            CREATE TABLE dead_letters (
              dead_letter_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
              source_type TEXT NOT NULL,
              source_id TEXT NOT NULL,
              related_event_id TEXT,
              aggregate_type TEXT,
              aggregate_id TEXT,
              failure_class TEXT NOT NULL,
              error_code TEXT,
              error_message TEXT NOT NULL,
              retry_exhausted INTEGER NOT NULL DEFAULT 0,
              external_effect_state TEXT NOT NULL DEFAULT 'none',
              replay_state TEXT NOT NULL DEFAULT 'pending',
              review_required INTEGER NOT NULL DEFAULT 0,
              payload_json TEXT NOT NULL DEFAULT '{}',
              first_failed_at TIMESTAMP NOT NULL DEFAULT (now()),
              last_failed_at TIMESTAMP NOT NULL DEFAULT (now()),
              replayed_at TIMESTAMP,
              resolved_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL DEFAULT (now()),
              updated_at TIMESTAMP NOT NULL DEFAULT (now())
            )
        """))


def _make_dlq_engine():
    """Create an in-memory SQLite engine with dead_letters table."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _register_sqlite_compat(engine)
    _build_dlq_tables(engine)
    return engine


def _make_e2e_engine():
    """Create an in-memory SQLite engine with events, deliveries, dead_letters."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _register_sqlite_compat(engine)
    _build_e2e_tables(engine)
    return engine


# ============================================================================
# Monkey-patch _isoformat to handle SQLite string timestamps
# ============================================================================

def _patch_isoformat():
    """SQLite returns timestamps as ISO strings, but _isoformat expects
    datetime objects."""
    import mneme.db.dead_letters as dl_mod

    _orig = dl_mod._isoformat

    def _safe_isoformat(dt):
        if dt is None:
            return None
        if isinstance(dt, str):
            return dt  # SQLite returns ISO strings already
        return dt.isoformat()

    dl_mod._isoformat = _safe_isoformat
    return _orig


def _unpatch_isoformat(orig):
    import mneme.db.dead_letters as dl_mod
    dl_mod._isoformat = orig


@pytest.fixture(autouse=True)
def _auto_patch_isoformat():
    orig = _patch_isoformat()
    yield
    _unpatch_isoformat(orig)


# ============================================================================
# Helpers
# ============================================================================

def _parse_json(val):
    """Normalise JSON: SQLite TEXT returns str, PG JSONB returns dict."""
    if isinstance(val, str):
        try:
            return _json.loads(val)
        except (_json.JSONDecodeError, TypeError):
            return {}
    if isinstance(val, dict):
        return val
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# 1. failure_class classification
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "error_message, expected_class",
    [
        # provider_transient_exhausted
        ("connection timeout", "provider_transient_exhausted"),
        ("503 Service Unavailable", "provider_transient_exhausted"),
        ("DNS resolution failed", "provider_transient_exhausted"),
        ("too many requests 429", "provider_transient_exhausted"),
        ("rate limit exceeded", "provider_transient_exhausted"),
        ("socket timeout", "provider_transient_exhausted"),
        ("Connection reset by peer", "provider_transient_exhausted"),
        # policy_denied_terminal
        ("permission denied by policy", "policy_denied_terminal"),
        ("access denied", "policy_denied_terminal"),
        ("quota exceeded", "policy_denied_terminal"),
        # payload_invalid
        ("validation error: missing field", "payload_invalid"),
        ("invalid payload schema", "payload_invalid"),
        ("Bad Request 400", "payload_invalid"),
        # external_side_effect_unknown
        ("duplicate key", "external_side_effect_unknown"),
        ("conflict 409", "external_side_effect_unknown"),
        ("already exists", "external_side_effect_unknown"),
        # code_bug (default)
        ("NullPointerException in handler", "code_bug"),
        ("unexpected error occurred", "code_bug"),
        ("", "code_bug"),
        (None, "code_bug"),
    ],
)
def test_classify_failure(error_message, expected_class):
    """Every keyword class maps to the correct failure_class."""
    assert classify_failure(error_message) == expected_class


# ─────────────────────────────────────────────────────────────────────────────
# 2. DLQ data-access layer
# ─────────────────────────────────────────────────────────────────────────────

class TestDeadLettersDAL:
    """Integration tests for the dead_letters data-access functions.

    Uses SQLite in-memory with SessionLocal patching so the DAL queries
    are exercised in isolation without a real PostgreSQL.
    """

    @pytest.fixture
    def db_setup(self):
        engine = _make_dlq_engine()
        session = Session(engine)
        with patch("mneme.db.dead_letters.SessionLocal") as mock_sl:
            mock_sl.return_value.__enter__.return_value = session
            mock_sl.return_value.__exit__.return_value = False
            yield session, mock_sl
        session.close()
        engine.dispose()

    def test_get_dead_letters_empty(self, db_setup):
        """Paginated query on a table with no matching rows returns empty list."""
        session, _ = db_setup
        rows, total = get_dead_letters(page=1, page_size=10)
        assert isinstance(rows, list)
        assert rows == []
        assert total == 0

    def test_get_dead_letters_pagination(self, db_setup):
        """Verify pagination math: page 1 vs page 2, total counts."""
        session, _ = db_setup
        unique_source = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)

        for i in range(5):
            session.execute(text("""
                INSERT INTO dead_letters (
                    source_type, source_id, failure_class, error_message,
                    retry_exhausted, replay_state, review_required,
                    external_effect_state, aggregate_type, aggregate_id,
                    first_failed_at, last_failed_at
                ) VALUES (
                    'event_delivery', :source_id, 'code_bug', :msg,
                    TRUE, 'pending', TRUE, 'none', 'test', :agg_id,
                    :now, :now
                )
            """), {
                "source_id": str(uuid4()),
                "msg": f"test dlq pagination {unique_source} {i}",
                "agg_id": str(uuid4()),
                "now": now,
            })
        session.commit()

        # Page 1 (size 3)
        rows1, total1 = get_dead_letters(page=1, page_size=3)
        assert total1 >= 5
        assert len(rows1) <= 3

        # Page 2 (size 3)
        rows2, total2 = get_dead_letters(page=2, page_size=3)
        assert total2 == total1
        assert len(rows2) <= 3

        # Ensure pages don't overlap
        ids1 = {r["dead_letter_id"] for r in rows1}
        ids2 = {r["dead_letter_id"] for r in rows2}
        assert ids1.isdisjoint(ids2)

    def test_get_dead_letters_filter_by_failure_class(self, db_setup):
        """Filter by failure_class returns only matching rows."""
        session, _ = db_setup
        unique_marker = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)

        for fc in ["provider_transient_exhausted", "policy_denied_terminal",
                    "payload_invalid", "code_bug",
                    "external_side_effect_unknown"]:
            session.execute(text("""
                INSERT INTO dead_letters (
                    source_type, source_id, failure_class, error_message,
                    retry_exhausted, replay_state, review_required,
                    external_effect_state, aggregate_type, aggregate_id,
                    first_failed_at, last_failed_at
                ) VALUES (
                    'event_delivery', :source_id, :fc, :msg,
                    TRUE, 'pending', TRUE, 'none', 'test', :agg_id,
                    :now, :now
                )
            """), {
                "source_id": str(uuid4()),
                "fc": fc,
                "msg": f"test dlq filter {unique_marker} {fc}",
                "agg_id": str(uuid4()),
                "now": now,
            })
        session.commit()

        rows, total = get_dead_letters(
            page=1, page_size=20,
            failure_class="code_bug",
        )
        assert total >= 1
        for r in rows:
            assert r["failure_class"] == "code_bug"

    def test_get_dead_letters_filter_by_replay_state(self, db_setup):
        """Filter by replay_state returns only matching rows."""
        session, _ = db_setup
        unique_marker = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)

        for rs in ["pending", "under_review", "replayed", "cancelled", "resolved"]:
            session.execute(text("""
                INSERT INTO dead_letters (
                    source_type, source_id, failure_class, error_message,
                    retry_exhausted, replay_state, review_required,
                    external_effect_state, aggregate_type, aggregate_id,
                    first_failed_at, last_failed_at
                ) VALUES (
                    'event_delivery', :source_id, 'code_bug', :msg,
                    TRUE, :rs, TRUE, 'none', 'test', :agg_id,
                    :now, :now
                )
            """), {
                "source_id": str(uuid4()),
                "msg": f"test dlq replay {unique_marker} {rs}",
                "rs": rs,
                "agg_id": str(uuid4()),
                "now": now,
            })
        session.commit()

        rows, total = get_dead_letters(
            page=1, page_size=20,
            replay_state="pending",
        )
        assert total >= 1
        for r in rows:
            assert r["replay_state"] == "pending"

    def test_get_dead_letters_filter_by_source_type(self, db_setup):
        """Filter by source_type returns only matching rows."""
        session, _ = db_setup
        unique_marker = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)

        for st in ["event_delivery", "job", "provider_call", "importer"]:
            session.execute(text("""
                INSERT INTO dead_letters (
                    source_type, source_id, failure_class, error_message,
                    retry_exhausted, replay_state, review_required,
                    external_effect_state, aggregate_type, aggregate_id,
                    first_failed_at, last_failed_at
                ) VALUES (
                    :st, :source_id, 'code_bug', :msg,
                    TRUE, 'pending', TRUE, 'none', 'test', :agg_id,
                    :now, :now
                )
            """), {
                "st": st,
                "source_id": str(uuid4()),
                "msg": f"test dlq source {unique_marker} {st}",
                "agg_id": str(uuid4()),
                "now": now,
            })
        session.commit()

        rows, total = get_dead_letters(
            page=1, page_size=20,
            source_type="job",
        )
        assert total >= 1
        for r in rows:
            assert r["source_type"] == "job"

    def test_get_dead_letters_time_range(self, db_setup):
        """Filter by created_after / created_before returns bounded results."""
        session, _ = db_setup
        unique_marker = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)
        one_hour_ago = now - datetime.timedelta(hours=1)

        session.execute(text("""
            INSERT INTO dead_letters (
                source_type, source_id, failure_class, error_message,
                retry_exhausted, replay_state, review_required,
                external_effect_state, aggregate_type, aggregate_id,
                first_failed_at, last_failed_at, created_at
            ) VALUES (
                'event_delivery', :source_id, 'code_bug', :msg,
                TRUE, 'pending', TRUE, 'none', 'test', :agg_id,
                :now, :now, :old_ts
            )
        """), {
            "source_id": str(uuid4()),
            "msg": f"test dlq time old {unique_marker}",
            "agg_id": str(uuid4()),
            "now": one_hour_ago,
            "old_ts": one_hour_ago,
        })
        session.commit()

        # Query with created_after = 30 min ago → old row excluded
        rows, total = get_dead_letters(
            page=1, page_size=20,
            created_after=now - datetime.timedelta(minutes=30),
        )
        # The old row (1 hr ago) must NOT appear in results
        for r in rows:
            msg = str(r.get("error_message", ""))
            assert unique_marker not in msg, (
                f"Old row ({one_hour_ago.isoformat()}) leaked through "
                f"created_after filter"
            )

    def test_get_dead_letter_by_id_returns_row(self, db_setup):
        """Fetch a single dead-letter by PK."""
        session, _ = db_setup
        unique_marker = str(uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)
        src_id = str(uuid4())
        agg_id = str(uuid4())

        result = session.execute(text("""
            INSERT INTO dead_letters (
                source_type, source_id, failure_class, error_message,
                retry_exhausted, replay_state, review_required,
                external_effect_state, aggregate_type, aggregate_id,
                first_failed_at, last_failed_at
            ) VALUES (
                'event_delivery', :source_id, 'code_bug', :msg,
                TRUE, 'pending', TRUE, 'none', 'test', :agg_id,
                :now, :now
            )
            RETURNING dead_letter_id
        """), {
            "source_id": src_id,
            "msg": f"test dlq by id {unique_marker}",
            "agg_id": agg_id,
            "now": now,
        }).mappings().one()
        dl_id = result["dead_letter_id"]
        session.commit()

        row = get_dead_letter_by_id(UUID(dl_id))
        assert row is not None
        assert row["failure_class"] == "code_bug"
        assert row["replay_state"] == "pending"
        assert row["review_required"] is True
        assert row["retry_exhausted"] is True
        assert row["source_type"] == "event_delivery"

    def test_get_dead_letter_by_id_not_found(self, db_setup):
        """Query for a non-existent dead_letter_id returns None."""
        session, _ = db_setup
        row = get_dead_letter_by_id(uuid4())
        assert row is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. DLQ admin API (integration with HTTP client)
# ─────────────────────────────────────────────────────────────────────────────

class TestDLQApi:
    """Integration tests against the running FastAPI application.

    Require a live server at ``API_BASE_URL`` (default ``http://localhost:8000``).
    These tests make HTTP calls only; they do not require direct DB access.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_server(self):
        """Skip API tests when the server is not reachable."""
        import os as _os
        import httpx
        base = _os.environ.get("API_BASE_URL", "http://localhost:8000")
        try:
            resp = httpx.get(f"{base}/health/live", timeout=2)
            if resp.status_code != 200:
                pytest.skip("Server not healthy")
        except Exception:
            pytest.skip("Server not reachable")

    @pytest.fixture
    def api_base(self):
        import os as _os
        return _os.environ.get("API_BASE_URL", "http://localhost:8000")

    def test_list_dead_letters_returns_200(self, api_base):
        """GET /api/v4/admin/dead-letters returns 200 with envelope."""
        import httpx
        resp = httpx.get(
            f"{api_base}/api/v4/admin/dead-letters",
            params={"page": 1, "page_size": 10},
            timeout=5,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "request_id" in body
        assert "data" in body
        assert "items" in body["data"]
        assert "page_info" in body["data"]

    def test_list_dead_letters_with_filters(self, api_base):
        """Filter parameters are accepted by the API."""
        import httpx
        resp = httpx.get(
            f"{api_base}/api/v4/admin/dead-letters",
            params={
                "page": 1,
                "page_size": 5,
                "failure_class": "code_bug",
                "replay_state": "pending",
                "source_type": "event_delivery",
            },
            timeout=5,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["page_info"]["page"] == 1

    def test_get_dead_letter_detail_404(self, api_base):
        """GET a non-existent dead_letter returns 404."""
        import httpx
        fake_id = uuid4()
        resp = httpx.get(
            f"{api_base}/api/v4/admin/dead-letters/{fake_id}",
            timeout=5,
        )
        assert resp.status_code == 404

    @pytest.mark.skip(
        reason="Requires direct PostgreSQL access to seed data; "
               "use this test only in full-integration environments"
    )
    def test_get_dead_letter_detail_200(self, api_base):
        """GET an existing dead_letter returns 200 with detail.

        NOTE: This test seeds data via SessionLocal → must connect to the
        same PostgreSQL that the API server uses.  Skipped by default in
        SQLite-based test runs.
        """
        import httpx
        from mneme.db.base import SessionLocal as PgSessionLocal
        from uuid import uuid4 as _uuid4
        from datetime import datetime as _dt, timezone as _tz

        unique_marker = str(_uuid4())
        now = _dt.now(_tz.utc)
        src_id = _uuid4()
        agg_id = _uuid4()
        dl_id = None

        with PgSessionLocal() as db:
            result = db.execute(text("""
                INSERT INTO dead_letters (
                    source_type, source_id, failure_class, error_message,
                    retry_exhausted, replay_state, review_required,
                    external_effect_state, aggregate_type, aggregate_id,
                    first_failed_at, last_failed_at
                ) VALUES (
                    'event_delivery', :source_id, 'payload_invalid', :msg,
                    true, 'pending', true, 'none', 'test_api', :agg_id,
                    :now, :now
                )
                RETURNING dead_letter_id
            """), {
                "source_id": src_id,
                "msg": f"test api detail {unique_marker}",
                "agg_id": agg_id,
                "now": now,
            }).mappings().one()
            dl_id = result["dead_letter_id"]
            db.commit()

        try:
            resp = httpx.get(
                f"{api_base}/api/v4/admin/dead-letters/{dl_id}",
                timeout=5,
            )
            assert resp.status_code == 200
            body = resp.json()
            data = body["data"]
            assert data["dead_letter_id"] == str(dl_id)
            assert data["failure_class"] == "payload_invalid"
            assert data["replay_state"] == "pending"
            assert data["review_required"] is True
            assert data["source_type"] == "event_delivery"
        finally:
            with PgSessionLocal() as db:
                db.execute(text(
                    "DELETE FROM dead_letters WHERE error_message LIKE :pat"
                ), {"pat": f"%{unique_marker}%"})
                db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 4. End-to-end: dispatch failure → dead_letters
# ─────────────────────────────────────────────────────────────────────────────

class TestDLQEndToEnd:
    """Simulate the full path: event → failed delivery → dead_letters.

    Uses SQLite in-memory with SessionLocal patching in both the DAL module
    and the retry_sweeper module.
    """

    @pytest.fixture
    def db_setup(self):
        engine = _make_e2e_engine()
        session = Session(engine)

        # Patch SessionLocal at all import sites used by the sweeper
        patches = [
            patch("mneme.worker.retry_sweeper.SessionLocal"),
            patch("mneme.db.dead_letters.SessionLocal"),
        ]
        mocks = []
        for p in patches:
            mock_sl = p.start()
            mock_sl.return_value.__enter__.return_value = session
            mock_sl.return_value.__exit__.return_value = False
            mocks.append(mock_sl)

        yield engine, session

        for p in reversed(patches):
            p.stop()
        session.close()
        engine.dispose()

    def test_promote_exhausted_inserts_dead_letter(self, db_setup):
        """Verify that the RetrySweeper._promote_exhausted actually inserts a
        row into the ``dead_letters`` table with correct fields."""
        from mneme.worker.retry_sweeper import RetrySweeper

        engine, session = db_setup
        unique_marker = str(uuid4())
        event_id = uuid4()
        agg_id = uuid4()
        corr_id = uuid4()
        now = datetime.datetime.now(datetime.timezone.utc)

        # Seed: create event
        session.execute(text("""
            INSERT INTO events (
                event_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, correlation_id, idempotency_key,
                producer, payload_json, visibility, publish_state,
                occurred_at, committed_at
            ) VALUES (
                :event_id, 'test.dlq_e2e', 'test_dlq', :agg_id,
                1, :corr_id, :idem_key,
                'pytest', '{}', 'internal', 'dispatched',
                :now, :now
            )
        """), {
            "event_id": str(event_id),
            "agg_id": str(agg_id),
            "corr_id": str(corr_id),
            "idem_key": f"dlq_e2e_{unique_marker}",
            "now": now,
        })

        # Create a failed delivery at max_attempts
        result = session.execute(text("""
            INSERT INTO event_deliveries (
                event_id, consumer_name, delivery_state, dispatch_attempts,
                last_error, failed_at, last_dispatched_at
            ) VALUES (
                :event_id, 'noop', 'failed', 5,
                :error, :now, :one_hour_ago
            )
            RETURNING delivery_id
        """), {
            "event_id": str(event_id),
            "error": f"503 Service Unavailable – e2e test {unique_marker}",
            "now": now,
            "one_hour_ago": (
                datetime.datetime.now(datetime.timezone.utc) -
                datetime.timedelta(hours=1)
            ),
        }).mappings().one()
        delivery_id = result["delivery_id"]
        session.commit()

        # Run the sweeper to promote → dead_letters
        sweeper = RetrySweeper(max_attempts=5)
        sweep_result = sweeper.sweep()

        assert sweep_result["dead_lettered"] >= 1

        # Verify the dead_letters row
        dl = session.execute(text("""
            SELECT * FROM dead_letters
            WHERE source_id = :delivery_id
        """), {"delivery_id": delivery_id}).mappings().first()

        assert dl is not None
        assert dl["source_type"] == "event_delivery"
        # SQLite stores UUIDs as strings; compare as str
        assert dl["related_event_id"] == str(event_id)
        assert dl["failure_class"] == "provider_transient_exhausted"
        assert bool(dl["retry_exhausted"]) is True
        assert dl["replay_state"] == "pending"
        assert bool(dl["review_required"]) is True
        assert dl["external_effect_state"] == "none"

        # Verify the delivery was marked dead_letter
        delivery = session.execute(text("""
            SELECT delivery_state FROM event_deliveries
            WHERE delivery_id = :delivery_id
        """), {"delivery_id": delivery_id}).mappings().first()
        assert delivery is not None
        assert delivery["delivery_state"] == "dead_letter"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Schema validation
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaAlignment:
    """Ensure Pydantic schemas align with DDL CHECK constraints."""

    def test_failure_class_enum_matches_ddl(self):
        """All 5 DDL failure_class values are present."""
        from mneme.schemas.dead_letters import FailureClass

        values = {e.value for e in FailureClass}
        expected = {
            "provider_transient_exhausted",
            "policy_denied_terminal",
            "payload_invalid",
            "code_bug",
            "external_side_effect_unknown",
        }
        assert values == expected

    def test_replay_state_enum_matches_ddl(self):
        """All 5 DDL replay_state values are present."""
        from mneme.schemas.dead_letters import ReplayState

        values = {e.value for e in ReplayState}
        expected = {"pending", "under_review", "replayed", "cancelled", "resolved"}
        assert values == expected

    def test_source_type_enum_matches_ddl(self):
        """All 4 DDL source_type values are present."""
        from mneme.schemas.dead_letters import SourceType

        values = {e.value for e in SourceType}
        expected = {"event_delivery", "job", "provider_call", "importer"}
        assert values == expected

    def test_external_effect_state_enum_matches_ddl(self):
        """All 4 DDL external_effect_state values are present."""
        from mneme.schemas.dead_letters import ExternalEffectState

        values = {e.value for e in ExternalEffectState}
        expected = {"none", "unknown", "confirmed_done", "confirmed_not_done"}
        assert values == expected

    def test_dead_letter_read_has_all_ddl_columns(self):
        """Every DDL column (except PK) is represented in the read model."""
        from mneme.schemas.dead_letters import DeadLetterRead

        fields = set(DeadLetterRead.model_fields.keys())

        # DDL columns in dead_letters (from 0001_baseline_45_tables.py)
        ddl_columns = {
            "dead_letter_id",
            "source_type",
            "source_id",
            "related_event_id",
            "aggregate_type",
            "aggregate_id",
            "failure_class",
            "error_code",
            "error_message",
            "retry_exhausted",
            "external_effect_state",
            "replay_state",
            "review_required",
            "payload_json",
            "first_failed_at",
            "last_failed_at",
            "replayed_at",
            "resolved_at",
            "created_at",
            "updated_at",
        }

        missing = ddl_columns - fields
        extra = fields - ddl_columns
        assert not missing, f"Schema missing DDL columns: {missing}"
        assert not extra, f"Schema has extra fields not in DDL: {extra}"
