"""L7-02 Federation Sync API — pre-wired endpoints.

Route prefix: ``/sync``

This module provides:
* Node handshake (register/verify peer).
* Outbound push (source instance → target instance).
* Inbound receive (target handles push from source).
* Sync queue management.

.. note::

   This is a **pre-wired skeleton**.  Full federation requires:
   1. A SyncWorker that drains ``sync_queue`` and performs HTTP pushes.
   2. Mutual TLS or token-based authentication between instances.
   3. Conflict resolution sweeper.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import RequestContext, get_request_context
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.federation import (
    create_federation_node,
    delete_federation_node,
    get_federation_node,
    list_federation_nodes,
    update_federation_node,
    list_sync_queue,
)
from mneme.schemas.common import PaginationParams
from mneme.schemas.events import (
    FederationNodeCreate,
    FederationNodeRead,
    FederationNodeUpdate,
    SyncQueueEntryRead,
)
from mneme.schemas.events import (
    NodeHandshakeRequest as NodeHandshakeRequestSchema,
    NodeHandshakeResponse as NodeHandshakeResponseSchema,
    SyncBatchRequest as SyncBatchRequestSchema,
    SyncBatchResponse as SyncBatchResponseSchema,
)

router = APIRouter(prefix="/sync", tags=["sync"])


# ═══════════════════════════════════════════════════════════════════════
# Node Registry
# ═══════════════════════════════════════════════════════════════════════


@router.get("/nodes", response_model=dict)
def list_nodes(
    status: str | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List registered federation peers."""
    items = list_federation_nodes(db, status=status)
    return {"items": [item.model_dump(mode="json") for item in items], "total": len(items)}


@router.post("/nodes", response_model=FederationNodeRead, status_code=201)
def register_node(
    payload: FederationNodeCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> FederationNodeRead:
    """Register a new federation peer node."""
    return create_federation_node(db, payload=payload)


@router.get("/nodes/{node_id}", response_model=FederationNodeRead)
def get_node(
    node_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> FederationNodeRead | None:
    """Get federation node details."""
    return get_federation_node(db, node_id=node_id)


@router.put("/nodes/{node_id}", response_model=FederationNodeRead)
def update_node(
    node_id: UUID,
    payload: FederationNodeUpdate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> FederationNodeRead | None:
    """Update a federation node."""
    return update_federation_node(db, node_id=node_id, payload=payload)


@router.delete("/nodes/{node_id}", response_model=dict)
def remove_node(
    node_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Remove a federation node registration."""
    deleted = delete_federation_node(db, node_id=node_id)
    return {"deleted": deleted}


# ═══════════════════════════════════════════════════════════════════════
# Handshake (unauthenticated — mutual verification)
# ═══════════════════════════════════════════════════════════════════════


@router.post("/handshake", response_model=NodeHandshakeResponseSchema)
def handshake(
    payload: NodeHandshakeRequestSchema,
    db: Session = Depends(get_db),
) -> NodeHandshakeResponseSchema:
    """Receive a handshake from a remote node.

    In production this endpoint must be protected by mutual TLS or
    a pre-shared authentication mechanism.
    """
    # Check if node already registered
    existing = get_federation_node(db, node_code=payload.node_code)
    if existing:
        return NodeHandshakeResponseSchema(
            accepted=True,
            node_id=existing.node_id,
            remote_node_code=existing.node_code,
            protocol_version=existing.api_version,
        )

    # Auto-register if not found
    from mneme.schemas.events import FederationNodeCreate, FederationSyncRole
    create_payload = FederationNodeCreate(
        node_code=payload.node_code,
        display_name=payload.display_name,
        instance_url=payload.instance_url,
        public_key=payload.public_key,
        api_version=payload.api_version,
        sync_role=FederationSyncRole.peer,
    )
    node = create_federation_node(db, payload=create_payload)
    return NodeHandshakeResponseSchema(
        accepted=True,
        node_id=node.node_id,
        remote_node_code=node.node_code,
        protocol_version=node.api_version,
    )


# ═══════════════════════════════════════════════════════════════════════
# Push / Receive Sync
# ═══════════════════════════════════════════════════════════════════════


@router.post("/push", response_model=SyncBatchResponseSchema)
def receive_sync_push(
    payload: SyncBatchRequestSchema,
    db: Session = Depends(get_db),
) -> SyncBatchResponseSchema:
    """Receive a sync push from a remote instance.

    Validates the payloads, enqueues them as inbound sync entries,
    and returns the result summary.
    """
    import logging
    logger = logging.getLogger(__name__)

    from mneme.schemas.events import SyncResultSchema, ConflictEntrySchema
    from mneme.db.event_log import get_latest_stream_version
    from mneme.db.federation import enqueue_inbound_sync

    # Verify source node exists
    source_node = get_federation_node(db, node_id=payload.source_node_id)
    if source_node is None:
        return SyncBatchResponseSchema(
            accepted_count=0,
            errors=[SyncResultSchema(
                accepted=False,
                stream_type="*",
                stream_id=UUID(int=0),
                stream_version=0,
                reason=f"unknown source node: {payload.source_node_id}",
            )],
        )

    response = SyncBatchResponseSchema()

    for sp in payload.payloads:
        try:
            # Check for version conflicts
            latest_version = get_latest_stream_version(
                db,
                stream_type=sp.stream_type,
                stream_id=sp.stream_id,
            )

            if sp.stream_version <= latest_version:
                # Possible conflict or duplicate
                if sp.stream_version < latest_version:
                    response.conflicts.append(ConflictEntrySchema(
                        stream_type=sp.stream_type,
                        stream_id=sp.stream_id,
                        stream_version=sp.stream_version,
                        local_version=latest_version,
                        remote_version=sp.stream_version,
                        reason="version_conflict",
                    ))
                    response.conflict_count += 1
                else:
                    response.skipped_count += 1
                continue

            # Enqueue as inbound sync
            enqueue_inbound_sync(
                db,
                node_id=payload.source_node_id,
                stream_type=sp.stream_type,
                stream_id=sp.stream_id,
                stream_version=sp.stream_version,
                event_type=sp.event_type,
                payload=sp.payload_json,
            )
            response.accepted_count += 1

        except Exception as exc:
            logger.exception("Failed to process sync payload")
            response.errors.append(SyncResultSchema(
                accepted=False,
                stream_type=sp.stream_type,
                stream_id=sp.stream_id,
                stream_version=sp.stream_version,
                reason=str(exc),
            ))

    return response


# ═══════════════════════════════════════════════════════════════════════
# Sync Queue Management
# ═══════════════════════════════════════════════════════════════════════


@router.get("/queue", response_model=dict)
def get_sync_queue(
    direction: str | None = None,
    status: str | None = None,
    node_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List sync queue entries."""
    items, total = list_sync_queue(
        db,
        direction=direction,
        status=status,
        node_id=node_id,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
