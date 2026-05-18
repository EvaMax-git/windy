"""Pytest configuration for Mneme tests.

Sets DATABASE_URL and REDIS_URL before any mneme imports.
Tests that use ``SessionLocal`` from ``mneme.db.base`` will connect to
the configured database (PostgreSQL or SQLite).

Key design for SQLite tests
----------------------------
SQLite :memory: databases are **per-connection**.  The default SQLAlchemy
connection pool (QueuePool, up to 5 connections) would give every
``SessionLocal()`` call a *different* ``:memory:`` database — so tables
created during ``pytest_configure`` would be invisible to test sessions.

We solve this by:

1. Replacing the engine with ``StaticPool`` (single, shared connection).
2. Reconfiguring ``SessionLocal`` via ``.configure(bind=new_engine)`` so that
   **all** modules that already imported ``SessionLocal`` (DAL layers, API
   routes, worker code) automatically use the StaticPool-backed engine.
3. Providing a ``db_session`` fixture that yields an independent session and
   guarantees rollback + close, preventing the SQLite error *"cannot commit
   transaction - SQL statements in progress"*.

Testcontainers PostgreSQL support
----------------------------------
When ``--postgres`` is passed on the command line (or ``USE_TESTCONTAINERS=1``
is in the environment), tests run against a real PostgreSQL container
managed by ``testcontainers``.  The container is started once per session,
Alembic migrations are run, and the container is torn down after the session.

Usage::

    pytest --postgres                          # use testcontainers PostgreSQL
    USE_TESTCONTAINERS=1 pytest                # same via env var
    pytest                                     # default SQLite :memory:
"""

from __future__ import annotations

import os
import logging
import sqlite3
import uuid as _uuid
from typing import Generator
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text as _text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

# ── Session-scoped flag: True when testcontainers PostgreSQL is active ─────────
_use_testcontainers: bool = False


def pytest_addoption(parser):
    """Register custom CLI options for test database selection."""
    parser.addoption(
        "--postgres",
        action="store_true",
        default=False,
        help="Use testcontainers PostgreSQL instead of SQLite :memory:",
    )


def _register_sqlite_adapters():
    """Register adapters for Python types that sqlite3 doesn't natively support.

    Without these, passing UUID or dict objects as query parameters
    to SQLite will raise ``sqlite3.ProgrammingError``.
    """
    import json

    # Adapter: Python UUID → dashed string
    sqlite3.register_adapter(UUID, lambda u: str(u))
    # Converter: string → Python UUID
    sqlite3.register_converter("UUID", lambda b: UUID(b.decode("utf-8")) if b else None)

    # Adapter: Python dict → JSON string
    sqlite3.register_adapter(dict, lambda d: json.dumps(d))
    # Adapter: Python list → JSON string (safety)
    sqlite3.register_adapter(list, lambda l: json.dumps(l))


def _register_psycopg2_adapters():
    """Register adapters so psycopg2 can bind Python dict/list to JSONB columns.

    psycopg2 does not natively adapt Python ``dict`` or ``list`` to
    PostgreSQL ``jsonb``.  Without these adapters, raw-SQL INSERT/UPDATE
    statements that pass a Python dict as a bind parameter will raise
    ``ProgrammingError: can't adapt type 'dict'``.
    """
    try:
        import psycopg2.extras
        import psycopg2.extensions

        psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)
        psycopg2.extensions.register_adapter(list, psycopg2.extras.Json)
    except ImportError:
        pass  # psycopg2 not installed — SQLite-only environment


def pytest_configure(config):
    """Set database connection URLs before test collection."""
    global _use_testcontainers

    # Register custom markers
    config.addinivalue_line("markers", "db: test requires a database connection")

    # Register SQLite UUID adapter so Python UUID objects can be bound
    _register_sqlite_adapters()

    # ── Determine if testcontainers PostgreSQL is requested ──────────────────
    use_pg = (
        config.getoption("--postgres", default=False)
        or os.getenv("USE_TESTCONTAINERS", "").strip() in ("1", "true", "yes")
    )
    _use_testcontainers = use_pg

    # ── Determine DATABASE_URL ──────────────────────────────────────────────
    # Priority:
    #   1. Explicit DATABASE_URL env var
    #   2. Testcontainers PostgreSQL (--postgres flag) — deferred to fixture
    #   3. MNEME_TEST_DB_HOST / MNEME_TEST_DB_* env vars
    #   4. Default: SQLite :memory:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        db_host = os.getenv("MNEME_TEST_DB_HOST", "").strip()
        if use_pg:
            # Use SQLite as a safe default during collection; the testcontainers
            # fixture will swap to PostgreSQL before any tests execute.
            database_url = "sqlite:///:memory:"
        elif db_host:
            # User explicitly requested a remote PostgreSQL
            db_port = os.getenv("MNEME_TEST_DB_PORT", "5432")
            db_user = os.getenv("MNEME_TEST_DB_USER", "mneme")
            db_password = os.getenv("MNEME_TEST_DB_PASSWORD", "")
            db_name = os.getenv("MNEME_TEST_DB_NAME", "mneme")
            database_url = (
                f"postgresql+psycopg2://{db_user}:{db_password}"
                f"@{db_host}:{db_port}/{db_name}"
            )
        else:
            # Default to SQLite in-memory for local / offline testing
            database_url = "sqlite:///:memory:"

    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

    # Register psycopg2 adapters when running against PostgreSQL
    if database_url.startswith("postgresql"):
        _register_psycopg2_adapters()

    # Clear cached settings so the engine uses the correct DATABASE_URL
    try:
        from mneme.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    # ── Schema creation ─────────────────────────────────────────────────────
    # For testcontainers PostgreSQL, SQLite schema creation is deferred —
    # the session fixture will set up the real PostgreSQL via Alembic.
    if not use_pg:
        _ensure_test_schema()

    # Seed the integration test user so FK constraints on created_by_user_id
    # are satisfied (affects assets, knowledge_documents, pipeline_defs, etc.)
    if database_url.startswith("postgresql") and not use_pg:
        _seed_test_user()


