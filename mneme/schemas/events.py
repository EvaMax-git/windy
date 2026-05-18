from __future__ import annotations

import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


class EventVisibility(str, Enum):
    internal = "internal"
    external = "external"
    audit_only = "audit_only"


class EventPublishState(str, Enum):
    pending = "pending"
    dispatched = "dispatched"
    delivered = "delivered"
    failed = "failed"
    dead_letter = "dead_letter"


class EventRead(ApiSchema):
    event_id: UUID
    event_type: str = Field(min_length=1, max_length=120)
    aggregate_type: str = Field(min_length=1, max_length=80)
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    correlation_id: UUID
    causation_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)
    producer: str = Field(min_length=1, max_length=80)
    payload_json: dict[str, Any] = Field(default_factory=dict)
    visibility: EventVisibility
    publish_state: EventPublishState
    occurred_at: datetime
    committed_at: datetime
    published_at: datetime | None = None
    last_error: str | None = None


class EventCreate(ApiSchema):
    event_type: str = Field(min_length=1, max_length=120)
    aggregate_type: str = Field(min_length=1, max_length=80)
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    producer: str = Field(default="mneme-api", min_length=1, max_length=80)
    payload_json: dict[str, Any] = Field(default_factory=dict)
    visibility: EventVisibility = EventVisibility.internal
    causation_id: UUID | None = None
    occurred_at: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Event Log (L7-01) — append-only domain event store
# ═══════════════════════════════════════════════════════════════════════════

class EventLogStreamType(str, Enum):
    memory = "memory"
    conversation = "conversation"
    message = "message"
    knowledge_document = "knowledge_document"
    knowledge_chunk = "knowledge_chunk"
    asset = "asset"
    agent = "agent"
    project = "project"


class EventLogEntryRead(ApiSchema):
    log_id: UUID
    stream_type: EventLogStreamType
    stream_id: UUID
    stream_version: int = Field(ge=0)
    event_type: str = Field(min_length=1, max_length=120)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    actor_type: str | None = None
    actor_id: UUID | None = None
    payload_json: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    committed_at: datetime
    project_id: UUID | None = None


class EventLogFilterParams(ApiSchema):
    project_id: UUID | None = None
    stream_type: str | None = None
    event_type: str | None = None
    since: datetime | None = None
    until: datetime | None = None


class EventLogListResponse(ApiSchema):
    items: list[EventLogEntryRead]
    total: int
    page: int
    page_size: int


# ═══════════════════════════════════════════════════════════════════════════
# Sync / Federation (L7-02) — pre-wired protocol types
# ═══════════════════════════════════════════════════════════════════════════

class FederationNodeStatus(str, Enum):
    active = "active"
    paused = "paused"
    inactive = "inactive"
    revoked = "revoked"


class FederationSyncRole(str, Enum):
    leader = "leader"
    peer = "peer"
    readonly = "readonly"


class FederationNodeRead(ApiSchema):
    node_id: UUID
    node_code: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    instance_url: str = Field(min_length=1, max_length=512)
    public_key: str | None = None
    api_version: str = Field(default="1.0", max_length=24)
    node_status: FederationNodeStatus = FederationNodeStatus.active
    sync_role: FederationSyncRole = FederationSyncRole.peer
    heartbeat_at: datetime | None = None
    last_sync_at: datetime | None = None
    config_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class FederationNodeCreate(ApiSchema):
    node_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    instance_url: str = Field(min_length=1, max_length=512)
    public_key: str | None = None
    api_version: str = Field(default="1.0", max_length=24)
    sync_role: FederationSyncRole = FederationSyncRole.peer
    config_json: dict[str, Any] = Field(default_factory=dict)


class FederationNodeUpdate(ApiSchema):
    display_name: str | None = None
    instance_url: str | None = None
    public_key: str | None = None
    api_version: str | None = None
    node_status: FederationNodeStatus | None = None
    sync_role: FederationSyncRole | None = None
    config_json: dict[str, Any] | None = None


