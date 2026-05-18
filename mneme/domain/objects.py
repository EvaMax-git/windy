"""Object Registry and Object Versions helpers.

The ``object_registry`` is the global object ledger — every formal domain
object (project, memory, asset, etc.) must be registered here with the *same*
primary key as the domain table.  The ``object_versions`` table provides a
cross-domain version index and audit snapshot, recording every state mutation
(action, actor, snapshot, diff, and pointers to the corresponding audit and
outbox event rows).

Integration pattern (compose inside the P1-08 transaction wrapper)
------------------------------------------------------------------

The helpers in this module are *leaf* database operations.  They do **not**
manage their own transaction boundaries.  Instead, callers embed them inside
the ``work`` callback passed to
:func:`mneme.db.audit.write_with_audit_and_outbox` (or
:func:`mneme.db.audit.write_with_audit_outbox_idempotency`) so that every
object registration and version insertion lands in the **same transaction** as
the domain row, the audit event, and the outbox event.

Minimal example for a domain object creation::

    from mneme.db.audit import write_with_audit_outbox_idempotency
    from mneme.domain.objects import register_object, create_version

    def _do_insert(db: Session) -> DomainRead:
        obj_id = uuid4()
        # 1. Domain row
        db.execute(_INSERT_DOMAIN, {"id": obj_id, ...})
        # 2. Registry entry
        register_object(
            db,
            object_id=obj_id,
            project_id=project_id,
            object_type="memory",
            owner_actor_type=context.actor.actor_type,
            owner_actor_id=context.actor.actor_id,
            ...
        )
        # 3. Initial version (action='create', version=1)
        create_version(
            db,
            object_id=obj_id,
            object_type="memory",
            version=1,
            action="create",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
        )
        return DomainRead(...)

    result = write_with_audit_outbox_idempotency(
        db, context,
        work=_do_insert,
        audit_event=..., outbox_event=...,
    )

See Also
--------
* :mod:`mneme.db.projects` — the Phase 1 canonical write path that uses this
  pattern.
* :doc:`/doc/Mneme_数据模型_完整版` § 10.3 — object_registry / object_versions
  contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.schemas.objects import (
    ObjectRegistryRead,
    ObjectVersionRead,
)

# ═══════════════════════════════════════════════════════════════════
# INSERT: object_registry
# ═══════════════════════════════════════════════════════════════════

_INSERT_REGISTRY = text(
    """
    INSERT INTO object_registry (
      object_id,
      project_id,
      object_type,
      object_key,
      owner_actor_type,
      owner_actor_id,
      status,
      current_version,
      sensitivity_level,
      source_type,
      source_id,
      canonical_uri,
      metadata_json
    )
    VALUES (
      :object_id,
      :project_id,
      :object_type,
      :object_key,
      :owner_actor_type,
      :owner_actor_id,
      :status,
      :current_version,
      :sensitivity_level,
      :source_type,
      :source_id,
      :canonical_uri,
      :metadata_json
    )
    """
).bindparams(
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("owner_actor_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_id", type_=PG_UUID(as_uuid=True)),
    bindparam("metadata_json", type_=JSONB),
)


def register_object(
    db: Session,
    *,
    object_id: UUID,
    object_type: str,
    project_id: UUID | None = None,
    object_key: str | None = None,
    owner_actor_type: str = "system",
    owner_actor_id: UUID | None = None,
    status: str = "active",
    current_version: int = 1,
    sensitivity_level: str = "normal",
    source_type: str | None = None,
    source_id: UUID | None = None,
    canonical_uri: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    """Insert a row into ``object_registry``.

    The ``object_id`` **must** be identical to the domain table's primary key.
    There is no separate mapping/surrogate id — ``object_registry.object_id``
    *is* the domain PK (see data-model § 10.3).

    All parameters are keyword-only to prevent accidental positional misuse.

    This function does **not** manage its own transaction.  Callers must
    invoke it inside an existing transaction (typically the ``work`` callback
    of :func:`mneme.db.audit.write_with_audit_and_outbox`).
    """
    db.execute(
        _INSERT_REGISTRY,
        {
            "object_id": object_id,
            "project_id": project_id,
            "object_type": object_type,
            "object_key": object_key,
            "owner_actor_type": owner_actor_type,
            "owner_actor_id": owner_actor_id,
            "status": status,
            "current_version": current_version,
            "sensitivity_level": sensitivity_level,
            "source_type": source_type,
            "source_id": source_id,
            "canonical_uri": canonical_uri,
            "metadata_json": metadata_json or {},
        },
    )


# ═══════════════════════════════════════════════════════════════════
# INSERT: object_versions
# ═══════════════════════════════════════════════════════════════════

_INSERT_VERSION = text(
    """
    INSERT INTO object_versions (
      object_id,
      object_type,
      version,
      action,
      actor_type,
      actor_id,
      event_id,
      audit_id,
      source_map_id,
      previous_version,
      checksum,
      snapshot_json,
      diff_json,
      reason
    )
    VALUES (
      :object_id,
      :object_type,
      :version,
      :action,
      :actor_type,
      :actor_id,
      :event_id,
      :audit_id,
      :source_map_id,
      :previous_version,
      :checksum,
      :snapshot_json,
      :diff_json,
      :reason
    )
    RETURNING object_version_id
    """
).bindparams(
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
    bindparam("actor_id", type_=PG_UUID(as_uuid=True)),
    bindparam("event_id", type_=PG_UUID(as_uuid=True)),
    bindparam("audit_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_map_id", type_=PG_UUID(as_uuid=True)),
    bindparam("snapshot_json", type_=JSONB),
    bindparam("diff_json", type_=JSONB),
)


def create_version(
    db: Session,
    *,
    object_id: UUID,
    object_type: str,
    version: int,
    action: str,
    actor_type: str,
    actor_id: UUID | None = None,
    event_id: UUID | None = None,
    audit_id: UUID | None = None,
    source_map_id: UUID | None = None,
    previous_version: int | None = None,
    checksum: str | None = None,
    snapshot_json: dict[str, Any] | None = None,
    diff_json: dict[str, Any] | None = None,
    reason: str | None = None,
) -> UUID:
    """Insert a row into ``object_versions`` and return the new ``object_version_id``.

    Every object lifecycle mutation — create, update, archive, delete,
    restore, supersede, etc. — must insert a corresponding version row.

    ``event_id`` and ``audit_id`` should point to the ``events`` and
    ``audit_events`` rows produced by the same logical write.  This enables
    full traceability: ``request_id`` → audit → version → domain snapshot.

    This function does **not** manage its own transaction.  Callers must
    invoke it inside an existing transaction.
    """
    return db.execute(
        _INSERT_VERSION,
        {
            "object_id": object_id,
            "object_type": object_type,
            "version": version,
            "action": action,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "event_id": event_id,
            "audit_id": audit_id,
            "source_map_id": source_map_id,
            "previous_version": previous_version,
            "checksum": checksum,
            "snapshot_json": snapshot_json or {},
            "diff_json": diff_json or {},
            "reason": reason,
        },
    ).scalar_one()


# ═══════════════════════════════════════════════════════════════════
# UPDATE: object_registry.current_version
# ═══════════════════════════════════════════════════════════════════

_UPDATE_REGISTRY_VERSION = text(
    """
    UPDATE object_registry
    SET current_version = :new_version
    WHERE object_id = :object_id
      AND object_type = :object_type
    """
).bindparams(
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
)


def bump_version(
    db: Session,
    *,
    object_id: UUID,
    object_type: str,
    new_version: int,
) -> None:
    """Update ``object_registry.current_version`` to *new_version*.

    Callers **must** have already inserted the corresponding
    ``object_versions`` row (or be about to do so inside the same
    transaction).  Failing to keep the two in sync violates the data-model
    contract.

    This is a low-level helper — the high-level pattern for a domain update
    is::

        # Inside the work callback:
        bump_version(db, object_id=..., object_type=..., new_version=next_ver)
        create_version(db, object_id=..., object_type=..., version=next_ver, ...)
        # ... and the domain-row UPDATE as well.
    """
    db.execute(
        _UPDATE_REGISTRY_VERSION,
        {
            "object_id": object_id,
            "object_type": object_type,
            "new_version": new_version,
        },
    )


# ═══════════════════════════════════════════════════════════════════
# SELECT helpers
# ═══════════════════════════════════════════════════════════════════

_SELECT_REGISTRY_BY_ID = text(
    """
    SELECT
      object_id,
      project_id,
      object_type,
      object_key,
      owner_actor_type,
      owner_actor_id,
      status,
      current_version,
      sensitivity_level,
      source_type,
      source_id,
      canonical_uri,
      metadata_json,
      created_at,
      updated_at,
      archived_at
    FROM object_registry
    WHERE object_id = :object_id
      AND object_type = :object_type
    """
).bindparams(
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_REGISTRY_BY_KEY = text(
    """
    SELECT
      object_id,
      project_id,
      object_type,
      object_key,
      owner_actor_type,
      owner_actor_id,
      status,
      current_version,
      sensitivity_level,
      source_type,
      source_id,
      canonical_uri,
      metadata_json,
      created_at,
      updated_at,
      archived_at
    FROM object_registry
    WHERE project_id = :project_id
      AND object_type = :object_type
      AND object_key = :object_key
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_VERSIONS = text(
    """
    SELECT
      object_version_id,
      object_id,
      object_type,
      version,
      action,
      actor_type,
      actor_id,
      event_id,
      audit_id,
      source_map_id,
      previous_version,
      checksum,
      snapshot_json,
      diff_json,
      reason,
      created_at
    FROM object_versions
    WHERE object_id = :object_id
      AND object_type = :object_type
    ORDER BY version DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_VERSION_BY_NO = text(
    """
    SELECT
      object_version_id,
      object_id,
      object_type,
      version,
      action,
      actor_type,
      actor_id,
      event_id,
      audit_id,
      source_map_id,
      previous_version,
      checksum,
      snapshot_json,
      diff_json,
      reason,
      created_at
    FROM object_versions
    WHERE object_id = :object_id
      AND object_type = :object_type
      AND version = :version
    """
).bindparams(
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
)

_COUNT_VERSIONS = text(
    """
    SELECT count(*)
    FROM object_versions
    WHERE object_id = :object_id
      AND object_type = :object_type
    """
).bindparams(
    bindparam("object_id", type_=PG_UUID(as_uuid=True)),
)


def _registry_from_row(row: Any) -> ObjectRegistryRead:
    data = dict(row._mapping)
    # SQLite returns JSONB columns as strings; parse them back to dicts
    if "metadata_json" in data and isinstance(data["metadata_json"], str):
        try:
            data["metadata_json"] = json.loads(data["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            data["metadata_json"] = {}
    return ObjectRegistryRead.model_validate(data)


def _version_from_row(row: Any) -> ObjectVersionRead:
    data = dict(row._mapping)
    # SQLite returns JSONB columns as strings; parse them back to dicts
    for json_field in ("snapshot_json", "diff_json"):
        if json_field in data and isinstance(data[json_field], str):
            try:
                data[json_field] = json.loads(data[json_field])
            except (json.JSONDecodeError, TypeError):
                data[json_field] = {}
    return ObjectVersionRead.model_validate(data)


def get_registry(
    db: Session,
    *,
    object_id: UUID,
    object_type: str,
) -> ObjectRegistryRead | None:
    """Look up an object_registry entry by its (object_id, object_type)."""
    row = db.execute(
        _SELECT_REGISTRY_BY_ID,
        {"object_id": object_id, "object_type": object_type},
    ).first()
    if row is None:
        return None
    return _registry_from_row(row)


def get_registry_by_key(
    db: Session,
    *,
    project_id: UUID,
    object_type: str,
    object_key: str,
) -> ObjectRegistryRead | None:
    """Look up an object_registry entry by its project-scoped unique key.

    Only rows where ``object_key IS NOT NULL`` are indexed/candidate for this
    lookup (the partial unique index
    ``uq_object_registry_project_key`` enforces this).
    """
    row = db.execute(
        _SELECT_REGISTRY_BY_KEY,
        {
            "project_id": project_id,
            "object_type": object_type,
            "object_key": object_key,
        },
    ).first()
    if row is None:
        return None
    return _registry_from_row(row)


def get_version(
    db: Session,
    *,
    object_id: UUID,
    object_type: str,
    version: int,
) -> ObjectVersionRead | None:
    """Fetch a specific version row by exact version number."""
    row = db.execute(
        _SELECT_VERSION_BY_NO,
        {"object_id": object_id, "object_type": object_type, "version": version},
    ).first()
    if row is None:
        return None
    return _version_from_row(row)


def list_versions(
    db: Session,
    *,
    object_id: UUID,
    object_type: str,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ObjectVersionRead], int]:
    """List version rows for an object, newest first (paginated).

    Returns ``(items, total_count)``.
    """
    total = db.execute(
        _COUNT_VERSIONS,
        {"object_id": object_id, "object_type": object_type},
    ).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _SELECT_VERSIONS,
        {
            "object_id": object_id,
            "object_type": object_type,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()
    versions = [_version_from_row(row) for row in rows]
    return versions, total