# ═══════════════════════════════════════════════════════════════════════════════
# Testcontainers PostgreSQL support
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session", autouse=True)
def postgres_container(request) -> Generator[str | None, None, None]:
    """Start a PostgreSQL container via testcontainers for the test session.

    This fixture is only active when ``--postgres`` is passed or
    ``USE_TESTCONTAINERS=1`` is set.  It:

    1. Starts a ``pgvector/pgvector:pg16`` container
    2. Sets ``DATABASE_URL`` env var to point to the container
    3. Runs Alembic migrations to create the full schema
    4. Seeds test users
    5. Reconfigures the engine + SessionLocal to use the container

    Yields the database URL string (or None when skipped).
    """
    if not _use_testcontainers:
        yield None
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.fail(
            "testcontainers is required for --postgres mode. "
            "Install with: pip install testcontainers"
        )

    logger.info("Starting testcontainers PostgreSQL …")
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="mneme",
        password="mneme_test_pw",
        dbname="mneme_test",
    )
    container.start()

    # Build the connection URL
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    db_url = (
        f"postgresql+psycopg2://mneme:mneme_test_pw"
        f"@{host}:{port}/mneme_test"
    )
    os.environ["DATABASE_URL"] = db_url
    logger.info("Testcontainers PostgreSQL running at %s:%s", host, port)

    # Clear cached settings and reconfigure the engine
    try:
        from mneme.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    _setup_postgres_schema(db_url)
    _seed_test_user()

    # Reconfigure SessionLocal to use the testcontainers PG
    _rebind_to_postgres(db_url)

    yield db_url

    # Tear down
    logger.info("Stopping testcontainers PostgreSQL …")
    container.stop()


