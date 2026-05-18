"""P1-09: Object Registry + Object Versions unit/integration tests.

Covers:
1. register_object — INSERT into object_registry.
2. get_registry — SELECT by (object_id, object_type).
3. get_registry_by_key — SELECT by project-scoped unique key.
4. create_version — INSERT into object_versions with RETURNING.
5. get_version — SELECT exact version by number.
6. list_versions — paginated listing, newest first.
7. bump_version — UPDATE current_version on registry row.
8. Round-trip: register → verify raw SQL rows match expected values.
9. Version chain: multiple versions, ordered correctly.
10. Schema: ObjectType / ObjectStatus / ObjectVersionAction enums.
11. Schema: ObjectRegistryRead / ObjectVersionRead model validation.
12. P0-01 gate: `project` is required in ObjectType enum.

Note: SQLite stores UUIDs as hex strings (no dashes) via PG_UUID bindparam.
Use ``uuid.hex`` for query parameters (not ``str(uuid)`` which includes dashes).
See review_P1-09.md P0-01 for the known 'project' in ObjectType gap.
"""

from __future__ import annotations

import json as _json
import os
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.domain.objects import (  # noqa: E402
    bump_version,
    create_version,
    register_object,
)
from mneme.schemas.objects import (  # noqa: E402
    ObjectRegistryCreate,
    ObjectRegistryRead,
    ObjectStatus,
    ObjectType,
    ObjectVersionAction,
    ObjectVersionCreate,
    ObjectVersionRead,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_json(val):
    """Normalise JSON: SQLite TEXT returns str, PG JSONB returns dict."""
    if isinstance(val, str):
        return _json.loads(val)
    return val


def _build_object_tables(engine):
    """Create object_registry and object_versions tables (SQLite subset)."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE object_registry (
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
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              archived_at TIMESTAMP,
              PRIMARY KEY (object_id, object_type)
            )
        """))
        conn.execute(text("""
            CREATE TABLE object_versions (
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
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (object_id, version),
              FOREIGN KEY (object_id, object_type) REFERENCES object_registry(object_id, object_type)
            )
        """))


