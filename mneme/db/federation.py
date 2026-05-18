"""Federation node registry + sync queue DAL (L7-02).

Provides CRUD for ``federation_nodes`` and ``sync_queue`` tables.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.schemas.events import (
    FederationNodeCreate,
    FederationNodeRead,
    FederationNodeUpdate,
    SyncDirection,
    SyncQueueEntryRead,
    SyncQueueStatus,
)

import logging
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Federation Nodes
# ═══════════════════════════════════════════════════════════════════════

_INSERT_NODE = text(r"""
    INSERT INTO federation_nodes (
        node_id, node_code, display_name, instance_url,
        public_key, api_version, node_status, sync_role,
        config_json
    )
    VALUES (
        :node_id, :node_code, :display_name, :instance_url,
        :public_key, :api_version, :node_status, :sync_role,
        :config_json
    )
    RETURNING
        node_id, node_code, display_name, instance_url,
        public_key, api_version, node_status, sync_role,
        heartbeat_at, last_sync_at, config_json,
        created_at, updated_at
""").bindparams(
    bindparam("node_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_NODE_BY_ID = text(r"""
    SELECT * FROM federation_nodes WHERE node_id = :node_id
""")

_SELECT_NODE_BY_CODE = text(r"""
    SELECT * FROM federation_nodes WHERE node_code = :node_code
""")

_LIST_NODES = text(r"""
    SELECT * FROM federation_nodes
    WHERE (:status IS NULL OR node_status = :status)
    ORDER BY display_name ASC
""")

_UPDATE_NODE = text(r"""
    UPDATE federation_nodes SET
        display_name  = COALESCE(:display_name, display_name),
        instance_url  = COALESCE(:instance_url, instance_url),
        public_key    = COALESCE(:public_key, public_key),
        api_version   = COALESCE(:api_version, api_version),
        node_status   = COALESCE(:node_status, node_status),
        sync_role     = COALESCE(:sync_role, sync_role),
        config_json   = CASE WHEN :config_json IS NOT NULL
                          THEN CAST(:config_json AS jsonb)
                          ELSE config_json END,
        heartbeat_at  = CASE WHEN :heartbeat THEN now() ELSE heartbeat_at END,
        last_sync_at  = CASE WHEN :sync_now THEN now() ELSE last_sync_at END,
        updated_at    = now()
    WHERE node_id = :node_id
    RETURNING
        node_id, node_code, display_name, instance_url,
        public_key, api_version, node_status, sync_role,
        heartbeat_at, last_sync_at, config_json,
        created_at, updated_at
""").bindparams(
    bindparam("node_id", type_=PG_UUID(as_uuid=True)),
)

_DELETE_NODE = text(r"""
    DELETE FROM federation_nodes WHERE node_id = :node_id
    RETURNING node_id
""")


def _node_from_row(row: Any) -> FederationNodeRead:
    data = dict(row._mapping)
    if isinstance(data.get("config_json"), str):
        data["config_json"] = json.loads(data["config_json"])
    elif data.get("config_json") is None:
        data["config_json"] = {}
    return FederationNodeRead.model_validate(data)


def create_federation_node(
    db: Session,
    *,
    payload: FederationNodeCreate,
) -> FederationNodeRead:
    """Register a new federation peer."""
    node_id = uuid4()
    row = db.execute(
        _INSERT_NODE,
        {
            "node_id": node_id,
            "node_code": payload.node_code,
            "display_name": payload.display_name,
            "instance_url": payload.instance_url,
            "public_key": payload.public_key,
            "api_version": payload.api_version,
            "node_status": "active",
            "sync_role": payload.sync_role.value,
            "config_json": json.dumps(payload.config_json),
        },
    ).one()
    return _node_from_row(row)


def get_federation_node(
    db: Session,
    *,
    node_id: UUID | None = None,
    node_code: str | None = None,
) -> FederationNodeRead | None:
    """Look up a federation node by ID or code."""
    if node_id:
        row = db.execute(_SELECT_NODE_BY_ID, {"node_id": node_id}).first()
    elif node_code:
        row = db.execute(_SELECT_NODE_BY_CODE, {"node_code": node_code}).first()
    else:
        return None
    if row is None:
        return None
    return _node_from_row(row)


def list_federation_nodes(
    db: Session,
    *,
    status: str | None = None,
) -> list[FederationNodeRead]:
    """List all registered federation nodes."""
    rows = db.execute(_LIST_NODES, {"status": status}).all()
    return [_node_from_row(r) for r in rows]


def update_federation_node(
    db: Session,
    *,
    node_id: UUID,
    payload: FederationNodeUpdate,
    heartbeat: bool = False,
    sync_now: bool = False,
) -> FederationNodeRead | None:
    """Update a federation node's attributes."""
    row = db.execute(
        _UPDATE_NODE,
        {
            "node_id": node_id,
            "display_name": payload.display_name,
            "instance_url": payload.instance_url,
            "public_key": payload.public_key,
            "api_version": payload.api_version,
            "node_status": payload.node_status.value if payload.node_status else None,
            "sync_role": payload.sync_role.value if payload.sync_role else None,
            "config_json": json.dumps(payload.config_json) if payload.config_json is not None else None,
            "heartbeat": heartbeat,
            "sync_now": sync_now,
        },
    ).first()
    if row is None:
        return None
    return _node_from_row(row)


