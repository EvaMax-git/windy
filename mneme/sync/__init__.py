"""L7-02 Federation Sync Protocol — pre-wired skeleton.

This package defines the sync protocol for multi-instance Mneme federation.

Architecture
------------
Each Mneme instance operates as a node in a federation mesh.
Nodes are peers unless explicitly configured as leader or readonly.

Sync flow::

    [Source Instance]               [Target Instance(s)]
         │                                │
         │  1. Append event_log           │
         │  2. SyncQueue.enqueue()        │
         │                                │
         │  3. POST /sync/push            │
         │ ──────────────────────────────> │
         │                                │  4. Validate & apply
         │                                │  5. SyncQueue (inbound)
         │                                │  6. Append event_log
         │                                │
         │  7. 200 { accepted, conflicts }│
         │ <────────────────────────────── │
         │                                │
         │  8. SyncQueue.mark_confirmed() │

Conflict resolution
-------------------
* Last-writer-wins (LWW) by ``stream_version`` on the same stream.
* On conflict: the inbound event is written to SyncQueue with status='conflict'.
* A separate sweeper or manual review resolves conflicts.

Protocol version
----------------
Current: ``1.0``
Each SyncQueue entry carries protocol version for future migrations.
"""

from mneme.sync.protocol import (
    SyncPayload,
    SyncResult,
    SyncBatchRequest,
    SyncBatchResponse,
    ConflictEntry,
    NodeHandshakeRequest,
    NodeHandshakeResponse,
    build_sync_payload,
    build_handshake,
    verify_node_identity,
)

__all__ = [
    "SyncPayload",
    "SyncResult",
    "SyncBatchRequest",
    "SyncBatchResponse",
    "ConflictEntry",
    "NodeHandshakeRequest",
    "NodeHandshakeResponse",
    "build_sync_payload",
    "build_handshake",
    "verify_node_identity",
]