class SyncDirection(str, Enum):
    outbound = "outbound"
    inbound = "inbound"


class SyncQueueStatus(str, Enum):
    pending = "pending"
    syncing = "syncing"
    confirmed = "confirmed"
    conflict = "conflict"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class SyncQueueEntryRead(ApiSchema):
    sync_queue_id: UUID
    direction: SyncDirection
    node_id: UUID
    stream_type: str
    stream_id: UUID
    stream_version: int
    event_type: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    sync_status: SyncQueueStatus = SyncQueueStatus.pending
    attempt_count: int = Field(default=0, ge=0)
    last_error: str | None = None
    locked_until: datetime | None = None
    enqueued_at: datetime
    synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ── Sync protocol request/response (for FastAPI validation) ───────────

class SyncPayloadSchema(ApiSchema):
    """One sync payload unit — mirrors an event_log row for transfer."""

    stream_type: str = Field(min_length=1, max_length=80)
    stream_id: UUID
    stream_version: int = Field(ge=0)
    event_type: str = Field(min_length=1, max_length=120)
    payload_json: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    committed_at: str  # ISO-8601
    project_id: UUID | None = None
    actor_type: str | None = None
    actor_id: UUID | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None


class SyncBatchRequest(ApiSchema):
    """Request body for a sync push from source to target."""

    source_node_id: UUID
    protocol_version: str = Field(default="1.0", max_length=24)
    payloads: list[SyncPayloadSchema] = Field(default_factory=list)
    timestamp: str = Field(
        default_factory=lambda: time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
    )
    signature: str | None = None


class ConflictEntrySchema(ApiSchema):
    """Describes one conflict detected during sync."""

    stream_type: str
    stream_id: UUID
    stream_version: int
    local_version: int
    remote_version: int
    reason: str = "version_conflict"


class SyncResultSchema(ApiSchema):
    """Result of processing one sync payload on the receiver side."""

    accepted: bool
    stream_type: str
    stream_id: UUID
    stream_version: int
    reason: str | None = None


class SyncBatchResponse(ApiSchema):
    """Response from target after processing a sync push."""

    accepted_count: int = 0
    conflict_count: int = 0
    skipped_count: int = 0
    errors: list[SyncResultSchema] = Field(default_factory=list)
    conflicts: list[ConflictEntrySchema] = Field(default_factory=list)


class NodeHandshakeRequest(ApiSchema):
    """Sent by a joining node to establish trust."""

    node_code: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    instance_url: str = Field(min_length=1, max_length=512)
    api_version: str = Field(default="1.0", max_length=24)
    public_key: str | None = None
    nonce: str = Field(
        default_factory=lambda: __import__("hashlib").sha256(
            str(time.time()).encode()
        ).hexdigest()[:16]
    )
    signature: str | None = None


class NodeHandshakeResponse(ApiSchema):
    """Response to a handshake request."""

    accepted: bool
    node_id: UUID | None = None
    remote_node_code: str | None = None
    protocol_version: str = "1.0"
    challenge: str | None = None
    error: str | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Graph Trigger Log (L7-03)
# ═══════════════════════════════════════════════════════════════════════════

class GraphTriggerEvent(str, Enum):
    insert = "insert"
    update = "update"
    delete = "delete"
    restore = "restore"


class GraphTriggerAction(str, Enum):
    node_created = "node_created"
    node_updated = "node_updated"
    node_archived = "node_archived"
    node_restored = "node_restored"
    node_deleted = "node_deleted"
    edge_created = "edge_created"
    edge_updated = "edge_updated"
    edge_cancelled = "edge_cancelled"
    skipped_no_change = "skipped_no_change"
    error = "error"


class GraphTriggerLogEntryRead(ApiSchema):
    trigger_log_id: UUID
    trigger_event: GraphTriggerEvent
    memory_id: UUID
    node_id: UUID | None = None
    edge_id: UUID | None = None
    action: GraphTriggerAction
    details_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