def delete_federation_node(
    db: Session,
    *,
    node_id: UUID,
) -> bool:
    """Remove a federation node registration."""
    row = db.execute(_DELETE_NODE, {"node_id": node_id}).first()
    return row is not None


# ═══════════════════════════════════════════════════════════════════════
# Sync Queue
# ═══════════════════════════════════════════════════════════════════════

_ENQUEUE_SYNC = text(r"""
    INSERT INTO sync_queue (
        sync_queue_id, direction, node_id,
        stream_type, stream_id, stream_version,
        event_type, payload_json,
        sync_status, enqueued_at
    )
    VALUES (
        :sync_queue_id, :direction, :node_id,
        :stream_type, :stream_id, :stream_version,
        :event_type, :payload_json,
        :sync_status, :enqueued_at
    )
    RETURNING
        sync_queue_id, direction, node_id,
        stream_type, stream_id, stream_version,
        event_type, payload_json,
        sync_status, attempt_count, last_error,
        locked_until, enqueued_at, synced_at,
        created_at, updated_at
""").bindparams(
    bindparam("sync_queue_id", type_=PG_UUID(as_uuid=True)),
    bindparam("node_id", type_=PG_UUID(as_uuid=True)),
    bindparam("stream_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_PENDING_OUTBOUND = text(r"""
    SELECT *
    FROM sync_queue
    WHERE direction = 'outbound'
      AND sync_status = 'pending'
      AND (locked_until IS NULL OR locked_until < now())
    ORDER BY enqueued_at ASC
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
""")

_SELECT_PENDING_INBOUND = text(r"""
    SELECT *
    FROM sync_queue
    WHERE direction = 'inbound'
      AND sync_status = 'pending'
      AND (locked_until IS NULL OR locked_until < now())
    ORDER BY enqueued_at ASC
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
""")

_UPDATE_SYNC_STATUS = text(r"""
    UPDATE sync_queue SET
        sync_status   = :sync_status,
        attempt_count = attempt_count + 1,
        last_error    = :last_error,
        locked_until  = :locked_until,
        synced_at     = CASE WHEN :sync_status IN ('confirmed', 'skipped', 'cancelled')
                            THEN now() ELSE synced_at END,
        updated_at    = now()
    WHERE sync_queue_id = :sync_queue_id
    RETURNING *
""").bindparams(
    bindparam("sync_queue_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_SYNC_QUEUE = text(r"""
    SELECT *
    FROM sync_queue
    WHERE (:direction IS NULL OR direction = :direction)
      AND (:status IS NULL OR sync_status = :status)
      AND (:node_id IS NULL OR node_id = :node_id)
    ORDER BY enqueued_at DESC
    LIMIT :limit OFFSET :offset
""")

_COUNT_SYNC_QUEUE = text(r"""
    SELECT COUNT(*) FROM sync_queue
    WHERE (:direction IS NULL OR direction = :direction)
      AND (:status IS NULL OR sync_status = :status)
      AND (:node_id IS NULL OR node_id = :node_id)
""")


def _sync_entry_from_row(row: Any) -> SyncQueueEntryRead:
    data = dict(row._mapping)
    if isinstance(data.get("payload_json"), str):
        data["payload_json"] = json.loads(data["payload_json"])
    elif data.get("payload_json") is None:
        data["payload_json"] = {}
    return SyncQueueEntryRead.model_validate(data)


def enqueue_outbound_sync(
    db: Session,
    *,
    node_id: UUID,
    stream_type: str,
    stream_id: UUID,
    stream_version: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> SyncQueueEntryRead:
    """Enqueue an event for outbound sync to a peer node."""
    entry_id = uuid4()
    row = db.execute(
        _ENQUEUE_SYNC,
        {
            "sync_queue_id": entry_id,
            "direction": "outbound",
            "node_id": node_id,
            "stream_type": stream_type,
            "stream_id": stream_id,
            "stream_version": stream_version,
            "event_type": event_type,
            "payload_json": json.dumps(payload or {}),
            "sync_status": "pending",
            "enqueued_at": datetime.now(timezone.utc),
        },
    ).one()
    return _sync_entry_from_row(row)


def enqueue_inbound_sync(
    db: Session,
    *,
    node_id: UUID,
    stream_type: str,
    stream_id: UUID,
    stream_version: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> SyncQueueEntryRead:
    """Enqueue a received inbound event for processing."""
    entry_id = uuid4()
    row = db.execute(
        _ENQUEUE_SYNC,
        {
            "sync_queue_id": entry_id,
            "direction": "inbound",
            "node_id": node_id,
            "stream_type": stream_type,
            "stream_id": stream_id,
            "stream_version": stream_version,
            "event_type": event_type,
            "payload_json": json.dumps(payload or {}),
            "sync_status": "pending",
            "enqueued_at": datetime.now(timezone.utc),
        },
    ).one()
    return _sync_entry_from_row(row)


def claim_pending_outbound(
    db: Session,
    *,
    batch_size: int = 50,
) -> list[SyncQueueEntryRead]:
    """Claim pending outbound sync entries (with SKIP LOCKED)."""
    rows = db.execute(
        _SELECT_PENDING_OUTBOUND,
        {"limit": batch_size},
    ).all()
    # Mark as syncing
    now = datetime.now(timezone.utc)
    entries: list[SyncQueueEntryRead] = []
    for row in rows:
        db.execute(
            _UPDATE_SYNC_STATUS,
            {
                "sync_queue_id": row.sync_queue_id,
                "sync_status": "syncing",
                "last_error": None,
                "locked_until": None,  # cleared; status guards re-claim
            },
        )
        entries.append(_sync_entry_from_row(row))
    return entries


def claim_pending_inbound(
    db: Session,
    *,
    batch_size: int = 50,
) -> list[SyncQueueEntryRead]:
    """Claim pending inbound sync entries (with SKIP LOCKED)."""
    rows = db.execute(
        _SELECT_PENDING_INBOUND,
        {"limit": batch_size},
    ).all()
    entries: list[SyncQueueEntryRead] = []
    for row in rows:
        db.execute(
            _UPDATE_SYNC_STATUS,
            {
                "sync_queue_id": row.sync_queue_id,
                "sync_status": "syncing",
                "last_error": None,
                "locked_until": None,
            },
        )
        entries.append(_sync_entry_from_row(row))
    return entries


def update_sync_status(
    db: Session,
    *,
    sync_queue_id: UUID,
    status: SyncQueueStatus,
    error: str | None = None,
    lock_seconds: int | None = None,
) -> SyncQueueEntryRead | None:
    """Update the status of a sync queue entry."""
    locked_until = None
    if lock_seconds and status in (SyncQueueStatus.pending, SyncQueueStatus.syncing):
        locked_until = datetime.now(timezone.utc).timestamp() + lock_seconds

    row = db.execute(
        _UPDATE_SYNC_STATUS,
        {
            "sync_queue_id": sync_queue_id,
            "sync_status": status.value,
            "last_error": error,
            "locked_until": locked_until,
        },
    ).first()
    if row is None:
        return None
    return _sync_entry_from_row(row)


def list_sync_queue(
    db: Session,
    *,
    direction: str | None = None,
    status: str | None = None,
    node_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[SyncQueueEntryRead], int]:
    """List sync queue entries with filters."""
    offset = (max(page, 1) - 1) * page_size
    params: dict[str, Any] = {
        "direction": direction,
        "status": status,
        "node_id": node_id,
        "limit": page_size,
        "offset": offset,
    }
    total = db.execute(_COUNT_SYNC_QUEUE, params).scalar_one()
    rows = db.execute(_LIST_SYNC_QUEUE, params).all()
    return [_sync_entry_from_row(r) for r in rows], total or 0
