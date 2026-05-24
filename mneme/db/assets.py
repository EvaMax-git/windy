"""Asset CRUD with audit + outbox + idempotency + object registry.

Every mutation that creates or modifies an ``assets`` or ``asset_metadata``
row is wrapped in :func:`mneme.db.audit.write_with_audit_outbox_idempotency`
so that:

* An ``assets`` / ``asset_metadata`` row is written.
* An ``object_registry`` row is written (P1-09).
* An ``object_versions`` row is written (with ``action='create'`` or ``action='update'``).
* An ``audit_events`` row is written.
* An ``events`` (outbox) row is written.
* All land in the same database transaction.

Ingest state machine
--------------------
``pending → staged → importing → ready`` (or ``failed``)

Asset UID format
----------------
``{project_code}-{hash_prefix[:12]}-{timestamp_ms}``

Storage ref strategy
--------------------
* asset creation → ``'pending'``
* after ``promote_from_staging()`` → formal path
  ``mneme_data/assets/{project_id}/{asset_uid}/{original_filename}``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.domain.objects import (
    bump_version,
    create_version,
    register_object,
)
from mneme.schemas.asset_metadata import (
    AssetMetadataCreateRequest,
    AssetMetadataRead,
    AssetMetadataUpdateRequest,
)
from mneme.schemas.storage import (
    AssetCreateRequest,
    AssetRead,
)


# ═══════════════════════════════════════════════════════════════════
# SQL — Assets
# ═══════════════════════════════════════════════════════════════════

_INSERT_ASSET = text(
    """
    INSERT INTO assets (
      asset_id,
      project_id,
      asset_uid,
      title,
      asset_type,
      media_type,
      original_filename,
      storage_backend,
      storage_ref,
      canonical_uri,
      content_hash,
      size_bytes,
      status,
      ingest_state,
      knowledge_state,
      current_version,
      sensitivity_level,
      retention_policy,
      source_inbox_item_id,
      created_by_user_id,
      imported_from,
      imported_source_id,
      metadata_json
    )
    VALUES (
      :asset_id,
      :project_id,
      :asset_uid,
      :title,
      :asset_type,
      :media_type,
      :original_filename,
      :storage_backend,
      :storage_ref,
      :canonical_uri,
      :content_hash,
      :size_bytes,
      :status,
      :ingest_state,
      :knowledge_state,
      :current_version,
      :sensitivity_level,
      :retention_policy,
      :source_inbox_item_id,
      :created_by_user_id,
      :imported_from,
      :imported_source_id,
      :metadata_json
    )
    RETURNING
      asset_id,
      project_id,
      asset_uid,
      title,
      asset_type,
      media_type,
      original_filename,
      storage_backend,
      storage_ref,
      canonical_uri,
      content_hash,
      size_bytes,
      status,
      ingest_state,
      knowledge_state,
      current_version,
      sensitivity_level,
      retention_policy,
      source_inbox_item_id,
      created_by_user_id,
      imported_from,
      imported_source_id,
      created_at,
      updated_at,
      archived_at
    """
).bindparams(
    bindparam("asset_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_inbox_item_id", type_=PG_UUID(as_uuid=True)),
    bindparam("created_by_user_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_ASSET_BY_ID = text(
    """
    SELECT
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    FROM assets
    WHERE asset_id = :asset_id
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_SELECT_ASSET_BY_UID = text(
    """
    SELECT
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    FROM assets
    WHERE project_id = :project_id AND asset_uid = :asset_uid
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LOOKUP_ASSET_BY_HASH = text(
    """
    SELECT
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    FROM assets
    WHERE content_hash = :content_hash
      AND (:project_id IS NULL OR project_id = :project_id)
      AND status != 'deleted'
    ORDER BY created_at DESC
    LIMIT 1
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_ASSETS_COUNT = text(
    """
    SELECT count(*) FROM assets
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:asset_type IS NULL OR asset_type = :asset_type)
      AND (:knowledge_state IS NULL OR knowledge_state = :knowledge_state)
      AND (:sensitivity_level IS NULL OR sensitivity_level = :sensitivity_level)
      AND (:status IS NULL OR status = :status)
      AND (:ingest_state IS NULL OR ingest_state = :ingest_state)
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_ASSETS = text(
    """
    SELECT
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    FROM assets
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:asset_type IS NULL OR asset_type = :asset_type)
      AND (:knowledge_state IS NULL OR knowledge_state = :knowledge_state)
      AND (:sensitivity_level IS NULL OR sensitivity_level = :sensitivity_level)
      AND (:status IS NULL OR status = :status)
      AND (:ingest_state IS NULL OR ingest_state = :ingest_state)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_ASSET = text(
    """
    UPDATE assets
    SET title = COALESCE(:title, title),
        sensitivity_level = COALESCE(:sensitivity_level, sensitivity_level),
        retention_policy = COALESCE(:retention_policy, retention_policy),
        current_version = current_version + 1,
        updated_at = now()
    WHERE asset_id = :asset_id
    RETURNING
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_ASSET_STATUS = text(
    """
    UPDATE assets
    SET status = :new_status,
        archived_at = CASE
            WHEN :new_status IN ('archived', 'deleted') THEN :now_ts
            WHEN :new_status = 'active' AND status IN ('archived', 'deleted') THEN NULL
            ELSE archived_at
        END,
        current_version = current_version + 1,
        updated_at = now()
    WHERE asset_id = :asset_id
      AND status = :expected_status
    RETURNING
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_PROMOTE_ASSET = text(
    """
    UPDATE assets
    SET storage_ref = :storage_ref,
        ingest_state = :ingest_state,
        size_bytes = COALESCE(:size_bytes, size_bytes),
        current_version = current_version + 1,
        updated_at = now()
    WHERE asset_id = :asset_id
      AND ingest_state = :expected_ingest_state
    RETURNING
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_INGEST_STATE = text(
    """
    UPDATE assets
    SET ingest_state = :new_ingest_state,
        current_version = current_version + 1,
        updated_at = now()
    WHERE asset_id = :asset_id
      AND ingest_state = :expected_ingest_state
    RETURNING
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_KNOWLEDGE_STATE = text(
    """
    UPDATE assets
    SET knowledge_state = :new_state,
        updated_at = now()
    WHERE asset_id = :asset_id
    RETURNING
      asset_id, project_id, asset_uid, title, asset_type,
      media_type, original_filename, storage_backend, storage_ref,
      canonical_uri, content_hash, size_bytes, status, ingest_state,
      knowledge_state, current_version, sensitivity_level,
      retention_policy, source_inbox_item_id, created_by_user_id,
      imported_from, imported_source_id, created_at, updated_at, archived_at
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

# ═══════════════════════════════════════════════════════════════════
# SQL — Project lookup (for asset_uid generation)
# ═══════════════════════════════════════════════════════════════════

_SELECT_PROJECT_CODE = text(
    """
    SELECT project_code FROM projects WHERE project_id = :project_id
    """
).bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# SQL — Asset Metadata
# ═══════════════════════════════════════════════════════════════════

_LIST_METADATA = text(
    """
    SELECT
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, value_type, source, confidence,
      created_at, updated_at
    FROM asset_metadata
    WHERE asset_id = :asset_id
    ORDER BY metadata_key ASC
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_METADATA_JSON = text(
    """
    UPDATE assets
    SET metadata_json = COALESCE(:metadata_json, metadata_json),
        updated_at = now()
    WHERE asset_id = :asset_id
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_SELECT_METADATA_BY_ID = text(
    """
    SELECT
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, value_type, source, confidence,
      created_at, updated_at
    FROM asset_metadata
    WHERE asset_metadata_id = :asset_metadata_id
    """
).bindparams(bindparam("asset_metadata_id", type_=PG_UUID(as_uuid=True)))

_DELETE_METADATA = text(
    """
    DELETE FROM asset_metadata
    WHERE asset_metadata_id = :asset_metadata_id
      AND asset_id = :asset_id
    RETURNING
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, value_type, source, confidence,
      created_at, updated_at
    """
).bindparams(
    bindparam("asset_metadata_id", type_=PG_UUID(as_uuid=True)),
    bindparam("asset_id", type_=PG_UUID(as_uuid=True)),
)

_UPSERT_METADATA = text(
    """
    INSERT INTO asset_metadata (
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, metadata_json, value_type, source, confidence
    )
    VALUES (
      :asset_metadata_id, :asset_id, :metadata_key,
      :metadata_value, :metadata_json, :value_type, :source, :confidence
    )
    ON CONFLICT (asset_id, metadata_key, source) DO UPDATE
    SET metadata_value = EXCLUDED.metadata_value,
        metadata_json = EXCLUDED.metadata_json,
        confidence = EXCLUDED.confidence,
        value_type = EXCLUDED.value_type,
        updated_at = now()
    RETURNING
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, value_type, source, confidence,
      created_at, updated_at
    """
).bindparams(
    bindparam("asset_metadata_id", type_=PG_UUID(as_uuid=True)),
    bindparam("asset_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_METADATA_BY_KEY = text(
    """
    SELECT
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, value_type, source, confidence,
      created_at, updated_at
    FROM asset_metadata
    WHERE asset_id = :asset_id
      AND metadata_key = :metadata_key
      AND source = :source
    LIMIT 1
    """
).bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════

def _asset_from_row(row: Any) -> AssetRead:
    data = dict(row._mapping)
    return AssetRead.model_validate(data)


def _metadata_from_row(row: Any) -> AssetMetadataRead:
    data = dict(row._mapping)
    return AssetMetadataRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════
# Value-type validation
# ═══════════════════════════════════════════════════════════════════

import re
from datetime import datetime as _dt


_VALID_VALUE_TYPES = {"text", "number", "boolean", "date", "json"}

# ISO 8601 date: YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Boolean accepted values
_BOOL_TRUE = {"true", "1", "yes"}
_BOOL_FALSE = {"false", "0", "no"}


def _validate_metadata_value(
    metadata_key: str,
    value: str | None,
    value_type: str,
) -> None:
    """Validate that *value* matches the declared *value_type*.

    Raises ``ValueError`` with a descriptive message on mismatch.

    Rules (mirroring ``asset_metadata.value_type`` CHECK constraint):
    - ``text``: any string (including ``None`` or empty).
    - ``number``: must be a valid numeric string (int or float).
    - ``boolean``: must be one of ``true/false/1/0/yes/no`` (case-insensitive).
    - ``date``: must be ISO 8601 ``YYYY-MM-DD``.
    - ``json``: must be valid JSON.
    - ``None`` value: always allowed (represents unset/cleared metadata).
    """
    if value_type not in _VALID_VALUE_TYPES:
        raise ValueError(
            f"Invalid value_type '{value_type}' for metadata key "
            f"'{metadata_key}'. Must be one of: {', '.join(sorted(_VALID_VALUE_TYPES))}."
        )

    if value is None:
        return

    if value_type == "number":
        try:
            float(value)
        except (ValueError, TypeError):
            raise ValueError(
                f"Metadata key '{metadata_key}': value '{value}' is not a valid "
                f"number for value_type='number'."
            )

    elif value_type == "boolean":
        if value.lower() not in _BOOL_TRUE | _BOOL_FALSE:
            raise ValueError(
                f"Metadata key '{metadata_key}': value '{value}' is not a valid "
                f"boolean. Accepted: true/false/1/0/yes/no (case-insensitive)."
            )

    elif value_type == "date":
        if not _DATE_RE.match(value):
            raise ValueError(
                f"Metadata key '{metadata_key}': value '{value}' is not a valid "
                f"date. Expected format: YYYY-MM-DD."
            )
        try:
            _dt.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"Metadata key '{metadata_key}': value '{value}' is not a valid "
                f"calendar date."
            )

    elif value_type == "json":
        try:
            json.loads(value)
        except (json.JSONDecodeError, TypeError):
            raise ValueError(
                f"Metadata key '{metadata_key}': value '{value}' is not valid JSON."
            )

    # "text" → always valid, no check needed


def _rebuild_metadata_cache(db: Session, asset_id: UUID) -> None:
    """Rebuild ``assets.metadata_json`` from current ``asset_metadata`` rows."""
    all_meta_rows = db.execute(
        _LIST_METADATA, {"asset_id": asset_id}
    ).all()
    cache: dict[str, Any] = {}
    for meta_row in all_meta_rows:
        meta_data = dict(meta_row._mapping)
        cache[meta_data["metadata_key"]] = meta_data["metadata_value"]

    db.execute(
        _UPDATE_METADATA_JSON,
        {"asset_id": asset_id, "metadata_json": json.dumps(cache)},
    )


def _idempotent_resolve(db: Session, asset_id: UUID) -> AssetRead:
    row = db.execute(_SELECT_ASSET_BY_ID, {"asset_id": asset_id}).first()
    if row is None:
        raise LookupError(f"asset {asset_id} not found during idempotent replay")
    return _asset_from_row(row)


# ═══════════════════════════════════════════════════════════════════
# Ingest state machine
# ═══════════════════════════════════════════════════════════════════

_VALID_INGEST_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"staged", "failed"},
    "staged": {"importing", "failed"},
    "importing": {"ready", "failed"},
    "ready": {"failed"},
    "failed": {"pending"},
}


def _can_transition_ingest(current: str, target: str) -> bool:
    return target in _VALID_INGEST_TRANSITIONS.get(current, set())


# ═══════════════════════════════════════════════════════════════════
# Status state machine
# ═══════════════════════════════════════════════════════════════════

_VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "active": {"archived", "deleted", "quarantined"},
    "archived": {"active"},
    "deleted": {"active"},
    "quarantined": {"active"},
}


def _can_transition_status(current: str, target: str) -> bool:
    return target in _VALID_STATUS_TRANSITIONS.get(current, set())


def _resolve_status_value(status: Any) -> str:
    """Normalise a status value to its string representation."""
    if status is None:
        return ""
    if hasattr(status, "value"):
        return status.value
    return str(status)


# ═══════════════════════════════════════════════════════════════════
# Asset UID generation
# ═══════════════════════════════════════════════════════════════════

def _generate_asset_uid(project_code: str, content_hash: str) -> str:
    """Generate a globally unique asset identifier.

    Format: ``{project_code}-{hash_prefix[:12]}-{timestamp_ms}``
    """
    hash_prefix = content_hash[:12]
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"{project_code}-{hash_prefix}-{timestamp_ms}"


def _lookup_project_code(db: Session, project_id: UUID) -> str:
    """Get the project_code for the given project_id."""
    row = db.execute(
        _SELECT_PROJECT_CODE, {"project_id": project_id}
    ).first()
    if row is None:
        raise ValueError(f"Project {project_id} not found")
    return row[0]


# ═══════════════════════════════════════════════════════════════════
# Public API — Assets
# ═══════════════════════════════════════════════════════════════════


def create_asset(
    db: Session,
    context: RequestContext,
    *,
    payload: AssetCreateRequest,
    project_code: str | None = None,
) -> AssetRead:
    """Create an asset with audit, outbox, idempotency, and object registry."""
    asset_id = uuid4()
    object_type = "asset"

    if project_code is None:
        project_code = _lookup_project_code(db, payload.project_id) if payload.project_id else "default"

    asset_uid = _generate_asset_uid(project_code, payload.content_hash)
    canonical_uri = payload.canonical_uri or f"mneme://{project_code}/assets/{asset_uid}"

    sensitivity_str = (
        payload.sensitivity_level.value
        if hasattr(payload.sensitivity_level, "value")
        else payload.sensitivity_level
    )
    asset_type_str = (
        payload.asset_type.value
        if hasattr(payload.asset_type, "value")
        else payload.asset_type
    )
    retention_str = (
        payload.retention_policy.value
        if hasattr(payload.retention_policy, "value")
        else payload.retention_policy
    )

    outbox_event = OutboxEvent(
        event_type="asset.created",
        aggregate_type=object_type,
        aggregate_id=asset_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or "",
        producer="mneme-api",
        payload_json={
            "project_id": str(payload.project_id) if payload.project_id else None,
            "asset_uid": asset_uid,
            "asset_type": asset_type_str,
            "title": payload.title,
            "sensitivity_level": sensitivity_str,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="asset.create",
        result="success",
        object_type=object_type,
        object_id=asset_id,
        project_id=payload.project_id,
        sensitivity_level=sensitivity_str,
    )

    def _do_insert(db: Session) -> AssetRead:
        row = db.execute(
            _INSERT_ASSET,
            {
                "asset_id": asset_id,
                "project_id": payload.project_id,
                "asset_uid": asset_uid,
                "title": payload.title,
                "asset_type": asset_type_str,
                "media_type": payload.media_type,
                "original_filename": payload.original_filename,
                "storage_backend": "mneme_data",
                "storage_ref": payload.storage_ref or "pending",
                "canonical_uri": canonical_uri,
                "content_hash": payload.content_hash,
                "size_bytes": payload.size_bytes,
                "status": "active",
                "ingest_state": "pending",
                "knowledge_state": "not_started",
                "current_version": 1,
                "sensitivity_level": sensitivity_str,
                "retention_policy": retention_str,
                "source_inbox_item_id": payload.source_inbox_item_id,
                "created_by_user_id": context.actor.actor_id,
                "imported_from": None,
                "imported_source_id": None,
                "metadata_json": json.dumps({}),
            },
        ).one()

        register_object(
            db,
            object_id=asset_id,
            object_type=object_type,
            project_id=payload.project_id,
            object_key=f"asset:{asset_uid}",
            owner_actor_type=context.actor.actor_type,
            owner_actor_id=context.actor.actor_id,
            sensitivity_level=sensitivity_str,
            canonical_uri=canonical_uri,
        )

        return _asset_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            version=1,
            action="create",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_idempotent_resolve,
        on_success=_post_audit,
    )


def get_asset(db: Session, asset_id: UUID) -> AssetRead | None:
    """Look up an asset by primary key."""
    row = db.execute(_SELECT_ASSET_BY_ID, {"asset_id": asset_id}).first()
    if row is None:
        return None
    return _asset_from_row(row)


def get_asset_by_uid(db: Session, project_id: UUID, asset_uid: str) -> AssetRead | None:
    """Look up an asset by its unique (project_id, asset_uid) pair."""
    row = db.execute(
        _SELECT_ASSET_BY_UID,
        {"project_id": project_id, "asset_uid": asset_uid},
    ).first()
    if row is None:
        return None
    return _asset_from_row(row)


def lookup_asset_by_hash(
    db: Session,
    *,
    content_hash: str,
    project_id: UUID | None = None,
) -> AssetRead | None:
    """Find an existing non-deleted asset with the same content hash."""
    row = db.execute(
        _LOOKUP_ASSET_BY_HASH,
        {"content_hash": content_hash, "project_id": project_id},
    ).first()
    if row is None:
        return None
    return _asset_from_row(row)


def list_assets(
    db: Session,
    *,
    project_id: UUID | None = None,
    asset_type: str | None = None,
    knowledge_state: str | None = None,
    sensitivity_level: str | None = None,
    status: str | None = None,
    ingest_state: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[AssetRead], int]:
    """List assets with optional filters and pagination."""
    params = {
        "project_id": project_id,
        "asset_type": asset_type,
        "knowledge_state": knowledge_state,
        "sensitivity_level": sensitivity_level,
        "status": status,
        "ingest_state": ingest_state,
    }
    total = db.execute(_LIST_ASSETS_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_ASSETS,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    items = [_asset_from_row(row) for row in rows]
    return items, total


def update_asset(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
    title: str | None = None,
    sensitivity_level: str | None = None,
    retention_policy: str | None = None,
) -> AssetRead:
    """Update mutable fields of an asset.

    Only non-None fields are updated. ``current_version`` is incremented.
    Sensitivity can only be raised, not lowered.
    """
    existing = get_asset(db, asset_id)
    if existing is None:
        raise ValueError(f"Asset {asset_id} not found")

    existing_sens = (
        existing.sensitivity_level.value
        if hasattr(existing.sensitivity_level, "value")
        else existing.sensitivity_level
    )

    if sensitivity_level is not None:
        sens_order = ["public", "normal", "private", "sensitive", "secret"]
        if sens_order.index(sensitivity_level) < sens_order.index(existing_sens):
            raise ValueError(
                f"Cannot lower sensitivity from '{existing_sens}' to '{sensitivity_level}'"
            )

    object_type = "asset"

    outbox_event = OutboxEvent(
        event_type="asset.updated",
        aggregate_type=object_type,
        aggregate_id=asset_id,
        aggregate_version=existing.current_version + 1,
        idempotency_key=f"{context.idempotency_key or ''}:update:{asset_id}",
        producer="mneme-api",
        payload_json={
            "asset_id": str(asset_id),
            "title": title,
            "sensitivity_level": sensitivity_level,
            "retention_policy": retention_policy,
        },
        visibility="internal",
        publish_state="pending",
    )

    diff: dict[str, Any] = {}
    if title is not None:
        diff["title"] = {"from": existing.title, "to": title}
    if sensitivity_level is not None:
        diff["sensitivity_level"] = {"from": existing_sens, "to": sensitivity_level}
    if retention_policy is not None:
        diff["retention_policy"] = {
            "from": (
                existing.retention_policy.value
                if hasattr(existing.retention_policy, "value")
                else existing.retention_policy
            ),
            "to": retention_policy,
        }

    audit_event = AuditEvent(
        action="asset.update",
        result="success",
        object_type=object_type,
        object_id=asset_id,
        project_id=existing.project_id,
        sensitivity_level=sensitivity_level or existing_sens,
        diff_summary=diff,
    )

    def _do_update(db: Session) -> AssetRead:
        row = db.execute(
            _UPDATE_ASSET,
            {
                "asset_id": asset_id,
                "title": title,
                "sensitivity_level": sensitivity_level,
                "retention_policy": retention_policy,
            },
        ).first()
        if row is None:
            raise ValueError(f"Asset {asset_id} not found during update")
        new_asset = _asset_from_row(row)

        bump_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            new_version=new_asset.current_version,
        )

        return new_asset

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            version=existing.current_version + 1,
            action="update",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
            previous_version=existing.current_version,
            diff_json=diff,
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> AssetRead:
        asset = get_asset(_db, asset_id)
        if asset is None:
            raise LookupError(f"asset {asset_id} not found")
        return asset

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


def change_asset_status(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
    new_status: str,
    expected_status: str | None = None,
) -> AssetRead:
    """Atomically transition an asset's ``status`` with state-machine validation.

    This is the canonical entry point for all status transitions
    (archive, delete, restore, quarantine, unquarantine).

    The ``expected_status`` parameter enables optimistic concurrency:
    the UPDATE only succeeds if the current status matches.  If omitted,
    the current status is read first and used as the expected value.

    Valid transitions (enforced by :func:`_can_transition_status`):

    * ``active → archived``  (soft-archive)
    * ``active → deleted``   (soft-delete / trash)
    * ``active → quarantined`` (admin hold)
    * ``archived → active``  (restore)
    * ``deleted → active``   (restore / undelete)
    * ``quarantined → active`` (release hold)
    """
    existing = get_asset(db, asset_id)
    if existing is None:
        raise ValueError(f"Asset {asset_id} not found")

    current_status = _resolve_status_value(existing.status)
    if expected_status is None:
        expected_status = current_status

    if current_status != expected_status:
        raise ValueError(
            f"Expected status '{expected_status}' but current is '{current_status}'"
        )

    if not _can_transition_status(current_status, new_status):
        raise ValueError(
            f"Invalid status transition: '{current_status}' -> '{new_status}'"
        )

    now_ts = datetime.now(timezone.utc)

    # Determine the audit/event action name
    if new_status == "deleted":
        action_name = "asset.delete"
    elif new_status == "archived":
        action_name = "asset.archive"
    elif new_status == "quarantined":
        action_name = "asset.quarantine"
    elif current_status == "deleted":
        action_name = "asset.restore"
    elif current_status == "archived":
        action_name = "asset.restore"
    elif current_status == "quarantined":
        action_name = "asset.unquarantine"
    else:
        action_name = "asset.status_change"

    object_type = "asset"

    outbox_event = OutboxEvent(
        event_type=action_name,
        aggregate_type=object_type,
        aggregate_id=asset_id,
        aggregate_version=existing.current_version + 1,
        idempotency_key=(
            f"{context.idempotency_key or ''}:status:"
            f"{new_status}:{asset_id}"
        ),
        producer="mneme-api",
        payload_json={
            "asset_id": str(asset_id),
            "previous_status": current_status,
            "new_status": new_status,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action=action_name,
        result="success",
        object_type=object_type,
        object_id=asset_id,
        project_id=existing.project_id,
        sensitivity_level=_resolve_status_value(existing.sensitivity_level),
        diff_summary={"status": {"from": current_status, "to": new_status}},
    )

    def _do_change(db: Session) -> AssetRead:
        row = db.execute(
            _UPDATE_ASSET_STATUS,
            {
                "asset_id": asset_id,
                "new_status": new_status,
                "expected_status": expected_status,
                "now_ts": now_ts,
            },
        ).first()
        if row is None:
            raise ValueError(
                f"Status change conflict for asset {asset_id}: "
                f"expected '{expected_status}', got '{new_status}'"
            )
        new_asset = _asset_from_row(row)

        bump_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            new_version=new_asset.current_version,
        )

        return new_asset

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            version=existing.current_version + 1,
            action="archive" if new_status == "archived" else (
                "delete" if new_status == "deleted" else
                "restore" if new_status == "active" else
                "quarantine" if new_status == "quarantined" else
                "status_change"
            ),
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
            previous_version=existing.current_version,
            diff_json={"status": {"from": current_status, "to": new_status}},
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> AssetRead:
        asset = get_asset(_db, asset_id)
        if asset is None:
            raise LookupError(f"asset {asset_id} not found")
        return asset

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_change,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


def archive_asset(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
    new_status: str = "deleted",
) -> AssetRead:
    """Soft-delete (or archive) an asset.

    Convenience wrapper around :func:`change_asset_status`.
    Does NOT delete physical files.

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry ``idempotency_key``).
        asset_id: UUID of the asset to archive.
        new_status: Target status — ``'deleted'`` (default) or ``'archived'``.

    Returns:
        Updated :class:`AssetRead`.

    Raises:
        ValueError: If the asset is not found or the status transition is invalid.
    """
    if new_status not in ("deleted", "archived"):
        raise ValueError(
            f"archive_asset expects 'deleted' or 'archived', got '{new_status}'"
        )
    return change_asset_status(
        db, context, asset_id=asset_id, new_status=new_status,
    )


def restore_asset(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
) -> AssetRead:
    """Restore a soft-deleted, archived, or quarantined asset back to ``'active'``.

    Convenience wrapper around :func:`change_asset_status`.

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry ``idempotency_key``).
        asset_id: UUID of the asset to restore.

    Returns:
        Updated :class:`AssetRead` with ``status='active'`` and ``archived_at=NULL``.

    Raises:
        ValueError: If the asset is not found or the status transition is invalid
                    (e.g. already active).
    """
    return change_asset_status(
        db, context, asset_id=asset_id, new_status="active",
    )


def promote_from_staging(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
    storage_ref: str,
    size_bytes: int | None = None,
) -> AssetRead:
    """Promote an asset from staging to permanent storage.

    Updates ``storage_ref`` to the formal path and advances
    ``ingest_state`` from ``'pending'`` to ``'staged'``.

    The ``'staged' → 'importing'`` transition is performed later
    by the pipeline consumer (:func:`advance_ingest_state`).
    """
    existing = get_asset(db, asset_id)
    if existing is None:
        raise ValueError(f"Asset {asset_id} not found")

    existing_ingest = (
        existing.ingest_state.value
        if hasattr(existing.ingest_state, "value")
        else existing.ingest_state
    )
    if not _can_transition_ingest(existing_ingest, "staged"):
        raise ValueError(
            f"Invalid ingest state transition: '{existing_ingest}' -> 'staged'"
        )

    object_type = "asset"

    outbox_event = OutboxEvent(
        event_type="asset.promoted",
        aggregate_type=object_type,
        aggregate_id=asset_id,
        aggregate_version=existing.current_version + 1,
        idempotency_key=f"{context.idempotency_key or ''}:promote:{asset_id}",
        producer="mneme-api",
        payload_json={
            "asset_id": str(asset_id),
            "storage_ref": storage_ref,
            "previous_ingest_state": existing_ingest,
            "new_ingest_state": "staged",
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="asset.promote",
        result="success",
        object_type=object_type,
        object_id=asset_id,
        project_id=existing.project_id,
        sensitivity_level=(
            existing.sensitivity_level.value
            if hasattr(existing.sensitivity_level, "value")
            else existing.sensitivity_level
        ),
        diff_summary={
            "storage_ref": {"from": existing.storage_ref, "to": storage_ref},
            "ingest_state": {"from": existing_ingest, "to": "staged"},
        },
    )

    def _do_promote(db: Session) -> AssetRead:
        row = db.execute(
            _PROMOTE_ASSET,
            {
                "asset_id": asset_id,
                "storage_ref": storage_ref,
                "ingest_state": "staged",
                "size_bytes": size_bytes,
                "expected_ingest_state": existing_ingest,
            },
        ).first()
        if row is None:
            raise ValueError(
                f"Promote conflict for asset {asset_id}"
            )
        new_asset = _asset_from_row(row)

        bump_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            new_version=new_asset.current_version,
        )

        return new_asset

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            version=existing.current_version + 1,
            action="update",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
            previous_version=existing.current_version,
            diff_json={
                "storage_ref": {"from": existing.storage_ref, "to": storage_ref},
                "ingest_state": {"from": existing_ingest, "to": "staged"},
            },
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> AssetRead:
        asset = get_asset(_db, asset_id)
        if asset is None:
            raise LookupError(f"asset {asset_id} not found")
        return asset

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_promote,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


def advance_ingest_state(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
    new_ingest_state: str,
    expected_ingest_state: str,
) -> AssetRead:
    """Advance the ingest state of an asset with validation."""
    existing = get_asset(db, asset_id)
    if existing is None:
        raise ValueError(f"Asset {asset_id} not found")

    existing_ingest = (
        existing.ingest_state.value
        if hasattr(existing.ingest_state, "value")
        else existing.ingest_state
    )

    if existing_ingest != expected_ingest_state:
        raise ValueError(
            f"Expected ingest_state '{expected_ingest_state}' "
            f"but current is '{existing_ingest}'"
        )

    if not _can_transition_ingest(existing_ingest, new_ingest_state):
        raise ValueError(
            f"Invalid ingest state transition: "
            f"'{existing_ingest}' -> '{new_ingest_state}'"
        )

    object_type = "asset"

    outbox_event = OutboxEvent(
        event_type=f"asset.ingest.{new_ingest_state}",
        aggregate_type=object_type,
        aggregate_id=asset_id,
        aggregate_version=existing.current_version + 1,
        idempotency_key=f"{context.idempotency_key or ''}:ingest:{asset_id}:{new_ingest_state}",
        producer="mneme-api",
        payload_json={
            "asset_id": str(asset_id),
            "previous_ingest_state": existing_ingest,
            "new_ingest_state": new_ingest_state,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action=f"asset.ingest.{new_ingest_state}",
        result="success",
        object_type=object_type,
        object_id=asset_id,
        project_id=existing.project_id,
        sensitivity_level=(
            existing.sensitivity_level.value
            if hasattr(existing.sensitivity_level, "value")
            else existing.sensitivity_level
        ),
        diff_summary={
            "ingest_state": {"from": existing_ingest, "to": new_ingest_state},
        },
    )

    def _do_advance(db: Session) -> AssetRead:
        row = db.execute(
            _UPDATE_INGEST_STATE,
            {
                "asset_id": asset_id,
                "new_ingest_state": new_ingest_state,
                "expected_ingest_state": expected_ingest_state,
            },
        ).first()
        if row is None:
            raise ValueError(f"Ingest state advance conflict for asset {asset_id}")
        new_asset = _asset_from_row(row)

        bump_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            new_version=new_asset.current_version,
        )

        return new_asset

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=asset_id,
            object_type=object_type,
            version=existing.current_version + 1,
            action="update",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
            previous_version=existing.current_version,
            diff_json={
                "ingest_state": {"from": existing_ingest, "to": new_ingest_state},
            },
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> AssetRead:
        asset = get_asset(_db, asset_id)
        if asset is None:
            raise LookupError(f"asset {asset_id} not found")
        return asset

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_advance,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


def set_knowledge_state(
    db: Session,
    *,
    asset_id: UUID,
    new_state: str,
) -> None:
    """Update the knowledge_state of an asset (lightweight, no audit/outbox)."""
    row = db.execute(
        _UPDATE_KNOWLEDGE_STATE,
        {"asset_id": asset_id, "new_state": new_state},
    ).first()
    if row is None:
        raise ValueError(f"Asset {asset_id} not found for knowledge_state update")


# ═══════════════════════════════════════════════════════════════════
# Public API — Asset Metadata
# ═══════════════════════════════════════════════════════════════════


def add_metadata(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
    payload: AssetMetadataCreateRequest,
) -> AssetMetadataRead:
    """Add or update an asset metadata entry (upsert).

    Uses atomic ``ON CONFLICT (asset_id, metadata_key, source) DO UPDATE``
    to handle the unique constraint correctly even under concurrent writes.

    Also rebuilds ``assets.metadata_json`` cache in the same transaction.
    """
    existing_asset = get_asset(db, asset_id)
    if existing_asset is None:
        raise ValueError(f"Asset {asset_id} not found")

    metadata_id = uuid4()
    source_str = payload.source or "manual"
    value_type_str = (
        payload.value_type.value
        if hasattr(payload.value_type, "value")
        else payload.value_type or "text"
    )

    # Validate value_type against the actual value
    _validate_metadata_value(
        payload.metadata_key, payload.metadata_value, value_type_str
    )

    # Derive a suffixed idempotency key so metadata writes don't collide
    # with the parent asset write that shares the same context.
    base_key = context.idempotency_key or ""
    meta_idem_key = (
        f"{base_key}:metadata:{asset_id}:{payload.metadata_key}:{source_str}"
        if base_key
        else ""
    )
    meta_ctx = RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        actor=context.actor,
        idempotency_key=meta_idem_key or None,
    )

    outbox_event = OutboxEvent(
        event_type="asset_metadata.upserted",
        aggregate_type="asset",
        aggregate_id=asset_id,
        aggregate_version=existing_asset.current_version,
        idempotency_key=meta_idem_key or "",
        producer="mneme-api",
        payload_json={
            "asset_id": str(asset_id),
            "metadata_key": payload.metadata_key,
            "source": source_str,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="asset_metadata.upsert",
        result="success",
        object_type="asset_metadata",
        object_id=metadata_id,
        project_id=existing_asset.project_id,
        sensitivity_level=(
            existing_asset.sensitivity_level.value
            if hasattr(existing_asset.sensitivity_level, "value")
            else existing_asset.sensitivity_level
        ),
    )

    def _do_upsert(db: Session) -> AssetMetadataRead:
        row = db.execute(
            _UPSERT_METADATA,
            {
                "asset_metadata_id": metadata_id,
                "asset_id": asset_id,
                "metadata_key": payload.metadata_key,
                "metadata_value": payload.metadata_value,
                "metadata_json": json.dumps(payload.metadata_json or {}),
                "value_type": value_type_str,
                "source": source_str,
                "confidence": payload.confidence,
            },
        ).first()

        if row is None:
            raise ValueError("Failed to upsert asset metadata")
        result = _metadata_from_row(row)

        # Rebuild the metadata_json cache on assets table
        _rebuild_metadata_cache(db, asset_id)
        return result

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> AssetMetadataRead:
        row = _db.execute(
            _SELECT_METADATA_BY_KEY,
            {
                "asset_id": asset_id,
                "metadata_key": payload.metadata_key,
                "source": source_str,
            },
        ).first()
        if row is not None:
            return _metadata_from_row(row)
        raise LookupError(
            f"Metadata key='{payload.metadata_key}' source='{source_str}' "
            f"not found for asset {asset_id}"
        )

    return write_with_audit_outbox_idempotency(
        db,
        meta_ctx,
        work=_do_upsert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


def get_metadata_by_id(
    db: Session,
    *,
    asset_metadata_id: UUID,
) -> AssetMetadataRead | None:
    """Look up a single metadata entry by its primary key."""
    row = db.execute(
        _SELECT_METADATA_BY_ID,
        {"asset_metadata_id": asset_metadata_id},
    ).first()
    if row is None:
        return None
    return _metadata_from_row(row)


def get_metadata_by_key(
    db: Session,
    *,
    asset_id: UUID,
    metadata_key: str,
    source: str = "manual",
) -> AssetMetadataRead | None:
    """Look up a metadata entry by (asset_id, metadata_key, source)."""
    row = db.execute(
        _SELECT_METADATA_BY_KEY,
        {"asset_id": asset_id, "metadata_key": metadata_key, "source": source},
    ).first()
    if row is None:
        return None
    return _metadata_from_row(row)


def list_metadata(
    db: Session,
    *,
    asset_id: UUID,
) -> list[AssetMetadataRead]:
    """List all metadata entries for an asset, ordered by key."""
    rows = db.execute(_LIST_METADATA, {"asset_id": asset_id}).all()
    return [_metadata_from_row(row) for row in rows]


def update_metadata(
    db: Session,
    context: RequestContext,
    *,
    asset_metadata_id: UUID,
    asset_id: UUID,
    payload: AssetMetadataUpdateRequest,
) -> AssetMetadataRead:
    """Partially update a metadata entry.

    Only non-None fields are updated. If ``value_type`` is changed, the
    existing ``metadata_value`` is re-validated against the new type.
    The ``assets.metadata_json`` cache is rebuilt after the update.
    """
    existing = get_metadata_by_id(db, asset_metadata_id=asset_metadata_id)
    if existing is None:
        raise ValueError(f"Asset metadata {asset_metadata_id} not found")

    if existing.asset_id != asset_id:
        raise ValueError(
            f"Metadata {asset_metadata_id} does not belong to asset {asset_id}"
        )

    existing_asset = get_asset(db, asset_id)
    if existing_asset is None:
        raise ValueError(f"Asset {asset_id} not found")

    new_value = (
        payload.metadata_value
        if payload.metadata_value is not None
        else existing.metadata_value
    )
    new_value_type = (
        payload.value_type.value
        if payload.value_type and hasattr(payload.value_type, "value")
        else (
            payload.value_type
            if payload.value_type
            else existing.value_type
        )
    )
    new_confidence = (
        payload.confidence
        if payload.confidence is not None
        else existing.confidence
    )
    new_metadata_json = (
        payload.metadata_json
        if payload.metadata_json is not None
        else {}
    )

    # Validate value against the (possibly new) value_type
    _validate_metadata_value(existing.metadata_key, new_value, new_value_type)

    # Derive a suffixed idempotency key to avoid collision with parent asset writes.
    base_key = context.idempotency_key or ""
    meta_idem_key = (
        f"{base_key}:metadata-update:{asset_metadata_id}"
        if base_key
        else ""
    )
    meta_ctx = RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        actor=context.actor,
        idempotency_key=meta_idem_key or None,
    )

    object_type = "asset_metadata"

    outbox_event = OutboxEvent(
        event_type="asset_metadata.updated",
        aggregate_type="asset",
        aggregate_id=asset_id,
        aggregate_version=existing_asset.current_version,
        idempotency_key=meta_idem_key or "",
        producer="mneme-api",
        payload_json={
            "asset_metadata_id": str(asset_metadata_id),
            "asset_id": str(asset_id),
            "metadata_key": existing.metadata_key,
        },
        visibility="internal",
        publish_state="pending",
    )

    diff: dict[str, Any] = {}
    if payload.metadata_value is not None:
        diff["metadata_value"] = {
            "from": existing.metadata_value,
            "to": payload.metadata_value,
        }
    if payload.value_type is not None:
        diff["value_type"] = {
            "from": existing.value_type,
            "to": new_value_type,
        }
    if payload.confidence is not None:
        diff["confidence"] = {
            "from": existing.confidence,
            "to": payload.confidence,
        }

    audit_event = AuditEvent(
        action="asset_metadata.update",
        result="success",
        object_type=object_type,
        object_id=asset_metadata_id,
        project_id=existing_asset.project_id,
        sensitivity_level=(
            existing_asset.sensitivity_level.value
            if hasattr(existing_asset.sensitivity_level, "value")
            else existing_asset.sensitivity_level
        ),
        diff_summary=diff,
    )

    def _do_update(db: Session) -> AssetMetadataRead:
        row = db.execute(
            _UPSERT_METADATA,
            {
                "asset_metadata_id": asset_metadata_id,
                "asset_id": asset_id,
                "metadata_key": existing.metadata_key,
                "metadata_value": new_value,
                "metadata_json": json.dumps(new_metadata_json),
                "value_type": new_value_type,
                "source": existing.source,
                "confidence": new_confidence,
            },
        ).first()

        if row is None:
            raise ValueError(
                f"Failed to update metadata {asset_metadata_id}"
            )

        result = _metadata_from_row(row)
        _rebuild_metadata_cache(db, asset_id)
        return result

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> AssetMetadataRead:
        meta = get_metadata_by_id(_db, asset_metadata_id=asset_metadata_id)
        if meta is None:
            raise LookupError(
                f"Metadata {asset_metadata_id} not found"
            )
        return meta

    return write_with_audit_outbox_idempotency(
        db,
        meta_ctx,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


def delete_metadata(
    db: Session,
    context: RequestContext,
    *,
    asset_metadata_id: UUID,
    asset_id: UUID,
) -> AssetMetadataRead:
    """Delete a metadata entry.

    The deleted row is returned for confirmation.  The
    ``assets.metadata_json`` cache is rebuilt after deletion.
    """
    existing = get_metadata_by_id(db, asset_metadata_id=asset_metadata_id)
    if existing is None:
        raise ValueError(f"Asset metadata {asset_metadata_id} not found")

    if existing.asset_id != asset_id:
        raise ValueError(
            f"Metadata {asset_metadata_id} does not belong to asset {asset_id}"
        )

    existing_asset = get_asset(db, asset_id)
    if existing_asset is None:
        raise ValueError(f"Asset {asset_id} not found")

    # Derive a suffixed idempotency key to avoid collision with parent asset writes.
    base_key = context.idempotency_key or ""
    meta_idem_key = (
        f"{base_key}:metadata-delete:{asset_metadata_id}"
        if base_key
        else ""
    )
    meta_ctx = RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        actor=context.actor,
        idempotency_key=meta_idem_key or None,
    )

    object_type = "asset_metadata"

    outbox_event = OutboxEvent(
        event_type="asset_metadata.deleted",
        aggregate_type="asset",
        aggregate_id=asset_id,
        aggregate_version=existing_asset.current_version,
        idempotency_key=meta_idem_key or "",
        producer="mneme-api",
        payload_json={
            "asset_metadata_id": str(asset_metadata_id),
            "asset_id": str(asset_id),
            "metadata_key": existing.metadata_key,
            "metadata_value": existing.metadata_value,
            "source": existing.source,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="asset_metadata.delete",
        result="success",
        object_type=object_type,
        object_id=asset_metadata_id,
        project_id=existing_asset.project_id,
        sensitivity_level=(
            existing_asset.sensitivity_level.value
            if hasattr(existing_asset.sensitivity_level, "value")
            else existing_asset.sensitivity_level
        ),
        diff_summary={
            "metadata_key": existing.metadata_key,
            "metadata_value": existing.metadata_value,
            "source": existing.source,
        },
    )

    def _do_delete(db: Session) -> AssetMetadataRead:
        row = db.execute(
            _DELETE_METADATA,
            {
                "asset_metadata_id": asset_metadata_id,
                "asset_id": asset_id,
            },
        ).first()

        if row is None:
            raise ValueError(
                f"Failed to delete metadata {asset_metadata_id} "
                f"(may have been already deleted or asset mismatch)"
            )

        result = _metadata_from_row(row)
        _rebuild_metadata_cache(db, asset_id)
        return result

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> AssetMetadataRead:
        meta = get_metadata_by_id(_db, asset_metadata_id=asset_metadata_id)
        if meta is None:
            # Already deleted — return the pre-delete snapshot we have
            raise LookupError(
                f"Metadata {asset_metadata_id} already deleted"
            )
        return meta

    return write_with_audit_outbox_idempotency(
        db,
        meta_ctx,
        work=_do_delete,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════
# Public API — Ingest (full upload → asset flow)
# ═══════════════════════════════════════════════════════════════════


def ingest_asset(
    db: Session,
    context: RequestContext,
    *,
    staged_info: "StagedFileInfo",
    project_id: UUID | None = None,
    project_code: str | None = None,
    title: str | None = None,
    asset_type: str = "document",
    sensitivity_level: str = "normal",
    retention_policy: str = "default",
    source: str = "api",
    source_uri: str | None = None,
    source_ref: str | None = None,
) -> "AssetRead":
    """Full ingest flow: create inbox + asset + promote file + link.

    This is the recommended single-call entry point for Asset 入库.
    It orchestrates the complete pipeline:

    1. Check for duplicate asset by content_hash (asset-level dedup).
       If an existing non-deleted asset with the same hash is found,
       a :class:`DuplicateAssetError` is raised so the API layer can
       return the existing asset.
    2. Create an inbox item (status='staged') via
       :func:`mneme.db.inbox.create_inbox_from_staging`.
    3. Create the asset record (ingest_state='pending').
    4. Promote the file from staging to permanent storage via
       :func:`mneme.storage.promote.promote_file`.
    5. Update the asset's ``storage_ref`` and advance
       ``ingest_state`` to ``'staged'`` via
       :func:`promote_from_staging`.
    6. Link the inbox item to the asset (``staged → linked``) via
       :func:`mneme.db.inbox.link_inbox_to_asset`.

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry idempotency_key).
        staged_info: Metadata from the staging operation.
        project_id: Target project UUID.
        project_code: Project code (auto-resolved if ``None``).
        title: Asset title (defaults to original filename).
        asset_type: Asset type string (default ``"document"``).
        sensitivity_level: Sensitivity level (default ``"normal"``).
        retention_policy: Retention policy (default ``"default"``).
        source: Source identifier for the inbox item (default ``"api"``).
        source_uri: Optional source URI.
        source_ref: Optional external reference.

    Returns:
        The newly created (and promoted) :class:`AssetRead`.

    Raises:
        DuplicateAssetError: If an asset with the same content_hash
            already exists in the project.
        PromoteError: If file promotion fails.
        ValueError: If any step fails validation.
    """
    from mneme.db.inbox import (
        create_inbox_from_staging,
        link_inbox_to_asset,
    )
    from mneme.storage.promote import promote_file, rollback_promote

    base_key = context.idempotency_key or ""

    # 1. Check for duplicate at asset level
    existing = lookup_asset_by_hash(
        db,
        content_hash=staged_info.content_hash,
        project_id=project_id,
    )
    if existing is not None:
        raise DuplicateAssetError(existing)

    # Resolve project_code if not provided
    if project_code is None:
        project_code = _lookup_project_code(db, project_id) if project_id else "default"

    # 2. Create inbox item from staging (uses suffixed idempotency key)
    inbox_ctx = RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        actor=context.actor,
        idempotency_key=f"{base_key}:inbox" if base_key else None,
    )
    inbox_item = create_inbox_from_staging(
        db,
        inbox_ctx,
        project_id=project_id,
        staged_info=staged_info,
        title=title or staged_info.original_filename,
        source=source,
        source_uri=source_uri or f"file://{staged_info.staging_path}",
        source_ref=source_ref,
    )

    # 3. Create asset record (uses suffixed idempotency key)
    asset_payload = AssetCreateRequest(
        project_id=project_id,
        title=title or staged_info.original_filename,
        asset_type=asset_type,
        media_type=staged_info.media_type,
        original_filename=staged_info.original_filename,
        content_hash=staged_info.content_hash,
        size_bytes=staged_info.size_bytes,
        sensitivity_level=sensitivity_level,
        retention_policy=retention_policy,
        source_inbox_item_id=inbox_item.inbox_item_id,
    )
    asset_ctx = RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        actor=context.actor,
        idempotency_key=f"{base_key}:asset" if base_key else None,
    )
    asset = create_asset(db, asset_ctx, payload=asset_payload, project_code=project_code)

    # 4. Promote file from staging to permanent storage (filesystem)
    storage_ref = None
    try:
        storage_ref = promote_file(
            staging_path=staged_info.staging_path,
            project_id=project_id,
            asset_uid=asset.asset_uid,
            original_filename=staged_info.original_filename,
        )

        # 5. Update asset storage_ref and ingest_state (DB)
        asset = promote_from_staging(
            db,
            context,
            asset_id=asset.asset_id,
            storage_ref=storage_ref,
            size_bytes=staged_info.size_bytes,
        )

        # 6. Link inbox item to asset
        link_inbox_to_asset(
            db,
            context,
            inbox_item_id=inbox_item.inbox_item_id,
            asset_id=asset.asset_id,
        )

    except Exception:
        # Rollback filesystem promote on any failure
        if storage_ref is not None:
            rollback_promote(storage_ref)
        raise

    return asset


class DuplicateAssetError(Exception):
    """Raised by :func:`ingest_asset` when a content-hash duplicate exists.

    The ``existing_asset`` attribute carries the full :class:`AssetRead`
    of the already-ingested asset so the API layer can return it directly.
    """

    def __init__(self, existing_asset: "AssetRead") -> None:
        super().__init__(
            f"Asset with content_hash '{existing_asset.content_hash}' "
            f"already exists as {existing_asset.asset_uid}"
        )
        self.existing_asset = existing_asset