def _setup_postgres_schema(db_url: str) -> None:
    """Run Alembic migrations on the testcontainers PostgreSQL database."""
    from sqlalchemy import create_engine as _sa_create_engine, text as _sa_text

    # Create extensions first (required by the baseline migration)
    _init_engine = _sa_create_engine(db_url)
    try:
        with _init_engine.connect() as conn:
            conn.execute(_sa_text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            conn.execute(_sa_text("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\""))
            conn.execute(_sa_text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        logger.info("PostgreSQL extensions created")
    except Exception:
        logger.exception("Failed to create extensions")
        raise
    finally:
        _init_engine.dispose()

    # Ensure DATABASE_URL is set for Alembic env.py
    os.environ["DATABASE_URL"] = db_url
    try:
        from mneme.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    # Run migrations
    try:
        import alembic.config
        from pathlib import Path

        alembic_ini = Path(__file__).parent.parent / "mneme" / "db" / "alembic" / "alembic.ini"
        if alembic_ini.is_file():
            argv = [
                "-c", str(alembic_ini),
                "upgrade", "heads",
            ]
            alembic.config.main(argv=argv)
            logger.info("Alembic migrations applied to testcontainers PostgreSQL")
        else:
            logger.warning("alembic.ini not found at %s; skipping migrations", alembic_ini)
    except Exception:
        logger.exception("Alembic migration failed; tests may fail")
        raise


def _rebind_to_postgres(db_url: str) -> None:
    """Rebind the global engine and SessionLocal to the PostgreSQL container.

    Disposes the old SQLite engine first, then creates a new PostgreSQL
    engine and reconfigures SessionLocal.  All modules that reference
    ``mneme.db.base.engine`` or ``mneme.db.base.SessionLocal`` are
    automatically updated because ``sessionmaker.configure()`` mutates the
    shared instance in-place.
    """
    import mneme.db.base as _base

    # Dispose the old SQLite engine (or any prior engine)
    old_engine = _base.engine
    try:
        old_engine.dispose()
    except Exception:
        pass

    # Create the new PostgreSQL engine
    new_engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

    # Rebind SessionLocal — this updates the shared sessionmaker instance
    _base.SessionLocal.configure(bind=new_engine)

    # Replace the module-level engine reference
    _base.engine = new_engine

    # Register psycopg2 adapters now that we're using PostgreSQL
    _register_psycopg2_adapters()

    logger.info("Engine + SessionLocal rebound to testcontainers PostgreSQL")


# ═══════════════════════════════════════════════════════════════════════════════
# Engine / session reconfiguration for SQLite
# ═══════════════════════════════════════════════════════════════════════════════

def _replace_sqlite_engine_with_static_pool():
    """Replace the mneme.db.base engine with a StaticPool-backed engine.

    SQLite :memory: databases are **connection-scoped**.  The default
    SQLAlchemy pool (QueuePool, max 5 connections) would create a separate
    in-memory database for each connection, so tables created during
    ``pytest_configure`` would only exist in *one* connection.

    StaticPool guarantees that all sessions — whether created by DAL
    functions or test helpers — share the exact same connection (and thus
    the same :memory: database).

    We use ``sessionmaker.configure(bind=...)`` to rebind the **existing**
    sessionmaker object rather than creating a new one.  This is essential
    because every module that does ``from mneme.db.base import SessionLocal``
    holds a reference to the same ``sessionmaker`` instance — calling
    ``.configure()`` updates that shared instance in-place, affecting ALL
    import sites.
    """
    import mneme.db.base as _base

    backend = _base.engine.url.get_backend_name()
    if backend != "sqlite":
        return False

    # Build a StaticPool engine pointing at the same URL
    new_engine = create_engine(
        _base.engine.url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    # Register SQLite compatibility functions on the new engine
    _register_sqlite_compat_functions(new_engine)

    # Reconfigure the EXISTING sessionmaker so all importers are affected
    _base.SessionLocal.configure(bind=new_engine)

    # Replace the module-level engine reference
    old_engine = _base.engine
    _base.engine = new_engine

    # Dispose the old engine (closes its pool connections)
    old_engine.dispose()

    logger.info("Replaced SQLite engine with StaticPool (single-connection)")
    return True


def _register_sqlite_compat_functions(engine):
    """Register ``now()`` and ``gen_random_uuid()`` SQL functions on *engine*.

    Many DAL queries use PostgreSQL-isms like ``CURRENT_TIMESTAMP`` or
    ``gen_random_uuid()``.  These functions make them work on SQLite.
    """
    import datetime as _dt_mod
    from uuid import uuid4 as _uuid4

    from sqlalchemy import event as _event

    @_event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _conn_record):
        # Register only if not already registered (idempotent)
        try:
            dbapi_conn.create_function(
                "now", 0,
                lambda: _dt_mod.datetime.now(_dt_mod.timezone.utc).isoformat(),
            )
        except Exception:
            pass  # function already exists

        try:
            dbapi_conn.create_function(
                "gen_random_uuid", 0,
                lambda: _uuid4().hex,  # Use .hex to match PG_UUID bindparam format
            )
        except Exception:
            pass  # function already exists


def _seed_test_user():
    """Insert the integration test user into PostgreSQL (idempotent).

    Many DAL tables reference ``users.user_id`` via ``created_by_user_id``.
    PostgreSQL enforces this FK; if no user row matches, inserts fail.
    This function inserts a well-known user (TEST_USER_ID) that all test
    helper contexts can reference.
    """
    try:
        from mneme.db.base import engine
        from sqlalchemy import text as _text

        with engine.connect() as conn:
            # Well-known test user for asset/knowledge/pipeline FK checks
            conn.execute(_text("""
                INSERT INTO users (user_id, username, email, display_name, role_code,
                                   status, password_hash, mfa_mode)
                VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa)
                ON CONFLICT (user_id) DO NOTHING
            """), {
                "uid": TEST_USER_ID,
                "uname": "test_integration_user",
                "email": "test@integration.local",
                "dname": "Integration Test User",
                "role": "owner",
                "status": "active",
                "phash": "$test$integration$hash",
                "mfa": "none",
            })

            # "owner" user referenced by review tests (_get_owner_id)
            conn.execute(_text("""
                INSERT INTO users (user_id, username, email, display_name, role_code,
                                   status, password_hash, mfa_mode)
                VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa)
                ON CONFLICT (username) DO NOTHING
            """), {
                "uid": _uuid.uuid4(),
                "uname": "owner",
                "email": "owner@integration.local",
                "dname": "Owner Test User",
                "role": "owner",
                "status": "active",
                "phash": "$test$owner$hash",
                "mfa": "none",
            })
            conn.commit()
            logger.info("Seeded test users for FK constraints")

            # Seed well-known test projects referenced by review-router tests
            conn.execute(_text("""
                INSERT INTO projects (project_id, project_code, name, status, sensitivity_default)
                VALUES (:pid, :code, :name, 'active', 'normal')
                ON CONFLICT (project_code) DO NOTHING
            """), {
                "pid": UUID("00000000-0000-0000-0000-000000000100"),
                "code": "TEST-PROJ-A",
                "name": "Test Project A (Review Router)",
            })
            conn.commit()
            logger.info("Seeded test project for review-router tests")
    except Exception:
        logger.warning("Failed to seed test user – FK tests may fail", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Schema creation
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_test_schema():
    """Create tables needed by tests if they do not exist.

    For SQLite databases (e.g. ``sqlite:///test.db`` or ``sqlite:///:memory:``)
    the schema must be created at test startup because Alembic migrations use
    PostgreSQL-specific DDL (gen_random_uuid, jsonb, plpgsql, etc.).

    For PostgreSQL the schema is assumed to already exist (created by the
    Docker entrypoint or a prior ``alembic upgrade head``).

    **Important**: for SQLite we call ``_replace_sqlite_engine_with_static_pool``
    FIRST, then create the schema on the StaticPool-backed engine.  This
    ensures the schema lives in the single shared connection that every
    subsequent ``SessionLocal()`` call will use.
    """
    try:
        from mneme.db.base import engine as _initial_engine

        backend = _initial_engine.url.get_backend_name()
        if backend != "sqlite":
            return  # PostgreSQL — assume schema exists

        # Replace engine with StaticPool version BEFORE creating schema
        is_static = _replace_sqlite_engine_with_static_pool()
        if not is_static:
            return

        # Now get the NEW engine and create schema on it
        from mneme.db.base import engine

        with engine.connect() as conn:
            _create_sqlite_schema(conn)
            conn.commit()
            logger.info("SQLite test schema ensured on StaticPool engine")

    except Exception:
        # If we can't create tables, tests that need DB will fail individually
        # rather than crashing the entire collection phase.
        logger.warning("Failed to initialize test schema", exc_info=True)


def _create_sqlite_schema(conn):
    """Create all tables matching 0001_baseline_45_tables.py for SQLite."""
    from sqlalchemy import text as _text

    # ── projects ────────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY NOT NULL,
            project_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            sensitivity_default TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            archived_at TEXT
        )
    """))

    # ── users ───────────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY NOT NULL,
            username TEXT NOT NULL UNIQUE,
            email TEXT UNIQUE,
            display_name TEXT NOT NULL,
            role_code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_bootstrap',
            password_hash TEXT NOT NULL DEFAULT '',
            mfa_mode TEXT NOT NULL DEFAULT 'none',
            locale TEXT NOT NULL DEFAULT 'zh-CN',
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            last_login_at TEXT,
            disabled_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── user_sessions (P1-03) ─────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            session_id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            session_token_hash TEXT NOT NULL UNIQUE,
            session_token_prefix TEXT NOT NULL,
            auth_method TEXT NOT NULL DEFAULT 'password',
            device_label TEXT,
            device_fingerprint TEXT,
            ip_hash TEXT,
            user_agent TEXT,
            step_up_verified_at TEXT,
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            revoke_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── review_items ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS review_items (
            review_item_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            review_type TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_version INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 100
                CHECK (priority BETWEEN 0 AND 1000),
            requester_actor_type TEXT NOT NULL DEFAULT 'system',
            requester_actor_id TEXT,
            reviewer_id TEXT,
            decision TEXT,
            reason TEXT,
            decision_payload TEXT NOT NULL DEFAULT '{}',
            due_at TEXT,
            decided_at TEXT,
            expires_at TEXT,
            correlation_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── events (outbox) ─────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY NOT NULL,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL DEFAULT 1,
            correlation_id TEXT,
            causation_id TEXT,
            idempotency_key TEXT UNIQUE,
            producer TEXT NOT NULL DEFAULT 'mneme-api',
            payload_json TEXT NOT NULL DEFAULT '{}',
            visibility TEXT NOT NULL DEFAULT 'internal',
            publish_state TEXT NOT NULL DEFAULT 'pending',
            occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
            committed_at TEXT NOT NULL DEFAULT (datetime('now')),
            published_at TEXT,
            last_error TEXT
        )
    """))

    # ── event_deliveries ────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS event_deliveries (
            delivery_id TEXT PRIMARY KEY NOT NULL,
            event_id TEXT NOT NULL,
            consumer_name TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            dispatch_attempts INTEGER NOT NULL DEFAULT 0,
            last_dispatched_at TEXT,
            acknowledged_at TEXT,
            failed_at TEXT,
            last_error TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (event_id, consumer_name)
        )
    """))

    # ── audit_events ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS audit_events (
            audit_id TEXT PRIMARY KEY NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            auth_context_type TEXT,
            auth_context_id TEXT,
            action TEXT NOT NULL,
            object_type TEXT,
            object_id TEXT,
            project_id TEXT,
            result TEXT NOT NULL DEFAULT 'success',
            reason_code TEXT,
            sensitivity_level TEXT NOT NULL DEFAULT 'normal',
            correlation_id TEXT,
            request_id TEXT,
            review_item_id TEXT,
            diff_summary TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
    """))

    # ── dead_letters ─────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS dead_letters (
            dead_letter_id TEXT PRIMARY KEY NOT NULL,
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
            first_failed_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_failed_at TEXT NOT NULL DEFAULT (datetime('now')),
            replayed_at TEXT,
            resolved_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── providers ────────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS providers (
            provider_id TEXT PRIMARY KEY NOT NULL,
            provider_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            provider_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            endpoint_base TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── provider_models ──────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS provider_models (
            provider_model_id TEXT PRIMARY KEY NOT NULL,
            provider_id TEXT NOT NULL,
            model_code TEXT NOT NULL,
            external_model_id TEXT NOT NULL,
            model_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            display_name TEXT,
            version_label TEXT,
            context_window_tokens INTEGER,
            max_input_tokens INTEGER,
            max_output_tokens INTEGER,
            input_price_per_1k REAL,
            output_price_per_1k REAL,
            currency_code TEXT NOT NULL DEFAULT 'USD',
            supports_streaming INTEGER NOT NULL DEFAULT 0,
            supports_json_mode INTEGER NOT NULL DEFAULT 0,
            supports_tools INTEGER NOT NULL DEFAULT 0,
            supports_vision INTEGER NOT NULL DEFAULT 0,
            sensitivity_ceiling TEXT NOT NULL DEFAULT 'private',
            config_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            deprecated_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (provider_id, model_code),
            UNIQUE (provider_id, external_model_id)
        )
    """))

    # ── capabilities ─────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS capabilities (
            capability_id TEXT PRIMARY KEY NOT NULL,
            capability_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            risk_level TEXT NOT NULL DEFAULT 'normal',
            default_budget_mode TEXT NOT NULL DEFAULT 'metered',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── credential_vault ────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS credential_vault (
            credential_id TEXT PRIMARY KEY NOT NULL,
            provider_id TEXT NOT NULL,
            credential_name TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            ciphertext BLOB,
            key_wrap BLOB,
            key_version TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            scope_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            rotated_at TEXT,
            last_used_at TEXT,
            revoked_at TEXT,
            created_by_user_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (provider_id, credential_name)
        )
    """))

    # ── capability_bindings ──────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS capability_bindings (
            capability_binding_id TEXT PRIMARY KEY NOT NULL,
            capability_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            provider_model_id TEXT,
            credential_id TEXT,
            project_id TEXT,
            binding_scope TEXT NOT NULL DEFAULT 'global',
            status TEXT NOT NULL DEFAULT 'active',
            priority INTEGER NOT NULL DEFAULT 100,
            sensitivity_floor TEXT NOT NULL DEFAULT 'public',
            sensitivity_ceiling TEXT NOT NULL DEFAULT 'private',
            budget_mode TEXT NOT NULL DEFAULT 'metered',
            require_review INTEGER NOT NULL DEFAULT 0,
            allow_streaming INTEGER NOT NULL DEFAULT 1,
            timeout_seconds INTEGER NOT NULL DEFAULT 120,
            rate_limit_key TEXT,
            policy_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_by_user_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── vault_access_logs ────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS vault_access_logs (
            access_log_id TEXT PRIMARY KEY NOT NULL,
            credential_id TEXT,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            auth_context_type TEXT,
            auth_context_id TEXT,
            action TEXT NOT NULL,
            result TEXT NOT NULL,
            capability_id TEXT,
            provider_id TEXT,
            request_id TEXT,
            correlation_id TEXT,
            reason_code TEXT,
            target_scope TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── usage_limits ─────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS usage_limits (
            usage_limit_id TEXT PRIMARY KEY NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            capability_id TEXT,
            provider_id TEXT,
            project_id TEXT,
            limit_scope TEXT NOT NULL,
            window_unit TEXT NOT NULL,
            max_requests INTEGER,
            max_input_tokens INTEGER,
            max_output_tokens INTEGER,
            max_total_tokens INTEGER,
            max_cost REAL,
            approval_threshold_cost REAL,
            block_threshold_cost REAL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── budget_tracking ──────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS budget_tracking (
            budget_tracking_id TEXT PRIMARY KEY NOT NULL,
            request_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            capability_id TEXT,
            provider_id TEXT,
            project_id TEXT,
            reservation_state TEXT NOT NULL,
            currency_code TEXT NOT NULL DEFAULT 'USD',
            estimated_input_tokens INTEGER,
            estimated_output_tokens INTEGER,
            actual_input_tokens INTEGER,
            actual_output_tokens INTEGER,
            reserved_cost REAL NOT NULL DEFAULT 0,
            committed_cost REAL NOT NULL DEFAULT 0,
            released_cost REAL NOT NULL DEFAULT 0,
            denied_reason TEXT,
            provider_request_fingerprint TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── api_call_logs ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS api_call_logs (
            api_call_log_id TEXT PRIMARY KEY NOT NULL,
            request_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            project_id TEXT,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            auth_context_type TEXT,
            auth_context_id TEXT,
            capability_id TEXT NOT NULL,
            capability_binding_id TEXT,
            provider_id TEXT NOT NULL,
            provider_model_id TEXT,
            credential_id TEXT,
            vault_access_log_id TEXT,
            budget_tracking_id TEXT,
            review_item_id TEXT,
            event_id TEXT,
            call_type TEXT NOT NULL,
            call_state TEXT NOT NULL DEFAULT 'planned',
            external_request_id TEXT,
            provider_request_fingerprint TEXT NOT NULL,
            request_summary TEXT NOT NULL DEFAULT '{}',
            response_summary TEXT NOT NULL DEFAULT '{}',
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            estimated_cost REAL,
            actual_cost REAL,
            currency_code TEXT NOT NULL DEFAULT 'USD',
            latency_ms INTEGER,
            retry_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT,
            retention_until TEXT NOT NULL DEFAULT (datetime('now', '+180 days')),
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── object_registry (P1-09) ──────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS object_registry (
            object_id TEXT NOT NULL,
            project_id TEXT,
            object_type TEXT NOT NULL,
            object_key TEXT,
            owner_actor_type TEXT NOT NULL DEFAULT 'system',
            owner_actor_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            current_version INTEGER NOT NULL DEFAULT 1,
            sensitivity_level TEXT NOT NULL DEFAULT 'normal',
            source_type TEXT,
            source_id TEXT,
            canonical_uri TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            archived_at TEXT,
            PRIMARY KEY (object_id, object_type)
        )
    """))

    # ── object_versions (P1-09) ──────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS object_versions (
            object_version_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            object_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            version INTEGER NOT NULL,
            action TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            event_id TEXT,
            audit_id TEXT,
            source_map_id TEXT,
            previous_version INTEGER,
            checksum TEXT,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            diff_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (object_id, object_type, version)
        )
    """))

    # ── inbox_items ──────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS inbox_items (
            inbox_item_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            inbox_type TEXT NOT NULL,
            source TEXT NOT NULL,
            source_uri TEXT,
            source_ref TEXT,
            status TEXT NOT NULL DEFAULT 'received',
            asset_id TEXT,
            title TEXT,
            content_hash TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            processed_at TEXT,
            created_by_actor_type TEXT NOT NULL DEFAULT 'user',
            created_by_actor_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (inbox_type IN ('file', 'url', 'text', 'email', 'message', 'api', 'importer')),
            CHECK (status IN ('received', 'staged', 'linked', 'processed', 'rejected', 'failed', 'archived'))
        )
    """))

    # ── assets ────────────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            asset_uid TEXT NOT NULL,
            title TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            media_type TEXT,
            original_filename TEXT,
            storage_backend TEXT NOT NULL DEFAULT 'mneme_data',
            storage_ref TEXT NOT NULL,
            canonical_uri TEXT,
            content_hash TEXT NOT NULL,
            size_bytes INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            ingest_state TEXT NOT NULL DEFAULT 'pending',
            knowledge_state TEXT NOT NULL DEFAULT 'not_started',
            current_version INTEGER NOT NULL DEFAULT 1,
            sensitivity_level TEXT NOT NULL DEFAULT 'normal',
            retention_policy TEXT NOT NULL DEFAULT 'default',
            source_inbox_item_id TEXT,
            created_by_user_id TEXT,
            imported_from TEXT,
            imported_source_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            archived_at TEXT,
            UNIQUE (project_id, asset_uid),
            UNIQUE (project_id, content_hash)
        )
    """))

    # ── asset_metadata ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS asset_metadata (
            asset_metadata_id TEXT PRIMARY KEY NOT NULL,
            asset_id TEXT NOT NULL,
            metadata_key TEXT NOT NULL,
            metadata_value TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            value_type TEXT NOT NULL DEFAULT 'text',
            source TEXT NOT NULL DEFAULT 'system',
            confidence REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (asset_id, metadata_key, source)
        )
    """))

    # ── jobs ─────────────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            job_key TEXT NOT NULL UNIQUE,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 100,
            queue_name TEXT NOT NULL DEFAULT 'default',
            scheduled_at TEXT NOT NULL DEFAULT (datetime('now')),
            available_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            finished_at TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            timeout_seconds INTEGER NOT NULL DEFAULT 900,
            cause_event_id TEXT,
            aggregate_type TEXT,
            aggregate_id TEXT,
            target_version INTEGER,
            input TEXT NOT NULL DEFAULT '{}',
            output TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '{}',
            last_error TEXT,
            created_by_actor_type TEXT NOT NULL DEFAULT 'system',
            created_by_actor_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── job_logs ─────────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS job_logs (
            job_log_id TEXT PRIMARY KEY NOT NULL,
            job_id TEXT NOT NULL,
            step TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT NOT NULL,
            attempt_no INTEGER NOT NULL DEFAULT 0,
            event_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── pipeline_defs ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS pipeline_defs (
            pipeline_def_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            pipeline_code TEXT NOT NULL,
            pipeline_type TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            description TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_by_user_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, pipeline_code, version)
        )
    """))

    # ── pipeline_runs ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            pipeline_run_id TEXT PRIMARY KEY NOT NULL,
            pipeline_def_id TEXT NOT NULL,
            project_id TEXT,
            root_job_id TEXT,
            trigger_type TEXT NOT NULL,
            trigger_event_id TEXT,
            target_type TEXT,
            target_id TEXT,
            target_version INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── knowledge_documents ────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            document_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            title TEXT NOT NULL,
            canonical_uri TEXT,
            document_status TEXT NOT NULL DEFAULT 'active',
            current_version INTEGER NOT NULL DEFAULT 1,
            sensitivity_level TEXT NOT NULL DEFAULT 'normal',
            summary TEXT,
            created_by_user_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── knowledge_blocks ───────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS knowledge_blocks (
            block_id TEXT PRIMARY KEY NOT NULL,
            document_id TEXT NOT NULL,
            block_key TEXT NOT NULL,
            block_order INTEGER NOT NULL,
            current_version INTEGER NOT NULL DEFAULT 1,
            block_type TEXT NOT NULL DEFAULT 'paragraph',
            content_markdown TEXT NOT NULL,
            content_text TEXT NOT NULL,
            token_count INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (document_id, block_key),
            UNIQUE (document_id, block_order)
        )
    """))

    # ── knowledge_chunks ────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            chunk_id TEXT PRIMARY KEY NOT NULL,
            document_id TEXT NOT NULL,
            block_id TEXT,
            chunk_order INTEGER NOT NULL,
            document_version INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            token_count INTEGER,
            embedding TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (document_id, document_version, chunk_order)
        )
    """))

    # ── index_states ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS index_states (
            index_state_id TEXT PRIMARY KEY NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            ready_version INTEGER NOT NULL DEFAULT 0,
            stale_version INTEGER NOT NULL DEFAULT 0,
            fts_state TEXT NOT NULL DEFAULT 'pending',
            vector_state TEXT NOT NULL DEFAULT 'pending',
            graph_state TEXT NOT NULL DEFAULT 'pending',
            citation_state TEXT NOT NULL DEFAULT 'pending',
            last_refreshed_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (object_type, object_id)
        )
    """))

    # ── source_maps ────────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS source_maps (
            source_map_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            source_asset_id TEXT,
            source_document_id TEXT,
            source_block_id TEXT,
            target_document_id TEXT,
            target_block_id TEXT,
            target_chunk_id TEXT,
            span TEXT NOT NULL DEFAULT '{}',
            confidence REAL,
            mapping_role TEXT NOT NULL DEFAULT 'citation',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── memory_stores (0005) ───────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS memory_stores (
            store_id TEXT PRIMARY KEY NOT NULL,
            agent_id TEXT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (type IN ('memory_card', 'identity', 'skill', 'rule', 'tool'))
        )
    """))

    # ── agents (P5-03) ────────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            agent_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
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

    # ── agent_tokens (P5-03) ─────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS agent_tokens (
            token_id TEXT PRIMARY KEY NOT NULL,
            agent_id TEXT NOT NULL,
            issued_by_user_id TEXT,
            token_hash TEXT NOT NULL,
            token_prefix TEXT NOT NULL,
            token_fingerprint TEXT NOT NULL,
            project_scope TEXT NOT NULL DEFAULT '[]',
            capability_scope TEXT NOT NULL DEFAULT '[]',
            sensitivity_ceiling TEXT NOT NULL DEFAULT 'normal',
            name TEXT,
            budget_limit_daily REAL,
            rate_limit_per_min INTEGER,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            last_used_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── memories (P4-05) ─────────────────────────────────────────────────
    # Columns added by later migrations (0002, 0005, 0014, 0015, 0015_graph)
    # are included here directly so SQLite tests match the production schema.
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            canonical_key TEXT NOT NULL,
            title TEXT,
            memory_text TEXT NOT NULL,
            current_version INTEGER DEFAULT 1,
            sensitivity_level TEXT DEFAULT 'private',
            status TEXT DEFAULT 'active',
            activated_from_candidate_id TEXT,
            activated_by_review_item_id TEXT,
            activated_at TEXT,
            expired_at TEXT,
            -- 0005_memory_stores
            store_id TEXT,
            -- 0002_p6_refine_columns
            quality_score REAL,
            search_weight REAL DEFAULT 1.0,
            last_refined_at TEXT,
            -- 0014_memory_decay_score
            decay_score REAL DEFAULT 1.0,
            decay_state TEXT DEFAULT 'active',
            last_decayed_at TEXT,
            last_reinforced_at TEXT,
            -- 0015_emotion_charge
            emotion_charge TEXT DEFAULT 'neutral',
            uncertainty_score REAL DEFAULT 0.5,
            last_emotion_inferred_at TEXT,
            -- 0015_graph_node_attrs
            node_type TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, canonical_key)
        )
    """))

    # ── memory_versions (P4-06) ──────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS memory_versions (
            memory_version_id TEXT PRIMARY KEY NOT NULL,
            memory_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            action TEXT NOT NULL,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            review_item_id TEXT,
            candidate_id TEXT,
            event_id TEXT,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (memory_id, version)
        )
    """))

    # ── memory_index_entries (P4-07) ─────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS memory_index_entries (
            memory_index_entry_id TEXT PRIMARY KEY NOT NULL,
            memory_id TEXT NOT NULL,
            memory_version INTEGER NOT NULL,
            project_id TEXT,
            index_profile TEXT DEFAULT 'default',
            embedding_model_id TEXT,
            content_hash TEXT NOT NULL,
            index_text TEXT NOT NULL,
            fts_state TEXT DEFAULT 'pending',
            vector_state TEXT DEFAULT 'pending',
            ready_at TEXT,
            stale_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (memory_id, memory_version, index_profile)
        )
    """))

    # ── memory_candidates (P4-04) ────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS memory_candidates (
            candidate_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            source_type TEXT NOT NULL,
            source_id TEXT,
            submitted_by_actor_type TEXT NOT NULL,
            submitted_by_actor_id TEXT,
            title TEXT,
            candidate_text TEXT NOT NULL,
            candidate_hash TEXT NOT NULL,
            sensitivity_level TEXT DEFAULT 'private',
            candidate_status TEXT DEFAULT 'pending_review',
            confidence_score REAL,
            review_required INTEGER DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, candidate_hash)
        )
    """))

    # ── memory_relations (P4-08) ─────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS memory_relations (
            memory_relation_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            from_memory_id TEXT NOT NULL,
            from_memory_version INTEGER,
            to_memory_id TEXT NOT NULL,
            to_memory_version INTEGER,
            relation_type TEXT NOT NULL,
            relation_status TEXT DEFAULT 'active',
            created_by_review_item_id TEXT,
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (from_memory_id, to_memory_id, relation_type)
        )
    """))

    # ── context_packs (P5-04) ────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS context_packs (
            context_pack_id TEXT PRIMARY KEY NOT NULL,
            request_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            agent_id TEXT,
            project_id TEXT,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            compile_mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            knowledge_version_set TEXT NOT NULL DEFAULT '[]',
            memory_version_set TEXT NOT NULL DEFAULT '[]',
            token_budget TEXT NOT NULL DEFAULT '{}',
            exclusion_summary TEXT NOT NULL DEFAULT '{}',
            api_call_log_id TEXT,
            retention_until TEXT NOT NULL DEFAULT (datetime('now', '+180 days')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── context_pack_items (P5-04) ───────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS context_pack_items (
            context_pack_item_id TEXT PRIMARY KEY NOT NULL,
            context_pack_id TEXT NOT NULL,
            item_order INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            object_id TEXT,
            object_version INTEGER,
            source_ref TEXT NOT NULL DEFAULT '{}',
            included INTEGER NOT NULL DEFAULT 1,
            exclusion_reason TEXT,
            score REAL,
            token_count INTEGER,
            reason TEXT,
            content_digest TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (context_pack_id, item_order)
        )
    """))

    # ── memory_sources (P4-06) ──────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS memory_sources (
            memory_source_id TEXT PRIMARY KEY NOT NULL,
            memory_id TEXT NOT NULL,
            memory_version INTEGER NOT NULL,
            candidate_id TEXT,
            raw_event_id TEXT,
            asset_id TEXT,
            document_id TEXT,
            block_id TEXT,
            message_id TEXT,
            source_span TEXT NOT NULL DEFAULT '{}',
            confidence REAL,
            source_role TEXT DEFAULT 'evidence',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── conversations (P4-01) ─────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            owner_user_id TEXT,
            conversation_type TEXT NOT NULL DEFAULT 'chat',
            title TEXT,
            source_platform TEXT NOT NULL,
            sensitivity_level TEXT NOT NULL DEFAULT 'private',
            retention_days INTEGER,
            conversation_status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT,
            ended_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (conversation_type IN ('chat', 'meeting', 'email_thread', 'system_event', 'agent_run')),
            CHECK (conversation_status IN ('active', 'archived', 'deleted')),
            CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret'))
        )
    """))

    # ── event_source (P4-01) ──────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS event_source (
            event_source_id TEXT PRIMARY KEY NOT NULL,
            conversation_id TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            external_conversation_id TEXT,
            source_account_id TEXT,
            source_uri TEXT,
            participants_json TEXT NOT NULL DEFAULT '[]',
            time_range_start TEXT,
            time_range_end TEXT,
            import_run_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── messages (P4-02) ──────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY NOT NULL,
            conversation_id TEXT NOT NULL,
            event_source_id TEXT,
            parent_message_id TEXT,
            role_code TEXT NOT NULL,
            sender_label TEXT,
            content_text TEXT NOT NULL,
            content_markdown TEXT,
            content_hash TEXT NOT NULL,
            sensitivity_level TEXT NOT NULL DEFAULT 'private',
            pii_flags TEXT NOT NULL DEFAULT '[]',
            message_time TEXT NOT NULL,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (role_code IN ('user', 'assistant', 'agent', 'system', 'tool', 'other')),
            CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
            UNIQUE (event_source_id, content_hash, message_time)
        )
    """))

    # ── raw_events (P4-03) ────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS raw_events (
            raw_event_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            event_source_id TEXT,
            conversation_id TEXT,
            message_id TEXT,
            raw_event_type TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            external_event_id TEXT,
            event_time TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            text_preview TEXT,
            sensitivity_level TEXT NOT NULL DEFAULT 'private',
            pii_flags TEXT NOT NULL DEFAULT '[]',
            retention_until TEXT,
            import_run_id TEXT,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (raw_event_type IN ('message', 'tool_call', 'tool_result', 'reaction', 'attachment', 'system_event', 'import_record')),
            CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
            UNIQUE (idempotency_key)
        )
    """))

    # ── pipeline_registry (0006 + 0011) ─────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS pipeline_registry (
            id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
            name TEXT NOT NULL,
            input_formats TEXT NOT NULL DEFAULT '{}',
            processor_module TEXT NOT NULL,
            accept_chunk_types TEXT NOT NULL DEFAULT '{}',
            target_stores TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))
    conn.execute(_text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_pipeline_registry_processor_module
        ON pipeline_registry (processor_module)
    """))

    # ── processing_jobs (0008) ──────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS processing_jobs (
            id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
            asset_id TEXT NOT NULL,
            pipeline_id TEXT NOT NULL,
            target_stores TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            chunks_produced INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (status IN ('queued', 'processing', 'done', 'failed'))
        )
    """))
    conn.execute(_text("""
        CREATE INDEX IF NOT EXISTS ix_processing_jobs_status
        ON processing_jobs (status)
    """))
    conn.execute(_text("""
        CREATE INDEX IF NOT EXISTS ix_processing_jobs_asset_id
        ON processing_jobs (asset_id)
    """))

    # ── eval_tasks (0004) ───────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS eval_tasks (
            task_id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
            task_name TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'precision_recall',
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            progress REAL NOT NULL DEFAULT 0.0,
            config_json TEXT NOT NULL DEFAULT '{}',
            total_items INTEGER NOT NULL DEFAULT 0,
            processed_items INTEGER NOT NULL DEFAULT 0,
            created_by_user_id TEXT,
            started_at TEXT,
            finished_at TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (task_type IN (
                'precision_recall', 'bleu', 'rouge', 'f1',
                'accuracy', 'manual', 'custom'
            )),
            CHECK (status IN (
                'pending', 'running', 'completed', 'failed', 'cancelled'
            )),
            CHECK (progress >= 0 AND progress <= 100)
        )
    """))

    # ── eval_results (0004) ─────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS eval_results (
            result_id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
            task_id TEXT NOT NULL,
            item_index INTEGER NOT NULL DEFAULT 0,
            input_text TEXT,
            expected_output TEXT,
            actual_output TEXT,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))

    # ── graph_nodes (0003) ──────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS graph_nodes (
            node_id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
            project_id TEXT,
            node_type TEXT NOT NULL,
            node_label TEXT NOT NULL,
            node_key TEXT,
            source_type TEXT,
            source_id TEXT,
            content_hash TEXT,
            properties_json TEXT NOT NULL DEFAULT '{}',
            sensitivity_level TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (node_type IN (
                'memory', 'document', 'chunk', 'entity',
                'concept', 'topic', 'asset', 'message'
            )),
            CHECK (sensitivity_level IN (
                'public', 'normal', 'private', 'sensitive', 'secret'
            )),
            CHECK (status IN ('active', 'archived', 'deleted', 'stale')),
            UNIQUE (project_id, node_key)
        )
    """))

    # ── graph_edges (0003) ──────────────────────────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS graph_edges (
            edge_id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
            project_id TEXT,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            edge_label TEXT,
            weight REAL NOT NULL DEFAULT 1.0,
            properties_json TEXT NOT NULL DEFAULT '{}',
            relation_status TEXT NOT NULL DEFAULT 'active',
            source_type TEXT,
            source_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (edge_type IN (
                'semantic_similarity', 'provenance', 'coreference',
                'causal', 'hierarchical', 'temporal', 'custom'
            )),
            CHECK (weight >= -1.0 AND weight <= 1.0),
            CHECK (relation_status IN ('active', 'resolved', 'cancelled', 'expired')),
            CHECK (from_node_id <> to_node_id),
            UNIQUE (from_node_id, to_node_id, edge_type)
        )
    """))

    # ── sub_library_registry (0007 + 0011 + 0018) ────────────────────────────
    conn.execute(_text("""
        CREATE TABLE IF NOT EXISTS sub_library_registry (
            id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            key TEXT NOT NULL DEFAULT '',
            capability_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (type IN ('vector', 'graph', 'fulltext', 'custom'))
        )
    """))
    conn.execute(_text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_sub_library_registry_key
        ON sub_library_registry (key)
        WHERE key IS NOT NULL AND key != ''
    """))

    # ── Seed bootstrap user ────────────────────────────────────────────────
    from uuid import uuid4

    owner_id = str(uuid4())
    conn.execute(
        _text(
            "INSERT OR IGNORE INTO users "
            "(user_id, username, email, display_name, role_code, status, "
            " password_hash, mfa_mode) "
            "VALUES (:uid, 'owner', 'owner@test.local', 'Owner', "
            "'owner', 'active', 'test_hash', 'none')"
        ),
        {"uid": owner_id},
    )

@pytest.fixture(autouse=True)
def isolate_tests():
    yield

# Schema is created during pytest_configure — no module-level call needed.


TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def db():
    """每个测试独立session, commit后不自动begin, 测试结束rollback"""
    from mneme.db.base import SessionLocal
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def test_user_id(db) -> UUID:
    """Ensure a test user exists in the users table for FK references.

    Many DAL modules (assets, knowledge_documents, pipeline_defs, etc.)
    write ``created_by_user_id`` which has a NOT DEFERRABLE FK to
    ``users.user_id``.  PostgreSQL enforces this; SQLite does not.
    This fixture inserts a well-known test user so the FK checks pass.

    Autouse: every DB test gets this user automatically.
    """
    from sqlalchemy import text as _text

    # Use INSERT … ON CONFLICT DO NOTHING for idempotency
    db.execute(_text("""
        INSERT INTO users (user_id, username, email, display_name, role_code,
                           status, password_hash, mfa_mode)
        VALUES (:uid, :uname, :email, :dname, :role, :status, :phash, :mfa)
        ON CONFLICT (user_id) DO NOTHING
    """), {
        "uid": TEST_USER_ID,
        "uname": "test_integration_user",
        "email": "test@integration.local",
        "dname": "Integration Test User",
        "role": "owner",
        "status": "active",
        "phash": "$test$integration$hash",
        "mfa": "none",
    })
    db.flush()
    return TEST_USER_ID
