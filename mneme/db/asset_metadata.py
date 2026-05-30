"""Asset Metadata DAL — CRUD operations for the ``asset_metadata`` table.

Every mutation that creates, modifies, or deletes an ``asset_metadata``
row is wrapped in :func:`mneme.db.audit.write_with_audit_outbox_idempotency`
so that:

* An ``asset_metadata`` row is written.
* An ``audit_events`` row is written.
* An ``events`` (outbox) row is written.
* The ``assets.metadata_json`` cache is rebuilt after every mutation.
* All land in the same database transaction.

Value-type validation
---------------------
Mirrors the ``CHECK (value_type IN ('text','number','boolean','date','json'))``
constraint on the table.  Any value that fails validation raises ``ValueError``.

Upsert semantics
----------------
``add_metadata`` uses an atomic ``ON CONFLICT ... DO UPDATE`` so that
concurrent writes to the same ``(asset_id, metadata_key, source)`` are
serialised correctly.
"""

from __future__ import annotations

import json
import re
from datetime import datetime as _dt
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.assets import get_asset
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.schemas.asset_metadata import (
    AssetMetadataCreateRequest,
    AssetMetadataRead,
    AssetMetadataUpdateRequest,
)


# ═══════════════════════════════════════════════════════════════════
# SQL — Asset Metadata
# ═══════════════════════════════════════════════════════════════════

_INSERT_METADATA = text(
    """
    INSERT INTO asset_metadata (
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, metadata_json, value_type, source, confidence
    )
    VALUES (
      :asset_metadata_id, :asset_id, :metadata_key,
      :metadata_value, :metadata_json, :value_type, :source, :confidence
    )
    RETURNING
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, value_type, source, confidence,
      created_at, updated_at
    """
).bindparams(
    bindparam("asset_metadata_id", type_=PG_UUID(as_uuid=True)),
    bindparam("asset_id", type_=PG_UUID(as_uuid=True)),
)

_UPDATE_EXISTING_METADATA = text(
    """
    UPDATE asset_metadata
    SET metadata_value = :metadata_value,
        metadata_json = :metadata_json,
        confidence = :confidence,
        updated_at = now()
    WHERE asset_id = :asset_id
      AND metadata_key = :metadata_key
      AND source = :source
    RETURNING
      asset_metadata_id, asset_id, metadata_key,
      metadata_value, value_type, source, confidence,
      created_at, updated_at
    """
).bindparams(
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

# Atomic upsert: try insert, on conflict update
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


# ═══════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════


def _metadata_from_row(row: Any) -> AssetMetadataRead:
    """Map a SQLAlchemy result row to an ``AssetMetadataRead`` schema."""
    data = dict(row._mapping)
    return AssetMetadataRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════
# Value-type validation
# ═══════════════════════════════════════════════════════════════════

_VALID_VALUE_TYPES = {"text", "number", "boolean", "date", "json"}

# ISO 8601 date: YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Boolean accepted values
_BOOL_TRUE = {"true", "1", "yes"}
_BOOL_FALSE = {"false", "0", "no"}


def validate_metadata_value(
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


# ═══════════════════════════════════════════════════════════════════
# Metadata cache rebuild
# ═══════════════════════════════════════════════════════════════════


def rebuild_metadata_cache(db: Session, asset_id: UUID) -> None:
    """Rebuild ``assets.metadata_json`` from current ``asset_metadata`` rows.

    Called automatically after every metadata mutation (create, update, delete)
    to keep the denormalized JSON cache on the ``assets`` table in sync.
    """
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


# ═══════════════════════════════════════════════════════════════════
# Public API — Asset Metadata CRUD
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

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry ``idempotency_key``).
        asset_id: UUID of the parent asset.
        payload: Metadata key, value, type, source, and confidence.

    Returns:
        The created or updated :class:`AssetMetadataRead`.

    Raises:
        ValueError: If the asset does not exist or value_type validation fails.
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
    validate_metadata_value(
        payload.metadata_key, payload.metadata_value, value_type_str
    )

    # Derive a suffixed idempotency key so metadata writes don't collide
    # with the parent asset write that shares the same context.
    base_key = context.idempotency_key or f"auto:{uuid4().hex}"
    meta_idem_key = f"{base_key}:metadata:{asset_id}:{payload.metadata_key}:{source_str}"
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
        idempotency_key=meta_idem_key or str(uuid4()),
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
        rebuild_metadata_cache(db, asset_id)
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
    """Look up a single metadata entry by its primary key.

    Args:
        db: Active SQLAlchemy session.
        asset_metadata_id: UUID of the metadata row.

    Returns:
        :class:`AssetMetadataRead` or ``None`` if not found.
    """
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
    """Look up a metadata entry by (asset_id, metadata_key, source).

    Args:
        db: Active SQLAlchemy session.
        asset_id: UUID of the parent asset.
        metadata_key: Key to look up.
        source: Source namespace (default ``"manual"``).

    Returns:
        :class:`AssetMetadataRead` or ``None`` if not found.
    """
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
    """List all metadata entries for an asset, ordered by key.

    Args:
        db: Active SQLAlchemy session.
        asset_id: UUID of the parent asset.

    Returns:
        List of :class:`AssetMetadataRead` (may be empty).
    """
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

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry ``idempotency_key``).
        asset_metadata_id: UUID of the metadata row to update.
        asset_id: UUID of the parent asset (for ownership verification).
        payload: Fields to update (only non-None fields are applied).

    Returns:
        The updated :class:`AssetMetadataRead`.

    Raises:
        ValueError: If the metadata entry or asset is not found, or if the
            metadata entry does not belong to the given asset.
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
    validate_metadata_value(existing.metadata_key, new_value, new_value_type)

    # Derive a suffixed idempotency key to avoid collision with parent asset writes.
    base_key = context.idempotency_key or str(uuid4())
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
        idempotency_key=meta_idem_key or str(uuid4()),
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
        rebuild_metadata_cache(db, asset_id)
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

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry ``idempotency_key``).
        asset_metadata_id: UUID of the metadata row to delete.
        asset_id: UUID of the parent asset (for ownership verification).

    Returns:
        The deleted :class:`AssetMetadataRead`.

    Raises:
        ValueError: If the metadata entry or asset is not found, or if the
            metadata entry does not belong to the given asset.
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
    base_key = context.idempotency_key or str(uuid4())
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
        idempotency_key=meta_idem_key or str(uuid4()),
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
        rebuild_metadata_cache(db, asset_id)
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
