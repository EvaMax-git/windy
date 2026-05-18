"""P1-10: Worker / Dispatcher / Outbox Poller tests.

Covers:
1. NoopConsumer behavior (name, can_handle, dispatch).
2. DispatchResult / DispatchOutcome enums and factory methods.
3. Dispatcher registry (register, duplicate rejection, consumers list).
4. PendingEvent frozen dataclass.
5. _coerce_uuid helper from both poller and dispatcher.
6. Dispatcher dispatch flow with in-memory SQLite DB:
   - empty pending list -> 0 dispatched.
   - single event -> event marked dispatched, delivery created & acked.
   - failed consumer -> delivery marked failed.
   - multiple events -> all processed.
   - event_deliveries UPSERT idempotency (re-dispatch of same event).
7. Outbox poller (fetch_pending_events) -- basic behavior via mock.
8. RedisConnection graceful degradation unit tests.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import uuid as _uuid
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

# Register UUID adapter early so SQLite can handle Python UUID bind params
sqlite3.register_adapter(UUID, lambda u: str(u))
from sqlalchemy.pool import StaticPool

# Ensure test env vars set before any mneme imports (setdefault to preserve existing)
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# DO NOT delete mneme modules from sys.modules — other tests need them.
from mneme.worker.dispatcher import (  # noqa: E402
    Consumer,
    Dispatcher,
    DispatchOutcome,
    DispatchResult,
    NoopConsumer,
    _coerce_uuid as _dispatcher_coerce_uuid,
)
from mneme.worker.poller import (  # noqa: E402
    PendingEvent,
    _coerce_uuid as _poller_coerce_uuid,
)


# ============================================================================
# SQLite PG-compatibility helpers
# ============================================================================

def _register_sqlite_compat(engine) -> None:
    """Register ``now()`` and ``gen_random_uuid()`` functions so that
    PostgreSQL-authored SQL works against the SQLite test database."""

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


def _build_dispatcher_tables(engine) -> None:
    """Create ``events`` and ``event_deliveries`` tables for dispatcher tests
    with SQLite-compatible DDL."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE events (
              event_id TEXT PRIMARY KEY DEFAULT (gen_random_uuid()),
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


def _make_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _register_sqlite_compat(engine)
    _build_dispatcher_tables(engine)
    return engine


def _insert_pending_event(db: Session, **kw) -> UUID:
    eid = str(kw.get("event_id", uuid4()))
    db.execute(text("""INSERT INTO events (
        event_id, event_type, aggregate_type, aggregate_id,
        aggregate_version, correlation_id, idempotency_key,
        producer, payload_json, publish_state, occurred_at
    ) VALUES (
        :eid, :etype, :atype, :aid,
        :aver, :cid, :ikey,
        :prod, :payload, 'pending', now()
    )"""), {
        "eid": eid, "etype": kw.get("event_type", "item.created"),
        "atype": kw.get("aggregate_type", "item"),
        "aid": str(kw.get("aggregate_id", uuid4())),
        "aver": kw.get("aggregate_version", 1),
        "cid": str(kw.get("correlation_id", uuid4())),
        "ikey": kw.get("idempotency_key", "test-key"),
        "prod": kw.get("producer", "mneme-api"),
        "payload": kw.get("payload_json", '{"request_id":"r1"}'),
    })
    return UUID(eid)