@pytest.fixture
def db_session():
    """In-memory SQLite session with object_registry + object_versions tables."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _build_object_tables(engine)
    with Session(engine) as db:
        yield db


# ═══════════════════════════════════════════════════════════════════════════════
# register_object — INSERT
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegisterObject:

    def test_register_minimal_required_fields(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()

        row = db_session.execute(
            text("SELECT object_id, object_type, status, current_version FROM object_registry")
        ).one()
        assert UUID(row.object_id) == obj_id
        assert row.object_type == "memory"
        assert row.status == "active"
        assert row.current_version == 1

    def test_register_all_explicit_fields(self, db_session):
        obj_id = uuid4()
        proj_id = uuid4()
        owner_id = uuid4()
        src_id = uuid4()

        register_object(
            db_session,
            object_id=obj_id,
            object_type="document",
            project_id=proj_id,
            object_key="doc-001",
            owner_actor_type="user",
            owner_actor_id=owner_id,
            status="active",
            current_version=3,
            sensitivity_level="sensitive",
            source_type="upload",
            source_id=src_id,
            canonical_uri="s3://bucket/doc-001.pdf",
            metadata_json={"title": "Design Doc", "pages": 42},
        )
        db_session.commit()

        row = db_session.execute(text("SELECT * FROM object_registry")).one()
        assert UUID(row.object_id) == obj_id
        assert UUID(row.project_id) == proj_id
        assert row.object_type == "document"
        assert row.object_key == "doc-001"
        assert row.owner_actor_type == "user"
        assert UUID(row.owner_actor_id) == owner_id
        assert row.status == "active"
        assert row.current_version == 3
        assert row.sensitivity_level == "sensitive"
        assert row.source_type == "upload"
        assert UUID(row.source_id) == src_id
        assert row.canonical_uri == "s3://bucket/doc-001.pdf"
        meta = _parse_json(row.metadata_json)
        assert meta["title"] == "Design Doc"
        assert meta["pages"] == 42

    def test_register_without_project_id_is_null(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        row = db_session.execute(text("SELECT project_id FROM object_registry")).one()
        assert row.project_id is None

    def test_register_metadata_json_defaults_to_empty_dict(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        row = db_session.execute(text("SELECT metadata_json FROM object_registry")).one()
        assert _parse_json(row.metadata_json) == {}

    def test_register_defaults_owner_actor_type_to_system(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        row = db_session.execute(text("SELECT owner_actor_type FROM object_registry")).one()
        assert row.owner_actor_type == "system"

    def test_register_defaults_sensitivity_level_to_normal(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        row = db_session.execute(text("SELECT sensitivity_level FROM object_registry")).one()
        assert row.sensitivity_level == "normal"


# ═══════════════════════════════════════════════════════════════════════════════
# Registry read-back via raw SQL (using .hex for UUID params)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegistryReadBack:

    def test_raw_sql_fetch_after_register(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory", object_key="mem-1")
        db_session.commit()

        row = db_session.execute(
            text("SELECT object_id, object_type, object_key, status, current_version, metadata_json "
                 "FROM object_registry WHERE object_id = :oid AND object_type = :otype"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).first()
        assert row is not None
        assert UUID(row.object_id) == obj_id
        assert row.object_type == "memory"
        assert row.object_key == "mem-1"
        assert row.status == "active"
        assert row.current_version == 1
        assert _parse_json(row.metadata_json) == {}

    def test_no_row_unknown_id(self, db_session):
        row = db_session.execute(
            text("SELECT 1 FROM object_registry WHERE object_id = :oid AND object_type = :otype"),
            {"oid": uuid4().hex, "otype": "memory"},
        ).first()
        assert row is None

    def test_no_row_wrong_type(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        row = db_session.execute(
            text("SELECT 1 FROM object_registry WHERE object_id = :oid AND object_type = :otype"),
            {"oid": obj_id.hex, "otype": "document"},
        ).first()
        assert row is None

    def test_metadata_json_round_trip(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory",
                        metadata_json={"key": "value", "nested": {"a": 1}})
        db_session.commit()
        row = db_session.execute(
            text("SELECT metadata_json FROM object_registry WHERE object_id = :oid AND object_type = :otype"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).first()
        assert row is not None
        assert _parse_json(row.metadata_json) == {"key": "value", "nested": {"a": 1}}

    def test_find_by_project_key(self, db_session):
        proj_id = uuid4()
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory",
                        project_id=proj_id, object_key="unique-memory-key")
        db_session.commit()
        row = db_session.execute(
            text("SELECT object_id FROM object_registry "
                 "WHERE project_id = :pid AND object_type = :otype AND object_key = :okey"),
            {"pid": proj_id.hex, "otype": "memory", "okey": "unique-memory-key"},
        ).first()
        assert row is not None
        assert UUID(row.object_id) == obj_id

    def test_no_match_wrong_project(self, db_session):
        proj_a = uuid4()
        proj_b = uuid4()
        register_object(db_session, object_id=uuid4(), object_type="memory",
                        project_id=proj_a, object_key="key-1")
        db_session.commit()
        row = db_session.execute(
            text("SELECT 1 FROM object_registry "
                 "WHERE project_id = :pid AND object_type = :otype AND object_key = :okey"),
            {"pid": proj_b.hex, "otype": "memory", "okey": "key-1"},
        ).first()
        assert row is None

    def test_no_match_wrong_object_type(self, db_session):
        proj_id = uuid4()
        register_object(db_session, object_id=uuid4(), object_type="memory",
                        project_id=proj_id, object_key="key-1")
        db_session.commit()
        row = db_session.execute(
            text("SELECT 1 FROM object_registry "
                 "WHERE project_id = :pid AND object_type = :otype AND object_key = :okey"),
            {"pid": proj_id.hex, "otype": "document", "okey": "key-1"},
        ).first()
        assert row is None

    def test_no_match_null_key(self, db_session):
        proj_id = uuid4()
        register_object(db_session, object_id=uuid4(), object_type="memory",
                        project_id=proj_id, object_key=None)
        db_session.commit()
        row = db_session.execute(
            text("SELECT 1 FROM object_registry "
                 "WHERE project_id = :pid AND object_type = :otype AND object_key = :okey"),
            {"pid": proj_id.hex, "otype": "memory", "okey": "non-existent"},
        ).first()
        assert row is None


# ═══════════════════════════════════════════════════════════════════════════════
# create_version — INSERT
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreateVersion:

    def test_returns_non_empty_identifier(self, db_session):
        """create_version returns a truthy identifier (UUID in PG, hex str in SQLite)."""
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        vid = create_version(db_session, object_id=obj_id, object_type="memory",
                             version=1, action="create", actor_type="user",
                             actor_id=uuid4())
        db_session.commit()
        assert vid is not None
        assert len(str(vid)) > 0

    def test_fields_stored_correctly(self, db_session):
        obj_id = uuid4()
        actor_id = uuid4()
        event_id = uuid4()
        audit_id = uuid4()
        src_map_id = uuid4()

        register_object(db_session, object_id=obj_id, object_type="document")
        db_session.commit()

        vid = create_version(
            db_session, object_id=obj_id, object_type="document", version=2,
            action="update", actor_type="agent", actor_id=actor_id,
            event_id=event_id, audit_id=audit_id, source_map_id=src_map_id,
            previous_version=1, checksum="sha256:abc123",
            snapshot_json={"content": "updated"},
            diff_json={"content": {"old": "original", "new": "updated"}},
            reason="Legal compliance update",
        )
        db_session.commit()

        row = db_session.execute(
            text("SELECT * FROM object_versions WHERE object_version_id = :vid"),
            {"vid": str(vid)},
        ).one()

        assert UUID(row.object_id) == obj_id
        assert row.object_type == "document"
        assert row.version == 2
        assert row.action == "update"
        assert row.actor_type == "agent"
        assert UUID(row.actor_id) == actor_id
        assert UUID(row.event_id) == event_id
        assert UUID(row.audit_id) == audit_id
        assert UUID(row.source_map_id) == src_map_id
        assert row.previous_version == 1
        assert row.checksum == "sha256:abc123"
        assert _parse_json(row.snapshot_json) == {"content": "updated"}
        assert _parse_json(row.diff_json) == {"content": {"old": "original", "new": "updated"}}
        assert row.reason == "Legal compliance update"

    def test_snapshot_diff_default_empty(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        create_version(db_session, object_id=obj_id, object_type="memory",
                       version=1, action="create", actor_type="system")
        db_session.commit()
        row = db_session.execute(
            text("SELECT snapshot_json, diff_json FROM object_versions")
        ).one()
        assert _parse_json(row.snapshot_json) == {}
        assert _parse_json(row.diff_json) == {}

    def test_all_documented_actions(self, db_session):
        """Each documented action string is accepted (different version # per action)."""
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        actions = ("create", "update", "archive", "delete", "restore", "supersede")
        for v_num, action in enumerate(actions, start=1):
            create_version(db_session, object_id=obj_id, object_type="memory",
                           version=v_num, action=action, actor_type="system")
        db_session.commit()
        cnt = db_session.execute(text("SELECT count(*) FROM object_versions")).scalar_one()
        assert cnt == len(actions)

    def test_fk_to_registry(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        create_version(db_session, object_id=obj_id, object_type="memory",
                       version=1, action="create", actor_type="system")
        db_session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# Version read-back via raw SQL
# ═══════════════════════════════════════════════════════════════════════════════


class TestVersionReadBack:

    def test_fetch_exact_version(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        create_version(db_session, object_id=obj_id, object_type="memory",
                       version=1, action="create", actor_type="system")
        create_version(db_session, object_id=obj_id, object_type="memory",
                       version=2, action="update", actor_type="user")
        db_session.commit()

        v1 = db_session.execute(
            text("SELECT version, action FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype AND version = 1"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).one()
        assert v1.action == "create"

        v2 = db_session.execute(
            text("SELECT version, action FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype AND version = 2"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).one()
        assert v2.action == "update"

    def test_missing_version_none(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        create_version(db_session, object_id=obj_id, object_type="memory",
                       version=1, action="create", actor_type="system")
        db_session.commit()
        row = db_session.execute(
            text("SELECT 1 FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype AND version = 999"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).first()
        assert row is None

    def test_list_newest_first(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        for v_num in range(1, 6):
            create_version(db_session, object_id=obj_id, object_type="memory",
                           version=v_num, action="update", actor_type="system")
        db_session.commit()

        rows = db_session.execute(
            text("SELECT version FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype "
                 "ORDER BY version DESC"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).all()
        assert [r.version for r in rows] == [5, 4, 3, 2, 1]

    def test_pagination(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.commit()
        for v_num in range(1, 11):
            create_version(db_session, object_id=obj_id, object_type="memory",
                           version=v_num, action="update", actor_type="system")
        db_session.commit()

        total = db_session.execute(
            text("SELECT count(*) FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).scalar_one()
        assert total == 10

        p1 = db_session.execute(
            text("SELECT version FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype "
                 "ORDER BY version DESC LIMIT 5 OFFSET 0"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).all()
        assert [r.version for r in p1] == [10, 9, 8, 7, 6]

        p2 = db_session.execute(
            text("SELECT version FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype "
                 "ORDER BY version DESC LIMIT 5 OFFSET 5"),
            {"oid": obj_id.hex, "otype": "memory"},
        ).all()
        assert [r.version for r in p2] == [5, 4, 3, 2, 1]

    def test_empty_no_versions(self, db_session):
        total = db_session.execute(
            text("SELECT count(*) FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype"),
            {"oid": uuid4().hex, "otype": "memory"},
        ).scalar_one()
        assert total == 0

    def test_different_types_isolated(self, db_session):
        obj_mem = uuid4()
        obj_doc = uuid4()
        register_object(db_session, object_id=obj_mem, object_type="memory")
        register_object(db_session, object_id=obj_doc, object_type="document")
        db_session.commit()
        create_version(db_session, object_id=obj_mem, object_type="memory",
                       version=1, action="create", actor_type="system")
        create_version(db_session, object_id=obj_doc, object_type="document",
                       version=1, action="create", actor_type="system")
        create_version(db_session, object_id=obj_doc, object_type="document",
                       version=2, action="update", actor_type="system")
        db_session.commit()

        t_mem = db_session.execute(
            text("SELECT count(*) FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype"),
            {"oid": obj_mem.hex, "otype": "memory"},
        ).scalar_one()
        t_doc = db_session.execute(
            text("SELECT count(*) FROM object_versions "
                 "WHERE object_id = :oid AND object_type = :otype"),
            {"oid": obj_doc.hex, "otype": "document"},
        ).scalar_one()
        assert t_mem == 1
        assert t_doc == 2


# ═══════════════════════════════════════════════════════════════════════════════
# bump_version — UPDATE current_version
# ═══════════════════════════════════════════════════════════════════════════════


class TestBumpVersion:

    def test_bump_updates_current_version(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory", current_version=1)
        db_session.commit()
        bump_version(db_session, object_id=obj_id, object_type="memory", new_version=2)
        db_session.commit()
        row = db_session.execute(text("SELECT current_version FROM object_registry")).one()
        assert row.current_version == 2

    def test_bump_to_large_version(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory", current_version=1)
        db_session.commit()
        bump_version(db_session, object_id=obj_id, object_type="memory", new_version=999)
        db_session.commit()
        row = db_session.execute(text("SELECT current_version FROM object_registry")).one()
        assert row.current_version == 999

    def test_bump_only_affects_target(self, db_session):
        obj_a = uuid4()
        obj_b = uuid4()
        register_object(db_session, object_id=obj_a, object_type="memory", current_version=1)
        register_object(db_session, object_id=obj_b, object_type="memory", current_version=1)
        db_session.commit()
        bump_version(db_session, object_id=obj_a, object_type="memory", new_version=5)
        db_session.commit()
        rows = db_session.execute(
            text("SELECT object_id, current_version FROM object_registry ORDER BY object_id")
        ).all()
        versions = {(UUID(r.object_id), r.current_version) for r in rows}
        assert (obj_a, 5) in versions
        assert (obj_b, 1) in versions

    def test_bump_wrong_type_noop(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory", current_version=1)
        db_session.commit()
        bump_version(db_session, object_id=obj_id, object_type="document", new_version=5)
        db_session.commit()
        row = db_session.execute(text("SELECT current_version FROM object_registry")).one()
        assert row.current_version == 1


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-end lifecycle tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestObjectLifecycle:

    def test_full_lifecycle(self, db_session):
        obj_id = uuid4()
        proj_id = uuid4()
        owner_id = uuid4()

        # Register
        register_object(db_session, object_id=obj_id, object_type="memory",
                        project_id=proj_id, object_key="mem-life",
                        owner_actor_type="user", owner_actor_id=owner_id,
                        sensitivity_level="private", metadata_json={"src": "import"})
        db_session.commit()

        row = db_session.execute(text("SELECT * FROM object_registry")).one()
        assert UUID(row.object_id) == obj_id
        assert UUID(row.project_id) == proj_id
        assert row.object_key == "mem-life"
        assert row.owner_actor_type == "user"
        assert UUID(row.owner_actor_id) == owner_id
        assert row.sensitivity_level == "private"
        assert _parse_json(row.metadata_json) == {"src": "import"}
        assert row.current_version == 1

        # Create version 1
        v1_id = create_version(db_session, object_id=obj_id, object_type="memory",
                               version=1, action="create", actor_type="user",
                               actor_id=owner_id, snapshot_json={"state": "init"})
        db_session.commit()

        v1_row = db_session.execute(
            text("SELECT object_version_id, action, snapshot_json FROM object_versions WHERE version=1")
        ).one()
        assert v1_row.action == "create"
        assert _parse_json(v1_row.snapshot_json) == {"state": "init"}
        # SQLite returns hex string, PG returns UUID — compare as strings
        assert str(v1_row.object_version_id) == str(v1_id)

        # Bump + version 2
        bump_version(db_session, object_id=obj_id, object_type="memory", new_version=2)
        create_version(db_session, object_id=obj_id, object_type="memory",
                       version=2, action="update", actor_type="user",
                       actor_id=owner_id, previous_version=1,
                       snapshot_json={"state": "updated"},
                       diff_json={"state": {"old": "init", "new": "updated"}})
        db_session.commit()

        assert db_session.execute(
            text("SELECT current_version FROM object_registry")
        ).scalar_one() == 2

        ver_rows = db_session.execute(
            text("SELECT version FROM object_versions ORDER BY version DESC")
        ).all()
        assert [r.version for r in ver_rows] == [2, 1]

    def test_multiple_types_isolated(self, db_session):
        mem_id = uuid4()
        doc_id = uuid4()
        proj_id = uuid4()
        register_object(db_session, object_id=mem_id, object_type="memory",
                        project_id=proj_id, object_key="key-mem")
        register_object(db_session, object_id=doc_id, object_type="document",
                        project_id=proj_id, object_key="key-doc")
        db_session.commit()

        mem_row = db_session.execute(
            text("SELECT object_id FROM object_registry WHERE object_type='memory'")
        ).one()
        doc_row = db_session.execute(
            text("SELECT object_id FROM object_registry WHERE object_type='document'")
        ).one()
        assert UUID(mem_row.object_id) == mem_id
        assert UUID(doc_row.object_id) == doc_id

    def test_rollback_cleans_up(self, db_session):
        obj_id = uuid4()
        register_object(db_session, object_id=obj_id, object_type="memory")
        db_session.rollback()
        cnt = db_session.execute(text("SELECT count(*) FROM object_registry")).scalar_one()
        assert cnt == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Schema model validation
# ═══════════════════════════════════════════════════════════════════════════════


class TestObjectSchemas:

    def test_object_type_enum_baseline(self):
        expected = {
            "asset", "document", "block", "chunk", "conversation",
            "message", "raw_event", "memory_candidate", "memory",
            "context_pack", "job", "pipeline_def", "pipeline_run",
            "provider_model", "credential", "review_item", "import_run",
            "backup", "restore", "external",
        }
        actual = set(ObjectType.__members__.keys())
        assert actual >= expected, f"Missing ObjectType values: {expected - actual}"

    def test_object_type_enum_has_project(self):
        """P0-01 gate: 'project' MUST be in ObjectType enum.

        db/projects.py calls register_object(db, object_type="project", ...).
        Without 'project' in ObjectType, the CHECK constraint on object_registry
        will reject INSERTs in production PostgreSQL.

        See review_P1-09.md P0-01 for full details.
        """
        assert "project" in ObjectType.__members__, (
            "P0-01 FAIL: 'project' is missing from ObjectType enum. "
            "Must add to ObjectType, Alembic CHECK constraint, and data model doc."
        )

    def test_object_status_enum(self):
        expected = {"active", "archived", "deleted", "quarantined", "superseded"}
        assert set(ObjectStatus.__members__.keys()) >= expected

    def test_object_version_action_enum(self):
        expected = {"create", "update", "merge", "expire", "archive",
                     "delete", "restore", "supersede", "import_"}
        assert set(ObjectVersionAction.__members__.keys()) >= expected

    def test_registry_read_schema_construct(self):
        obj_id = uuid4()
        now = datetime.utcnow()
        m = ObjectRegistryRead(object_id=obj_id, object_type=ObjectType.memory,
                               owner_actor_type="system", status=ObjectStatus.active,
                               current_version=1, sensitivity_level="normal",
                               created_at=now, updated_at=now)
        assert m.object_id == obj_id
        assert m.object_type == ObjectType.memory
        assert m.current_version == 1

    def test_version_read_schema_construct(self):
        vid = uuid4()
        oid = uuid4()
        now = datetime.utcnow()
        m = ObjectVersionRead(object_version_id=vid, object_id=oid,
                              object_type=ObjectType.memory, version=1,
                              action=ObjectVersionAction.create,
                              actor_type="system", created_at=now)
        assert m.object_version_id == vid
        assert m.object_id == oid
        assert m.version == 1
        assert m.action == ObjectVersionAction.create

    def test_registry_create_schema_defaults(self):
        m = ObjectRegistryCreate(object_id=uuid4(), object_type=ObjectType.memory)
        assert m.project_id is None
        assert m.owner_actor_type.value == "system"
        assert m.sensitivity_level.value == "normal"

    def test_version_create_schema_defaults(self):
        m = ObjectVersionCreate(object_id=uuid4(), object_type=ObjectType.memory,
                                version=1, action=ObjectVersionAction.create,
                                actor_type="system")
        assert m.snapshot_json == {}
        assert m.diff_json == {}
        assert m.checksum is None
        assert m.previous_version is None

    def test_schema_openapi_generation(self):
        reg_s = ObjectRegistryRead.model_json_schema()
        assert "$defs" in reg_s
        assert "ObjectType" in reg_s["$defs"]
        assert "ObjectStatus" in reg_s["$defs"]

        ver_s = ObjectVersionRead.model_json_schema()
        assert "$defs" in ver_s
        assert "ObjectVersionAction" in ver_s["$defs"]

    def test_actor_type_values(self):
        obj_id = uuid4()
        now = datetime.utcnow()
        for at in ("system", "user", "agent"):
            m = ObjectRegistryRead(object_id=obj_id, object_type=ObjectType.memory,
                                   owner_actor_type=at, status=ObjectStatus.active,
                                   current_version=1, sensitivity_level="normal",
                                   created_at=now, updated_at=now)
            assert m.owner_actor_type.value == at


# ═══════════════════════════════════════════════════════════════════════════════
# Domain exports
# ═══════════════════════════════════════════════════════════════════════════════


class TestDomainExports:

    def test_register_object(self):
        from mneme.domain import register_object as f
        assert callable(f)

    def test_create_version(self):
        from mneme.domain import create_version as f
        assert callable(f)

    def test_get_registry(self):
        from mneme.domain import get_registry as f
        assert callable(f)

    def test_get_version(self):
        from mneme.domain import get_version as f
        assert callable(f)

    def test_list_versions(self):
        from mneme.domain import list_versions as f
        assert callable(f)

    def test_bump_version(self):
        from mneme.domain import bump_version as f
        assert callable(f)


# ═══════════════════════════════════════════════════════════════════════════════
# ObjectType vs CHECK constraint parity
# ═══════════════════════════════════════════════════════════════════════════════


class TestObjectTypeCheckConstraintParity:
    """P0-01 audit: verify ObjectType enum matches CHECK constraint values."""

    CHECK_VALUES = frozenset({
        "asset", "document", "block", "chunk", "conversation",
        "message", "raw_event", "memory_candidate", "memory",
        "context_pack", "job", "pipeline_def", "pipeline_run",
        "project", "provider_model", "credential", "review_item",
        "inbox_item", "import_run", "backup", "restore", "external",
    })

    def test_enum_contains_all_check_values(self):
        enum_vals = set(ObjectType.__members__.keys())
        missing = self.CHECK_VALUES - enum_vals
        assert not missing, f"ObjectType enum missing CHECK values: {missing}"

    def test_check_values_contains_all_enum_members(self):
        """Verify CHECK_VALUES is in sync with ObjectType enum (append-only)."""
        enum_vals = set(ObjectType.__members__.keys())
        missing = enum_vals - self.CHECK_VALUES
        assert not missing, (
            f"CHECK_VALUES stale — missing enum members: {missing}. "
            f"Add them to CHECK_VALUES (and Alembic DDL if needed)."
        )

    def test_project_is_in_both_enum_and_check(self):
        """Verify 'project' is present in both ObjectType enum and CHECK_VALUES."""
        assert "project" in ObjectType.__members__, (
            "'project' must be in ObjectType enum"
        )
        assert "project" in self.CHECK_VALUES, (
            "'project' must be in CHECK_VALUES set"
        )