def _make_pending_event_from_row(row) -> PendingEvent:
    import json
    p = row["payload_json"]
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except (json.JSONDecodeError, TypeError):
            p = {}
    elif not isinstance(p, dict):
        p = {}
    return PendingEvent(
        event_id=UUID(row["event_id"]),
        event_type=row["event_type"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=UUID(row["aggregate_id"]),
        aggregate_version=row["aggregate_version"],
        correlation_id=UUID(row["correlation_id"]),
        causation_id=UUID(row["causation_id"]) if row["causation_id"] else None,
        idempotency_key=row["idempotency_key"],
        producer=row["producer"],
        payload_json=p,
        visibility=row["visibility"] or "internal",
    )


class _SessionProxy:
    """Context-manager proxy that returns the shared session without closing it."""
    def __init__(self, session):
        self._session = session
    def __enter__(self):
        return self._session
    def __exit__(self, *args):
        return False


# ============================================================================
# NoopConsumer Tests
# ============================================================================

class TestNoopConsumer:
    def test_name_returns_noop(self) -> None:
        assert NoopConsumer().name == "noop"

    def test_can_handle_always_true(self) -> None:
        c = NoopConsumer()
        assert c.can_handle("item.created") is True
        assert c.can_handle("") is True

    def test_dispatch_returns_ack(self) -> None:
        r = NoopConsumer().dispatch(
            event_id=uuid4(), event_type="x", aggregate_type="x",
            aggregate_id=uuid4(), payload={}, delivery_id=uuid4(),
        )
        assert r.outcome == DispatchOutcome.acknowledged
        assert r.error is None

    def test_dispatch_never_raises(self) -> None:
        c = NoopConsumer()
        for _ in range(10):
            c.dispatch(event_id=uuid4(), event_type="t", aggregate_type="x",
                       aggregate_id=uuid4(), payload={}, delivery_id=uuid4())


# ============================================================================
# DispatchOutcome / DispatchResult Tests
# ============================================================================

class TestDispatchOutcome:
    def test_values(self) -> None:
        assert DispatchOutcome.acknowledged.value == "acknowledged"
        assert DispatchOutcome.failed.value == "failed"

    def test_parse(self) -> None:
        assert DispatchOutcome("acknowledged") == DispatchOutcome.acknowledged
        with pytest.raises(ValueError):
            DispatchOutcome("bogus")


class TestDispatchResult:
    def test_ack_factory(self) -> None:
        r = DispatchResult.ack()
        assert r.outcome == DispatchOutcome.acknowledged
        assert r.error is None

    def test_fail_factory(self) -> None:
        r = DispatchResult.fail("timeout")
        assert r.outcome == DispatchOutcome.failed
        assert r.error == "timeout"

    def test_equality(self) -> None:
        assert DispatchResult.ack() == DispatchResult(
            outcome=DispatchOutcome.acknowledged, error=None)

    def test_inequality(self) -> None:
        assert DispatchResult.ack() != DispatchResult.fail("e")


# ============================================================================
# Dispatcher Registry Tests
# ============================================================================

class _StubConsumer(Consumer):
    def __init__(self, name: str, handled: frozenset[str] | None = None):
        self._name = name
        self._handled = handled or frozenset()
    @property
    def name(self) -> str:
        return self._name
    def can_handle(self, event_type: str) -> bool:
        return event_type in self._handled
    def dispatch(self, **kw) -> DispatchResult:
        return DispatchResult.ack()


class TestDispatcherRegistry:
    def test_register_single(self) -> None:
        d = Dispatcher(); d.register(_StubConsumer("c1"))
        assert len(d.consumers) == 1

    def test_register_multiple(self) -> None:
        d = Dispatcher()
        d.register(_StubConsumer("c1"))
        d.register(_StubConsumer("c2"))
        assert len(d.consumers) == 2

    def test_duplicate_name_raises(self) -> None:
        d = Dispatcher(); d.register(_StubConsumer("c1"))
        with pytest.raises(ValueError, match="already registered"):
            d.register(_StubConsumer("c1"))

    def test_consumers_returns_copy(self) -> None:
        d = Dispatcher(); d.register(_StubConsumer("c1"))
        cs = d.consumers; cs.append(_StubConsumer("c2"))
        assert len(d.consumers) == 1


class TestDispatcherMatching:
    def test_matches_registered(self) -> None:
        d = Dispatcher()
        d.register(_StubConsumer("a", frozenset({"item.created"})))
        m = d._matching_consumers("item.created")
        assert len(m) == 1 and m[0].name == "a"

    def test_no_match(self) -> None:
        d = Dispatcher()
        d.register(_StubConsumer("a", frozenset({"item.created"})))
        assert d._matching_consumers("other") == []

    def test_multiple_same_type(self) -> None:
        d = Dispatcher()
        d.register(_StubConsumer("a", frozenset({"x"})))
        d.register(_StubConsumer("b", frozenset({"x"})))
        d.register(_StubConsumer("c", frozenset({"y"})))
        assert {c.name for c in d._matching_consumers("x")} == {"a", "b"}


class TestDispatchPendingEmpty:
    def test_empty_returns_zero(self) -> None:
        assert Dispatcher().dispatch_pending([]) == 0


# ============================================================================
# PendingEvent Tests
# ============================================================================

class TestPendingEvent:
    def test_creation(self) -> None:
        eid, aid, cid = uuid4(), uuid4(), uuid4()
        pe = PendingEvent(
            event_id=eid, event_type="x", aggregate_type="x",
            aggregate_id=aid, aggregate_version=1, correlation_id=cid,
            causation_id=None, idempotency_key="k", producer="p",
        )
        assert pe.event_id == eid
        assert pe.payload_json == {}

    def test_frozen_prevents_mutation(self) -> None:
        eid, aid, cid = uuid4(), uuid4(), uuid4()
        pe = PendingEvent(
            event_id=eid, event_type="x", aggregate_type="x",
            aggregate_id=aid, aggregate_version=1, correlation_id=cid,
            causation_id=None, idempotency_key="k", producer="p",
        )
        with pytest.raises(Exception):
            pe.event_type = "changed"  # type: ignore[misc]

    def test_event_id_str(self) -> None:
        eid, aid, cid = uuid4(), uuid4(), uuid4()
        pe = PendingEvent(
            event_id=eid, event_type="x", aggregate_type="x",
            aggregate_id=aid, aggregate_version=1, correlation_id=cid,
            causation_id=None, idempotency_key="k", producer="p",
        )
        assert pe.event_id_str == str(eid)

    def test_defaults(self) -> None:
        eid, aid, cid = uuid4(), uuid4(), uuid4()
        pe = PendingEvent(
            event_id=eid, event_type="x", aggregate_type="x",
            aggregate_id=aid, aggregate_version=1, correlation_id=cid,
            causation_id=None, idempotency_key="k", producer="p",
        )
        assert pe.visibility == "internal"


# ============================================================================
# _coerce_uuid Tests
# ============================================================================

class TestCoerceUuid:
    def test_uuid_passthrough(self) -> None:
        u = uuid4()
        assert _dispatcher_coerce_uuid(u) is u
        assert _poller_coerce_uuid(u) is u

    def test_str_conversion(self) -> None:
        u = uuid4()
        assert _dispatcher_coerce_uuid(str(u)) == u
        assert _poller_coerce_uuid(str(u)) == u

    def test_hex_no_dashes(self) -> None:
        s = "550e8400e29b41d4a716446655440000"
        assert isinstance(_dispatcher_coerce_uuid(s), UUID)

    def test_equivalent(self) -> None:
        u = uuid4()
        assert _dispatcher_coerce_uuid(u) == _poller_coerce_uuid(u)
        assert _dispatcher_coerce_uuid(str(u)) == _poller_coerce_uuid(str(u))


# ============================================================================
# Consumer Interface Tests
# ============================================================================

class TestConsumerInterface:
    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            Consumer()  # type: ignore[abstract]

    def test_noop_is_subclass(self) -> None:
        assert isinstance(NoopConsumer(), Consumer)


# ============================================================================
# DB-dependent Dispatch Flow Tests
# ============================================================================

@pytest.fixture
def db_setup():
    """In-memory SQLite engine + session with dispatcher tables and PG compat."""
    engine = _make_engine()
    session = Session(engine)
    yield engine, session
    session.close()
    engine.dispose()


def _patch_session(monkeypatch, session):
    import mneme.worker.dispatcher as m
    monkeypatch.setattr(m, "SessionLocal", lambda: _SessionProxy(session))


class TestDispatchFlowWithDB:
    """Integration tests for the full dispatch flow against SQLite."""

    def test_empty_dispatch(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher(); d.register(NoopConsumer())
        assert d.dispatch_pending([]) == 0

    def test_single_event_acked(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher(); d.register(NoopConsumer())
        eid = _insert_pending_event(session, idempotency_key="s1")
        session.commit()

        row = session.execute(
            text("SELECT * FROM events WHERE event_id=:e"), {"e": str(eid)}
        ).mappings().one()
        pe = _make_pending_event_from_row(row)

        assert d.dispatch_pending([pe]) == 1

        st = session.execute(
            text("SELECT publish_state FROM events WHERE event_id=:e"),
            {"e": str(eid)},
        ).scalar_one()
        assert st == "dispatched"

        dr = session.execute(
            text("SELECT delivery_state, consumer_name, dispatch_attempts "
                 "FROM event_deliveries WHERE event_id=:e"),
            {"e": str(eid)},
        ).mappings().one()
        assert dr["consumer_name"] == "noop"
        assert dr["delivery_state"] == "acknowledged"
        assert dr["dispatch_attempts"] >= 1

    def test_failed_consumer(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher()

        class Failer(Consumer):
            @property
            def name(self): return "failer"
            def can_handle(self, et): return True
            def dispatch(self, **kw): return DispatchResult.fail("boom")

        d.register(Failer())
        eid = _insert_pending_event(session, idempotency_key="f1")
        session.commit()

        row = session.execute(
            text("SELECT * FROM events WHERE event_id=:e"), {"e": str(eid)}
        ).mappings().one()
        pe = _make_pending_event_from_row(row)

        assert d.dispatch_pending([pe]) == 1

        dr = session.execute(
            text("SELECT delivery_state, last_error FROM event_deliveries "
                 "WHERE event_id=:e AND consumer_name='failer'"),
            {"e": str(eid)},
        ).mappings().one()
        assert dr["delivery_state"] == "failed"
        assert "boom" in (dr["last_error"] or "")

    def test_consumer_raising_caught(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher()

        class Raiser(Consumer):
            @property
            def name(self): return "raiser"
            def can_handle(self, et): return True
            def dispatch(self, **kw):
                raise RuntimeError("kaboom!")

        d.register(Raiser())
        eid = _insert_pending_event(session, idempotency_key="r1")
        session.commit()

        row = session.execute(
            text("SELECT * FROM events WHERE event_id=:e"), {"e": str(eid)}
        ).mappings().one()
        pe = _make_pending_event_from_row(row)

        assert d.dispatch_pending([pe]) == 1

        dr = session.execute(
            text("SELECT delivery_state, last_error FROM event_deliveries "
                 "WHERE event_id=:e AND consumer_name='raiser'"),
            {"e": str(eid)},
        ).mappings().one()
        assert dr["delivery_state"] == "failed"
        assert "kaboom!" in (dr["last_error"] or "")

    def test_re_dispatch_increments_attempts(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher()

        class AF(Consumer):
            @property
            def name(self): return "af"
            def can_handle(self, et): return True
            def dispatch(self, **kw): return DispatchResult.fail("retry")

        d.register(AF())
        eid = _insert_pending_event(session, idempotency_key="retry1")
        session.commit()

        row = session.execute(
            text("SELECT * FROM events WHERE event_id=:e"), {"e": str(eid)}
        ).mappings().one()
        pe = _make_pending_event_from_row(row)

        d.dispatch_pending([pe])
        a1 = session.execute(
            text("SELECT dispatch_attempts FROM event_deliveries "
                 "WHERE event_id=:e AND consumer_name='af'"),
            {"e": str(eid)},
        ).scalar_one()

        d.dispatch_pending([pe])
        a2 = session.execute(
            text("SELECT dispatch_attempts FROM event_deliveries "
                 "WHERE event_id=:e AND consumer_name='af'"),
            {"e": str(eid)},
        ).scalar_one()

        assert a2 > a1

    def test_batch_of_three(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher(); d.register(NoopConsumer())

        eids = [_insert_pending_event(session, idempotency_key=f"b{i}") for i in range(3)]
        session.commit()

        pes = []
        for eid in eids:
            row = session.execute(
                text("SELECT * FROM events WHERE event_id=:e"), {"e": str(eid)}
            ).mappings().one()
            pes.append(_make_pending_event_from_row(row))

        assert d.dispatch_pending(pes) == 3
        for eid in eids:
            st = session.execute(
                text("SELECT publish_state FROM events WHERE event_id=:e"),
                {"e": str(eid)},
            ).scalar_one()
            assert st == "dispatched"
        cnt = session.execute(text("SELECT count(*) FROM event_deliveries")).scalar_one()
        assert cnt == 3

    def test_selective_consumer(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher(); d.register(NoopConsumer())

        class Sel(Consumer):
            @property
            def name(self): return "sel"
            def can_handle(self, et): return et == "agent.created"
            def dispatch(self, **kw): return DispatchResult.ack()

        d.register(Sel())
        eid = _insert_pending_event(session, idempotency_key="sel1", event_type="item.created")
        session.commit()

        row = session.execute(
            text("SELECT * FROM events WHERE event_id=:e"), {"e": str(eid)}
        ).mappings().one()
        pe = _make_pending_event_from_row(row)

        assert d.dispatch_pending([pe]) == 1  # only noop
        names = {
            r["consumer_name"]
            for r in session.execute(
                text("SELECT consumer_name FROM event_deliveries WHERE event_id=:e"),
                {"e": str(eid)},
            ).mappings().all()
        }
        assert names == {"noop"}


# ============================================================================
# Error Truncation
# ============================================================================

class TestErrorTruncation:
    def test_long_error_truncated(self, db_setup, monkeypatch) -> None:
        _engine, session = db_setup
        _patch_session(monkeypatch, session)
        d = Dispatcher()

        long_err = "x" * 5000

        class LongErr(Consumer):
            @property
            def name(self): return "le"
            def can_handle(self, et): return True
            def dispatch(self, **kw): return DispatchResult.fail(long_err)

        d.register(LongErr())
        eid = _insert_pending_event(session, idempotency_key="le1")
        session.commit()

        row = session.execute(
            text("SELECT * FROM events WHERE event_id=:e"), {"e": str(eid)}
        ).mappings().one()
        pe = _make_pending_event_from_row(row)

        d.dispatch_pending([pe])

        stored = session.execute(
            text("SELECT last_error FROM event_deliveries "
                 "WHERE event_id=:e AND consumer_name='le'"),
            {"e": str(eid)},
        ).scalar_one()
        assert len(stored or "") <= 2000


# ============================================================================
# RedisConnection Unit Tests
# ============================================================================

class TestRedisConnection:
    def test_unavailable_returns_false(self, monkeypatch) -> None:
        from mneme.worker.app import RedisConnection

        def fake_probe(self):
            self._client = None; self._available = False
        monkeypatch.setattr(RedisConnection, "_probe", fake_probe)

        c = RedisConnection("redis://localhost:9999/0")
        assert c.available is False

    def test_probe_cached(self, monkeypatch) -> None:
        from mneme.worker.app import RedisConnection
        cnt = 0

        def count_probe(self):
            nonlocal cnt; cnt += 1
            self._client = None; self._available = False
        monkeypatch.setattr(RedisConnection, "_probe", count_probe)

        c = RedisConnection("redis://localhost:9999/0")
        _ = c.available; _ = c.available
        assert cnt == 1

    def test_close_clears_state(self, monkeypatch) -> None:
        from mneme.worker.app import RedisConnection

        def fake_probe(self):
            self._client = None; self._available = False
        monkeypatch.setattr(RedisConnection, "_probe", fake_probe)

        c = RedisConnection("redis://localhost:9999/0")
        _ = c.available
        c.close()
        assert c._available is None

    def test_close_no_client(self) -> None:
        from mneme.worker.app import RedisConnection
        c = RedisConnection("redis://localhost:9999/0")
        c._client = None; c._available = False
        c.close()  # should not raise


# ============================================================================
# Worker Entry-point Tests
# ============================================================================

class TestWorkerEntryPoints:
    def test_main_module_has_main(self) -> None:
        from mneme.worker.__main__ import main
        assert callable(main)

    def test_app_has_main(self) -> None:
        from mneme.worker.app import main
        assert callable(main)

    def test_app_has_run_loop(self) -> None:
        from mneme.worker.app import run_loop
        assert callable(run_loop)

    def test_default_constants(self) -> None:
        from mneme.worker.app import DEFAULT_BATCH_SIZE, DEFAULT_POLL_INTERVAL_SECONDS
        assert DEFAULT_POLL_INTERVAL_SECONDS == 5
        assert DEFAULT_BATCH_SIZE == 20


# ============================================================================
# Consumer Contract Verification
# ============================================================================

class TestConsumerContract:
    def test_incomplete_subclass_raises(self) -> None:
        class Bad(Consumer):
            pass
        with pytest.raises(TypeError):
            Bad()  # type: ignore[abstract]

    def test_dispatch_result_has_fields(self) -> None:
        r = NoopConsumer().dispatch(
            event_id=uuid4(), event_type="x", aggregate_type="x",
            aggregate_id=uuid4(), payload={}, delivery_id=uuid4(),
        )
        assert hasattr(r, "outcome") and hasattr(r, "error")

    def test_unique_name_enforced(self) -> None:
        d = Dispatcher()

        class A(Consumer):
            @property
            def name(self): return "dup"
            def can_handle(self, et): return True
            def dispatch(self, **kw): return DispatchResult.ack()

        class B(A): pass

        d.register(A())
        with pytest.raises(ValueError, match="already registered"):
            d.register(B())


# ============================================================================
# P2-01: LeaseManager Tests
# ============================================================================

import time as _time  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402
from mneme.worker.lease import LeaseManager, LEASE_KEY_PREFIX, _HEARTBEAT_LUA, _RELEASE_LUA  # noqa: E402


def _fake_redis():
    """Create a MagicMock that simulates a minimal Redis client.

    Returns a mock that supports ``set``, ``get``, ``eval``, ``ping``,
    and ``close``.
    """
    r = MagicMock()
    r.ping.return_value = True
    r.close.return_value = None
    return r


def _make_lease(
    redis_client=None,
    lease_name="test-lease",
    ttl_seconds=30,
    heartbeat_interval_seconds=10,
) -> LeaseManager:
    if redis_client is None:
        redis_client = _fake_redis()
    return LeaseManager(
        redis_client,
        lease_name,
        ttl_seconds=ttl_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


class TestLeaseManagerInit:
    """Unit tests for LeaseManager construction and properties."""

    def test_instance_id_is_uuid_string(self) -> None:
        m = _make_lease()
        assert isinstance(m.instance_id, str)
        assert len(m.instance_id) == 36  # standard UUID format

    def test_instance_ids_are_unique(self) -> None:
        a = _make_lease()
        b = _make_lease()
        assert a.instance_id != b.instance_id

    def test_lease_key_format(self) -> None:
        m = _make_lease(lease_name="dispatcher")
        assert m.lease_key == f"{LEASE_KEY_PREFIX}:dispatcher"

    def test_ttl_properties(self) -> None:
        m = _make_lease(ttl_seconds=45)
        assert m.ttl_seconds == 45
        assert m.ttl_ms == 45_000

    def test_not_held_initially(self) -> None:
        m = _make_lease()
        assert m.is_held is False

    def test_custom_lease_name_in_key(self) -> None:
        m = _make_lease(lease_name="sweeper")
        assert m.lease_key == f"{LEASE_KEY_PREFIX}:sweeper"

    def test_seconds_since_last_heartbeat_initial(self) -> None:
        m = _make_lease()
        # Initially _last_heartbeat_at is 0.0, so elapsed = monotonic time
        assert m.seconds_since_last_heartbeat > 0

    def test_heartbeat_is_due(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        m = _make_lease(redis_client=r, heartbeat_interval_seconds=5)
        m.acquire()
        # Immediately after acquire, heartbeat should not be due
        assert m.heartbeat_is_due(5) is False
        assert m.heartbeat_is_due(0) is True


class TestLeaseManagerAcquire:
    """Tests for ``LeaseManager.acquire()``."""

    def test_acquire_success(self) -> None:
        r = _fake_redis()
        r.set.return_value = True  # "OK" response
        m = _make_lease(redis_client=r)

        assert m.acquire() is True
        assert m.is_held is True
        r.set.assert_called_once_with(
            m.lease_key,
            m.instance_id,
            nx=True,
            px=m.ttl_ms,
        )

    def test_acquire_fails_when_already_held(self) -> None:
        r = _fake_redis()
        r.set.return_value = None  # Redis returns nil when key exists
        m = _make_lease(redis_client=r)

        assert m.acquire() is False
        assert m.is_held is False

    def test_acquire_redis_error(self) -> None:
        from redis.exceptions import RedisError
        r = _fake_redis()
        r.set.side_effect = RedisError("connection lost")
        m = _make_lease(redis_client=r)

        assert m.acquire() is False
        assert m.is_held is False

    def test_acquire_twice_no_double_acquire(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        m = _make_lease(redis_client=r)

        assert m.acquire() is True
        # Second acquire should still succeed locally (Redis handles it),
        # but it shouldn't break anything.
        r.set.return_value = None  # already held
        assert m.acquire() is False
        # is_held remains True from first acquire
        assert m.is_held is True


class TestLeaseManagerHeartbeat:
    """Tests for ``LeaseManager.heartbeat()``."""

    def test_heartbeat_success(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        r.eval.return_value = 1  # Lua script returns 1 on success
        m = _make_lease(redis_client=r)

        m.acquire()
        assert m.heartbeat() is True
        assert m.is_held is True
        # Verify Lua script was called with correct args
        r.eval.assert_called_once()
        call_args = r.eval.call_args
        assert call_args[0][0] == _HEARTBEAT_LUA
        assert call_args[0][1] == 1  # num keys
        assert call_args[0][2] == m.lease_key
        assert call_args[0][3] == m.instance_id
        assert call_args[0][4] == str(m.ttl_ms)

    def test_heartbeat_skipped_when_not_held(self) -> None:
        r = _fake_redis()
        m = _make_lease(redis_client=r)

        assert m.heartbeat() is False
        r.eval.assert_not_called()

    def test_heartbeat_lease_stolen(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        r.eval.return_value = 0  # Lua returned 0: lease stolen/expired
        m = _make_lease(redis_client=r)

        m.acquire()
        assert m.heartbeat() is False
        assert m.is_held is False  # Should clear local flag

    def test_heartbeat_redis_error(self) -> None:
        from redis.exceptions import RedisError
        r = _fake_redis()
        r.set.return_value = True
        r.eval.side_effect = RedisError("redis timeout")
        m = _make_lease(redis_client=r)

        m.acquire()
        assert m.heartbeat() is False
        assert m.is_held is False  # Should clear local flag on error

    def test_heartbeat_multiple_cycles(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        r.eval.return_value = 1
        m = _make_lease(redis_client=r)

        m.acquire()
        for _ in range(5):
            assert m.heartbeat() is True
            assert m.is_held is True
        assert r.eval.call_count == 5


class TestLeaseManagerRelease:
    """Tests for ``LeaseManager.release()``."""

    def test_release_success(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        r.eval.return_value = 1  # DEL returned 1
        m = _make_lease(redis_client=r)

        m.acquire()
        assert m.release() is True
        assert m.is_held is False

        call_args = r.eval.call_args
        assert call_args[0][0] == _RELEASE_LUA
        assert call_args[0][1] == 1
        assert call_args[0][2] == m.lease_key
        assert call_args[0][3] == m.instance_id

    def test_release_skipped_when_not_held(self) -> None:
        r = _fake_redis()
        m = _make_lease(redis_client=r)

        assert m.release() is False
        r.eval.assert_not_called()

    def test_release_lease_not_owned(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        r.eval.return_value = 0  # DEL returned 0: key not found or not ours
        m = _make_lease(redis_client=r)

        m.acquire()
        assert m.release() is False
        assert m.is_held is False

    def test_release_redis_error(self) -> None:
        from redis.exceptions import RedisError
        r = _fake_redis()
        r.set.return_value = True
        r.eval.side_effect = RedisError("redis down")
        m = _make_lease(redis_client=r)

        m.acquire()
        assert m.release() is False
        assert m.is_held is False  # cleared on error

    def test_release_idempotent(self) -> None:
        r = _fake_redis()
        r.set.return_value = True
        r.eval.return_value = 1
        m = _make_lease(redis_client=r)

        m.acquire()
        assert m.release() is True
        # Second release: is_held is already False, should no-op
        r.eval.reset_mock()
        assert m.release() is False
        r.eval.assert_not_called()


class TestLeaseManagerCheckHeld:
    """Tests for ``LeaseManager.check_held()``."""

    def test_check_held_true(self) -> None:
        r = _fake_redis()
        r.get.return_value = None
        m = _make_lease(redis_client=r)

        # Set up: mock GET returns our instance id
        r.get.return_value = m.instance_id.encode("utf-8")
        assert m.check_held() is True

        r.get.assert_called_once_with(m.lease_key)

    def test_check_held_false_wrong_value(self) -> None:
        r = _fake_redis()
        m = _make_lease(redis_client=r)
        r.get.return_value = b"some-other-instance"

        assert m.check_held() is False

    def test_check_held_false_key_missing(self) -> None:
        r = _fake_redis()
        m = _make_lease(redis_client=r)
        r.get.return_value = None

        assert m.check_held() is False

    def test_check_held_redis_error(self) -> None:
        from redis.exceptions import RedisError
        r = _fake_redis()
        m = _make_lease(redis_client=r)
        r.get.side_effect = RedisError("redis timeout")

        assert m.check_held() is False


class TestLeaseManagerConcurrency:
    """Simulate concurrent lease acquisition between two workers."""

    @staticmethod
    def _make_shared_store():
        """Return a dict and two mock Redis clients that share it.

        This simulates a real Redis key-value store so we can test
        leader election semantics without a real Redis server.
        """
        store: dict[str, bytes] = {}
        waiter_a: list[bool] = []
        waiter_b: list[bool] = []

        def _build_client(waiter: list[bool]):
            r = MagicMock()

            def _set(key, value, nx=False, px=None):
                if nx and key in store:
                    return None  # already exists
                store[key] = value.encode("utf-8") if isinstance(value, str) else value
                if px is not None:
                    waiter.append(True)  # record TTL was set
                return True

            def _get(key):
                return store.get(key)

            def _eval(script, num_keys, *args):
                if "PEXPIRE" in script:
                    # Heartbeat Lua
                    key = args[0]
                    instance_id = args[1]
                    current = store.get(key)
                    if current is not None and current.decode("utf-8") == instance_id:
                        return 1
                    return 0
                elif "DEL" in script:
                    # Release Lua
                    key = args[0]
                    instance_id = args[1]
                    current = store.get(key)
                    if current is not None and current.decode("utf-8") == instance_id:
                        del store[key]
                        return 1
                    return 0
                return 0

            r.set.side_effect = _set
            r.get.side_effect = _get
            r.eval.side_effect = _eval
            return r

        a = _build_client(waiter_a)
        b = _build_client(waiter_b)
        return store, a, b

    def test_first_worker_acquires_second_fails(self) -> None:
        store, ra, rb = self._make_shared_store()
        worker_a = _make_lease(ra, lease_name="dispatch")
        worker_b = _make_lease(rb, lease_name="dispatch")

        assert worker_a.acquire() is True
        assert worker_a.is_held is True

        assert worker_b.acquire() is False
        assert worker_b.is_held is False

        # Verify the store contains worker_a's instance id
        assert worker_a.lease_key in store
        assert store[worker_a.lease_key].decode("utf-8") == worker_a.instance_id

    def test_heartbeat_maintains_lease(self) -> None:
        store, ra, rb = self._make_shared_store()
        worker_a = _make_lease(ra, lease_name="dispatch")
        worker_b = _make_lease(rb, lease_name="dispatch")

        worker_a.acquire()
        # Heartbeat should succeed
        assert worker_a.heartbeat() is True
        assert worker_a.is_held is True

        # Lease still belongs to worker_a
        assert store[worker_a.lease_key].decode("utf-8") == worker_a.instance_id

    def test_release_then_second_acquires(self) -> None:
        store, ra, rb = self._make_shared_store()
        worker_a = _make_lease(ra, lease_name="dispatch")
        worker_b = _make_lease(rb, lease_name="dispatch")

        worker_a.acquire()
        assert worker_a.release() is True
        assert worker_a.is_held is False
        assert worker_a.lease_key not in store

        # Now worker_b can acquire
        assert worker_b.acquire() is True
        assert worker_b.is_held is True
        assert store[worker_b.lease_key].decode("utf-8") == worker_b.instance_id

    def test_second_worker_cannot_steal_via_heartbeat(self) -> None:
        store, ra, rb = self._make_shared_store()
        worker_a = _make_lease(ra, lease_name="dispatch")
        worker_b = _make_lease(rb, lease_name="dispatch")

        worker_a.acquire()
        # worker_b tries to heartbeat on a lease it doesn't own
        worker_b._held = True  # simulate it "thinks" it holds it
        assert worker_b.heartbeat() is False
        assert worker_b.is_held is False

        # worker_a should still hold the lease
        assert worker_a.heartbeat() is True
        assert worker_a.is_held is True

    def test_simulate_crash_recovery(self) -> None:
        """Worker A crashes (lease expires). Worker B can acquire."""
        store, ra, rb = self._make_shared_store()
        worker_a = _make_lease(ra, lease_name="dispatch", ttl_seconds=1)
        worker_b = _make_lease(rb, lease_name="dispatch", ttl_seconds=30)

        worker_a.acquire()
        # Simulate crash: delete the key from store without calling release
        worker_a._held = False  # don't call release, just clear local state
        del store[worker_a.lease_key]

        # Worker B should now be able to acquire
        assert worker_b.acquire() is True
        assert worker_b.is_held is True


class TestLeaseManagerLifecycle:
    """End-to-end lifecycle: acquire → heartbeat → release."""

    def test_full_lifecycle(self) -> None:
        store = {}
        r = MagicMock()

        def _set(key, value, nx=False, px=None):
            if nx and key in store:
                return None
            store[key] = value.encode("utf-8") if isinstance(value, str) else value
            return True

        def _get(key):
            return store.get(key)

        def _eval(script, num_keys, *args):
            if "PEXPIRE" in script:
                key = args[0]
                instance_id = args[1]
                current = store.get(key)
                if current is not None and current.decode("utf-8") == instance_id:
                    return 1
                return 0
            elif "DEL" in script:
                key = args[0]
                instance_id = args[1]
                current = store.get(key)
                if current is not None and current.decode("utf-8") == instance_id:
                    del store[key]
                    return 1
                return 0
            return 0

        r.set.side_effect = _set
        r.get.side_effect = _get
        r.eval.side_effect = _eval

        m = _make_lease(r, lease_name="lifecycle", ttl_seconds=10)

        # 1. Acquire
        assert m.acquire() is True
        assert m.is_held is True
        assert m.check_held() is True

        # 2. Heartbeat x3
        for _ in range(3):
            assert m.heartbeat() is True

        # 3. Release
        assert m.release() is True
        assert m.is_held is False
        assert m.lease_key not in store
        assert m.check_held() is False
